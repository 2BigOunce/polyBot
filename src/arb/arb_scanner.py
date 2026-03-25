"""
Resolution Arbitrage — Phase 6: Orderbook Scanner

After a market resolves on-chain, attempts to buy stale sell orders on the
winning token before (fast path) and after (slow path) the CLOB marks it closed.

Fast path: buy immediately after ConditionResolution, market still "open" on CLOB.
  - ConditionResolution is final (post-UMA challenge) — on-chain data is authoritative.
  - Stale orders exist in the book and can be matched like any normal trade.
  - Eliminates the ~1-5s CLOB processing delay.

Slow path: fallback if fast path finds nothing (orders already cancelled).
  - Waits for closed=true, cross-checks CLOB winner flag, retries with fresh book.

Sizing strategy:
    price < 0.08  → 30% of capital (high profit multiple: x10+)
    price < 0.30  → 15% of capital
    price >= 0.30 → 10% of capital
"""

import asyncio

import aiohttp
from telegram import Bot

from config.settings import ARB_CAPITAL, ARB_MAX_SINGLE_TRADE, ARB_MAX_STALE_PRICE, REAL_TRADING_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from src.arb.clob_ws import get_closed_event, get_cached_book, get_cached_stale, subscribe_orderbook
from src.arb.market_cache import get_cached_market

_bot: Bot | None = None


def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return _bot

CLOB_API = "https://clob.polymarket.com"

# Deduplication: avoid scanning the same conditionId twice
# (NegRisk markets can fire multiple ConditionResolution events rapidly)
_scanned: set[str] = set()

_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
        _session = aiohttp.ClientSession(connector=connector)
    return _session


def _get_arb_size(stale_price: float) -> float:
    """
    Return max USDC to deploy at a given stale price level.
    Lower price = larger profit multiple = bigger bet.
    """
    if stale_price < 0.08:
        return min(ARB_CAPITAL * 0.30, ARB_MAX_SINGLE_TRADE)
    elif stale_price < 0.30:
        return min(ARB_CAPITAL * 0.15, ARB_MAX_SINGLE_TRADE)
    else:
        return min(ARB_CAPITAL * 0.10, ARB_MAX_SINGLE_TRADE)


def _find_stale(book: dict) -> list[dict]:
    """Return stale asks sorted cheapest first (highest profit)."""
    asks = book.get("asks", [])
    return sorted(
        [a for a in asks if float(a.get("price", 1.0)) <= ARB_MAX_STALE_PRICE],
        key=lambda a: float(a["price"]),
    )


async def _sweep(
    stale: list[dict],
    token_id: str,
    question: str,
    winner: str,
    condition_id: str,
    session: aiohttp.ClientSession,
) -> None:
    """Build opportunities from stale asks and execute (or alert in dry-run)."""
    remaining_budget = ARB_CAPITAL
    opportunities: list[dict] = []

    for ask in stale:
        price = float(ask["price"])
        shares_available = float(ask["size"])
        cost_usdc = shares_available * price
        budget_for_level = min(_get_arb_size(price), remaining_budget)

        if budget_for_level <= 0:
            break

        size_usdc = min(cost_usdc, budget_for_level)
        shares_to_buy = size_usdc / price

        opportunities.append({
            "price": price,
            "shares": shares_to_buy,
            "size_usdc": size_usdc,
            "profit_x": round(1.0 / price, 1),
        })
        remaining_budget -= size_usdc

    if not opportunities:
        return

    total_cost   = sum(o["size_usdc"] for o in opportunities)
    total_profit = sum(o["shares"] * (1.0 - o["price"]) for o in opportunities)
    best_price   = opportunities[0]["price"]

    print(
        f"[arb] 🎯 {question[:50]}\n"
        f"[arb]    {winner} | {len(opportunities)} price level(s)"
        f" | best={best_price:.3f} | cost={total_cost:.0f} USDC"
        f" | profit≈{total_profit:.0f} USDC"
    )

    if not REAL_TRADING_ENABLED:
        multiple = round(1.0 / best_price, 1)
        msg = (
            f"👁 ARB OPPORTUNITY (dry run)\n"
            f"Market: {question[:60]}\n"
            f"Winner: {winner}\n"
            f"Best price: {best_price:.3f} (x{multiple})\n"
            f"Cost: {total_cost:.0f} USDC → Profit ≈ {total_profit:.0f} USDC\n"
            f"({len(opportunities)} price level(s))"
        )
        await _get_bot().send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        return

    try:
        from src.trading.arb_executor import execute_arb  # VPS-only, not committed
        await execute_arb(
            token_id=token_id,
            opportunities=opportunities,
            condition_id=condition_id,
            question=question,
            winner=winner,
            session=session,
        )
    except ImportError:
        print("[arb] arb_executor not available — deploy src/trading/arb_executor.py on VPS")


async def scan_resolved_market(condition_id: str, winner_index: int) -> None:
    """
    Given a resolved conditionId and the winning outcome index:
      Fast path — buy stale orders immediately (before CLOB closes the market).
      Slow path — wait for closed=true and retry if fast path finds nothing.
    """
    if condition_id in _scanned:
        return
    _scanned.add(condition_id)

    session = _get_session()
    closed_event = get_closed_event(condition_id)

    # ── Step 1: resolve token_id from cache or HTTP ───────────────────────────
    cached = get_cached_market(condition_id)

    market = None
    if cached:
        tokens = cached.get("tokens", [])
        if winner_index >= len(tokens):
            return
        winning_token = tokens[winner_index]
        token_id      = winning_token.get("token_id")
        winner        = winning_token.get("outcome", f"outcome_{winner_index}")
        question      = cached.get("question", condition_id[:20])
        print(f"[arb] ⚡ cache hit — skipped market HTTP fetch ({condition_id[:14]}…)")
    else:
        try:
            async with session.get(f"{CLOB_API}/markets/{condition_id}") as resp:
                if resp.status == 404:
                    print(f"[arb] 404 — not a Polymarket market: {condition_id[:14]}…")
                    return
                market = await resp.json()
        except Exception as e:
            print(f"[arb] market fetch failed ({condition_id[:14]}…): {e}")
            return

        tokens = market.get("tokens", [])
        if winner_index >= len(tokens):
            return

        winning_token = tokens[winner_index]
        token_id      = winning_token.get("token_id")
        winner        = winning_token.get("outcome", f"outcome_{winner_index}")
        question      = market.get("question", condition_id[:20])

    if not token_id:
        return

    # Subscribe to orderbook WS immediately — book builds in RAM during CLOB delay
    await subscribe_orderbook(token_id)

    if market and market.get("closed", False):
        closed_event.set()

    # ── Step 2: fast path — buy before CLOB marks market closed ───────────────
    # ConditionResolution is final (post-UMA challenge window).
    # Stale orders at sub-cent prices can be matched like normal trades.
    # We skip the CLOB winner flag check here — trust on-chain payoutNumerators.
    book = get_cached_book(token_id)
    if book is None:
        try:
            async with session.get(
                f"{CLOB_API}/book", params={"token_id": token_id}
            ) as resp:
                book = await resp.json()
        except Exception as e:
            print(f"[arb] fast path book fetch failed: {e}")

    # Use pre-computed stale asks if available (built during CLOB delay), else compute now
    stale = get_cached_stale(token_id)
    if stale is None and book:
        stale = _find_stale(book)

    if stale:
        already_closed = bool(market and market.get("closed", False))
        # If CLOB already closed, cross-check winner flag before buying
        if already_closed and not winning_token.get("winner", False):
            print(f"[arb] ⚠️  fast path winner flag mismatch — skipping {condition_id[:14]}…")
            return
        label = "CLOSED" if already_closed else "PRE-CLOSE ⚡"
        print(f"[arb] {label} — {len(stale)} stale ask(s) | {condition_id[:14]}…")
        await _sweep(stale, token_id, question, winner, condition_id, session)
        return

    # If book was fetched and completely empty, no slow path needed — no orders to find
    if book is not None and not book.get("asks"):
        return

    print(f"[arb] fast path: no stale asks | {condition_id[:14]}…")

    # ── Step 3: slow path — wait for closed=true + retry ─────────────────────
    # Fast path found no stale orders (likely already cancelled by MMs).
    # Wait for CLOB to confirm closed, then check once more with a fresh book.
    if not closed_event.is_set():
        try:
            await asyncio.wait_for(closed_event.wait(), timeout=10.0)
            print(f"[arb] WS push received — {condition_id[:14]}…")
        except asyncio.TimeoutError:
            print(f"[arb] WS timeout — HTTP fallback for {condition_id[:14]}…")

    # Confirm closed via HTTP polling if not yet confirmed
    if not (market and market.get("closed", False)):
        for attempt in range(180):
            try:
                async with session.get(f"{CLOB_API}/markets/{condition_id}") as resp:
                    if resp.status == 404:
                        return
                    if resp.status == 429:
                        await asyncio.sleep(2.0)
                        continue
                    market = await resp.json()
            except Exception as e:
                print(f"[arb] slow path fetch failed (attempt {attempt+1}): {e}")
                await asyncio.sleep(1.0)
                continue

            if market.get("closed", False):
                break

            await asyncio.sleep(1.0)
        else:
            print(f"[arb] market never closed on CLOB after 3min — skipping {condition_id[:14]}…")
            return

    # Safety check: verify CLOB winner flag now that market is officially closed
    tokens = market.get("tokens", [])
    if winner_index >= len(tokens):
        return
    winning_token = tokens[winner_index]
    if not winning_token.get("winner", False):
        print(f"[arb] ⚠️  on-chain index {winner_index} != CLOB winner flag — skipping {condition_id[:14]}…")
        return

    # Fresh orderbook — use WS cache or REST
    book = get_cached_book(token_id)
    if book is not None:
        print(f"[arb] 📚 using cached orderbook ({condition_id[:14]}…)")
    else:
        try:
            async with session.get(
                f"{CLOB_API}/book", params={"token_id": token_id}
            ) as resp:
                book = await resp.json()
        except Exception as e:
            print(f"[arb] orderbook fetch failed: {e}")
            return

    stale = get_cached_stale(token_id) or _find_stale(book)
    if not stale:
        return

    await _sweep(stale, token_id, question, winner, condition_id, session)

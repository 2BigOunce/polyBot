"""
Resolution Arbitrage — Phase 6: Orderbook Scanner

After a market resolves, queries the CLOB orderbook for stale sell orders
on the winning token. If profitable asks are found, triggers the executor.

Sizing strategy (calibrated on observed bot behavior):
    price < 0.08  → 50% of capital (high profit multiple: x10+)
    price < 0.30  → 20% of capital
    price >= 0.30 → 5%  of capital (smaller margin, smaller bet)
"""

import asyncio

import aiohttp

from config.settings import ARB_CAPITAL, ARB_MAX_STALE_PRICE, REAL_TRADING_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

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
        return ARB_CAPITAL * 0.30
    elif stale_price < 0.30:
        return ARB_CAPITAL * 0.15
    else:
        return ARB_CAPITAL * 0.10


async def scan_resolved_market(condition_id: str, winner_index: int) -> None:
    """
    Given a resolved conditionId and the winning outcome index:
      1. Deduplicate (skip if already scanned)
      2. Fetch market info — verify it is officially closed
      3. Find the winning token_id by index (works for binary AND multi-outcome)
      4. Query orderbook for ALL stale asks below ARB_MAX_STALE_PRICE
      5. Sweep them all (cheapest first) up to our capital budget
    """
    if condition_id in _scanned:
        return
    _scanned.add(condition_id)

    session = _get_session()

    from src.arb.clob_ws import get_closed_event, get_cached_book, subscribe_orderbook

    # Register WS closed event before any network call (avoid race condition)
    closed_event = get_closed_event(condition_id)

    # Step 1 — optimistic market fetch: get token_id immediately, don't wait for closed.
    # The CLOB processing delay (~1-5s) is used productively:
    #   we subscribe to the orderbook WS now so the book builds in memory while we wait.
    market = None
    try:
        async with session.get(f"{CLOB_API}/markets/{condition_id}") as resp:
            if resp.status == 404:
                print(f"[arb] 404 — not a Polymarket market: {condition_id[:14]}…")
                return
            market = await resp.json()
    except Exception as e:
        print(f"[arb] optimistic fetch failed ({condition_id[:14]}…): {e}")

    if market:
        # Subscribe to orderbook WS immediately — book will be ready when market closes
        tokens = market.get("tokens", [])
        if winner_index < len(tokens):
            early_token_id = tokens[winner_index].get("token_id")
            if early_token_id:
                await subscribe_orderbook(early_token_id)

        # If market is already closed (rare), signal immediately and skip the wait
        if market.get("closed", False):
            closed_event.set()

    # Step 2 — wait for CLOB to mark the market as closed (WS push or timeout)
    if not closed_event.is_set():
        try:
            await asyncio.wait_for(closed_event.wait(), timeout=10.0)
            print(f"[arb] WS push received — {condition_id[:14]}…")
        except asyncio.TimeoutError:
            print(f"[arb] WS timeout — HTTP fallback for {condition_id[:14]}…")

    # Step 3 — confirm closed via HTTP if not already confirmed
    if market is None or not market.get("closed", False):
        for attempt in range(90):
            try:
                async with session.get(f"{CLOB_API}/markets/{condition_id}") as resp:
                    if resp.status == 404:
                        print(f"[arb] 404 — not a Polymarket market: {condition_id[:14]}…")
                        return
                    market = await resp.json()
            except Exception as e:
                print(f"[arb] market fetch failed ({condition_id[:14]}… attempt {attempt+1}): {e}")
                await asyncio.sleep(0.1)
                continue

            if market.get("closed", False):
                break

            await asyncio.sleep(0.1)
        else:
            print(f"[arb] market never closed on CLOB — skipping {condition_id[:14]}…")
            return

    question = market.get("question", condition_id[:20])

    # Step 2 — find the winning token_id by index (works for any number of outcomes)
    # Cross-check: on-chain winner_index must match CLOB token.winner flag
    tokens = market.get("tokens", [])
    if winner_index >= len(tokens):
        return  # index out of range — skip silently
    winning_token = tokens[winner_index]
    token_id = winning_token.get("token_id")
    winner = winning_token.get("outcome", f"outcome_{winner_index}")

    if not token_id:
        return

    # Safety check: verify CLOB also marks this token as winner
    if not winning_token.get("winner", False):
        print(f"[arb] ⚠️  on-chain index {winner_index} != CLOB winner flag — skipping {condition_id[:14]}…")
        return

    # Step 3 — get orderbook: in-memory cache (0ms) or fallback to REST
    book = get_cached_book(token_id)
    if book is not None:
        print(f"[arb] 📚 using cached orderbook ({condition_id[:14]}…)")
    else:
        try:
            async with session.get(
                f"{CLOB_API}/orderbook",
                params={"token_id": token_id},
            ) as resp:
                book = await resp.json()
        except Exception as e:
            print(f"[arb] orderbook fetch failed: {e}")
            return

    # asks = people selling the token (we buy from them)
    # Each ask: {"price": "0.02", "size": "500"} — size is in shares
    asks = book.get("asks", [])
    stale = sorted(
        [a for a in asks if float(a.get("price", 1.0)) <= ARB_MAX_STALE_PRICE],
        key=lambda a: float(a["price"]),  # cheapest first = highest profit
    )

    if not stale:
        return  # nothing to arb

    # Step 4 — sweep all stale asks, cheapest first, up to capital budget (hard cap: ARB_MAX_SINGLE_TRADE)
    remaining_budget = ARB_CAPITAL
    opportunities: list[dict] = []

    for ask in stale:
        price = float(ask["price"])
        shares_available = float(ask["size"])
        cost_usdc = shares_available * price          # USDC to buy all shares at this level
        budget_for_level = min(_get_arb_size(price), remaining_budget)

        if budget_for_level <= 0:
            break

        # How many shares can we afford at this level?
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
        # Dry run — send Telegram alert without placing any order
        from telegram import Bot
        multiple = round(1.0 / best_price, 1)
        msg = (
            f"👁 ARB OPPORTUNITY (dry run)\n"
            f"Market: {question[:60]}\n"
            f"Winner: {winner}\n"
            f"Best price: {best_price:.3f} (x{multiple})\n"
            f"Cost: {total_cost:.0f} USDC → Profit ≈ {total_profit:.0f} USDC\n"
            f"({len(opportunities)} price level(s))"
        )
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        return

    try:
        from src.trading.arb_executor import execute_arb
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

"""
Resolution Arbitrage — Phase 6: Orderbook Scanner

After a market resolves, queries the CLOB orderbook for stale sell orders
on the winning token. If profitable asks are found, triggers the executor.

Sizing strategy (calibrated on observed bot behavior):
    price < 0.08  → 50% of capital (high profit multiple: x10+)
    price < 0.30  → 20% of capital
    price >= 0.30 → 5%  of capital (smaller margin, smaller bet)
"""

import aiohttp

from config.settings import ARB_CAPITAL, ARB_MAX_STALE_PRICE

CLOB_API = "https://clob.polymarket.com"

_session: aiohttp.ClientSession | None = None

# Deduplication: avoid scanning the same conditionId twice
# (NegRisk markets can fire multiple ConditionResolution events rapidly)
_scanned: set[str] = set()


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


def _get_arb_size(stale_price: float) -> float:
    """
    Return max USDC to deploy at a given stale price level.
    Lower price = larger profit multiple = bigger bet.
    """
    if stale_price < 0.08:
        return min(ARB_CAPITAL * 0.50, 500.0)
    elif stale_price < 0.30:
        return min(ARB_CAPITAL * 0.20, 200.0)
    else:
        return min(ARB_CAPITAL * 0.05, 50.0)


async def scan_resolved_market(condition_id: str, winner: str) -> None:
    """
    Given a resolved conditionId and its winning outcome:
      1. Deduplicate (skip if already scanned)
      2. Fetch market info — verify it is officially closed
      3. Find the winning token_id
      4. Query orderbook for ALL stale asks below ARB_MAX_STALE_PRICE
      5. Sweep them all (cheapest first) up to our capital budget
    """
    if condition_id in _scanned:
        return
    _scanned.add(condition_id)

    session = _get_session()

    # Step 1 — fetch market info and verify it is closed
    try:
        async with session.get(f"{CLOB_API}/markets/{condition_id}") as resp:
            if resp.status == 404:
                return  # not a Polymarket market (other protocols use same CTF)
            market = await resp.json()
    except Exception as e:
        print(f"[arb] market fetch failed ({condition_id[:14]}…): {e}")
        return

    if not market.get("closed", False):
        # Market not yet officially closed on CLOB side — skip
        return

    question = market.get("question", condition_id[:20])

    # Step 2 — find the winning token_id
    token_id = None
    for token in market.get("tokens", []):
        if token.get("outcome", "").upper() == winner.upper():
            token_id = token.get("token_id")
            break

    if not token_id:
        return  # token not found — NegRisk format mismatch, skip silently

    # Step 3 — query orderbook for stale asks
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

    # Step 4 — sweep all stale asks, cheapest first, up to capital budget
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

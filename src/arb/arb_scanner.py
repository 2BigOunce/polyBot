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


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


def _get_arb_size(stale_price: float) -> float:
    """
    Return USDC size to deploy based on how stale the price is.
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
      1. Fetch market info from CLOB to get the winning token_id
      2. Query orderbook for stale sell orders (asks) below ARB_MAX_STALE_PRICE
      3. If found, trigger the arb executor
    """
    session = _get_session()

    # Step 1 — fetch market info
    try:
        async with session.get(f"{CLOB_API}/markets/{condition_id}") as resp:
            market = await resp.json()
    except Exception as e:
        print(f"[arb] market fetch failed ({condition_id[:14]}…): {e}")
        return

    question = market.get("question", condition_id[:20])

    # Step 2 — find the winning token_id
    token_id = None
    for token in market.get("tokens", []):
        if token.get("outcome", "").upper() == winner.upper():
            token_id = token.get("token_id")
            break

    if not token_id:
        print(f"[arb] No {winner} token found for: {question[:50]}")
        return

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

    asks = book.get("asks", [])
    stale = [a for a in asks if float(a.get("price", 1.0)) <= ARB_MAX_STALE_PRICE]

    if not stale:
        return  # no stale orders — nothing to arb

    # Take the cheapest ask first (highest profit multiple)
    best = min(stale, key=lambda a: float(a["price"]))
    price = float(best["price"])
    size  = _get_arb_size(price)
    profit_multiple = round(1.0 / price, 1)

    print(
        f"[arb] 🎯 STALE ORDER — {question[:50]}\n"
        f"[arb]    {winner} token | price={price:.3f} | size={size:.0f} USDC"
        f" | x{profit_multiple} profit"
    )

    try:
        from src.trading.arb_executor import execute_arb
        await execute_arb(
            token_id=token_id,
            stale_price=price,
            size_usdc=size,
            condition_id=condition_id,
            question=question,
            winner=winner,
            session=session,
        )
    except ImportError:
        print("[arb] arb_executor not available (deploy src/trading/arb_executor.py on VPS)")

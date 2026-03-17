"""
Pre-entry CLOB history check.

Fetches a wallet's historical Polymarket activity and computes its win rate
on closed markets. Used by deposit_monitor to reject wallets with a bad
track record before they enter the watchlist.
"""

import asyncio

import aiohttp

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

_sem = asyncio.Semaphore(5)

# Reject thresholds
_MIN_CLOSED_STRICT = 10   # ≥10 closed trades and WR < 40%  → reject
_MAX_WR_STRICT     = 0.40
_MIN_CLOSED_HARSH  = 5    # ≥5 closed trades  and WR < 25%  → reject (very bad)
_MAX_WR_HARSH      = 0.25


async def _get_winning_outcome(market: str, session: aiohttp.ClientSession) -> str | None:
    """Return the winning outcome string if the market is closed, else None."""
    try:
        async with session.get(f"{CLOB_API}/markets/{market}") as resp:
            data = await resp.json()
        if not data.get("closed"):
            return None
        for token in data.get("tokens", []):
            if token.get("winner"):
                return token.get("outcome")
    except Exception:
        pass
    return None


async def fetch_wallet_wr(wallet: str, session: aiohttp.ClientSession) -> dict:
    """
    Compute a wallet's historical win rate from resolved Polymarket markets.

    Returns
    -------
    dict with keys:
        closed   : number of resolved markets the wallet had a BUY position on
        wins     : number of those that resolved in the wallet's favour
        win_rate : wins / closed  (0.0 if no closed positions found)
    """
    async with _sem:
        try:
            async with session.get(
                f"{DATA_API}/activity",
                params={"user": wallet, "limit": 200},
            ) as resp:
                trades = await resp.json()
        except Exception as e:
            print(f"[clob_history] activity fetch failed for {wallet[:10]}…: {e}")
            return {"closed": 0, "wins": 0, "win_rate": 0.0}

        if not isinstance(trades, list):
            return {"closed": 0, "wins": 0, "win_rate": 0.0}

        # Collect distinct (conditionId, outcome) BUY positions
        positions: dict[tuple[str, str], None] = {}
        for trade in trades:
            if trade.get("side") != "BUY":
                continue
            market  = trade.get("conditionId", "")
            outcome = trade.get("outcome", "")
            if market and outcome:
                positions[(market, outcome)] = None

        if not positions:
            return {"closed": 0, "wins": 0, "win_rate": 0.0}

        # Resolve each distinct market (one API call per market, not per trade)
        distinct_markets = {m for m, _ in positions}
        winning_outcomes: dict[str, str | None] = {}
        for market in distinct_markets:
            winning_outcomes[market] = await _get_winning_outcome(market, session)

        # Count wins and losses on resolved markets only
        closed = 0
        wins   = 0
        for (market, outcome) in positions:
            winner = winning_outcomes.get(market)
            if winner is None:
                continue  # market still open — skip
            closed += 1
            if outcome.lower() == winner.lower():
                wins += 1

        win_rate = wins / closed if closed > 0 else 0.0
        return {"closed": closed, "wins": wins, "win_rate": win_rate}


async def should_reject_wallet(wallet: str, session: aiohttp.ClientSession) -> bool:
    """
    Return True if the wallet's CLOB history is bad enough to reject at entry.

    Rejection criteria:
      - ≥10 closed positions with WR < 40%   (statistically significant)
      - ≥ 5 closed positions with WR < 25%   (very bad, even small sample)

    Wallets with no history (new to Polymarket) always return False.
    """
    wr = await fetch_wallet_wr(wallet, session)
    closed = wr["closed"]
    rate   = wr["win_rate"]

    if closed >= _MIN_CLOSED_STRICT and rate < _MAX_WR_STRICT:
        print(f"[entry_filter] {wallet[:10]}… rejected — WR {rate:.0%} over {closed} closed trades (≥10, <40%)")
        return True
    if closed >= _MIN_CLOSED_HARSH and rate < _MAX_WR_HARSH:
        print(f"[entry_filter] {wallet[:10]}… rejected — WR {rate:.0%} over {closed} closed trades (≥5, <25%)")
        return True

    if closed > 0:
        print(f"[entry_filter] {wallet[:10]}… accepted — WR {rate:.0%} over {closed} closed trades")
    else:
        print(f"[entry_filter] {wallet[:10]}… accepted — no CLOB history (new to Polymarket)")

    return False

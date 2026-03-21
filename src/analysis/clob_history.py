"""
Pre-entry CLOB history evaluation.

Fetches a wallet's 90-day Polymarket activity and applies insider entry filters:
  - Too many distinct markets → likely a trader, not an insider
  - Too many sports trades    → sports bettor, not an insider
  - No history (0 trades)    → new to Polymarket, monitor until 3rd trade

Also exposes fetch_wallet_wr() for backfill scripts.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp

from config.settings import INSIDER_MAX_MARKETS, INSIDER_MAX_SPORTS_PCT

DATA_API    = "https://data-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
WINDOW_DAYS = 90

_sem = asyncio.Semaphore(5)


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


async def _get_market_tags(market: str, session: aiohttp.ClientSession) -> list[str]:
    """Return the tags list for a market from the CLOB API."""
    try:
        async with session.get(f"{CLOB_API}/markets/{market}") as resp:
            data = await resp.json()
        return data.get("tags") or []
    except Exception:
        return []


async def evaluate_wallet_entry(
    wallet: str, session: aiohttp.ClientSession
) -> tuple[bool, str, dict]:
    """
    Evaluate a wallet against the insider entry filters using its last 90 days of activity.

    Returns (rejected, reason, stats) where:
      - rejected    : True → silently discard, don't add to watchlist
      - reason      : human-readable rejection reason (empty string if accepted)
      - stats       : dict with keys: trades_count, distinct_markets, sports_pct, has_history

    Filters applied:
      1. Distinct BUY markets > INSIDER_MAX_MARKETS (3) → reject (trader, not insider)
      2. Sports trades > INSIDER_MAX_SPORTS_PCT (30%)   → reject (sports bettor)
      3. 0 trades → accept but mark has_history=False   (alert at 3rd trade instead)
    """
    async with _sem:
        try:
            async with session.get(
                f"{DATA_API}/activity",
                params={"user": wallet, "limit": 500},
            ) as resp:
                all_trades = await resp.json()
        except Exception as e:
            print(f"[entry_filter] activity fetch failed for {wallet[:10]}…: {e}")
            return False, "", {"trades_count": 0, "distinct_markets": 0, "sports_pct": 0.0, "has_history": False}

    if not isinstance(all_trades, list):
        return False, "", {"trades_count": 0, "distinct_markets": 0, "sports_pct": 0.0, "has_history": False}

    # Filter to the 90-day window
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=WINDOW_DAYS)
    recent = []
    for t in all_trades:
        ts = t.get("timestamp")
        if ts:
            try:
                dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                if dt >= cutoff:
                    recent.append(t)
            except Exception:
                pass

    if not recent:
        print(f"[entry_filter] {wallet[:10]}… accepted — no activity in last {WINDOW_DAYS}d (new to Polymarket)")
        return False, "", {"trades_count": 0, "distinct_markets": 0, "sports_pct": 0.0, "has_history": False}

    # Count distinct BUY markets
    buy_markets: set[str] = set()
    total_buys = 0
    for t in recent:
        if t.get("side") == "BUY":
            market = t.get("conditionId", "")
            if market:
                buy_markets.add(market)
                total_buys += 1

    distinct_markets = len(buy_markets)

    # Filter 1: too many distinct markets
    if distinct_markets > INSIDER_MAX_MARKETS:
        reason = f"{distinct_markets} distinct markets in 90d (max {INSIDER_MAX_MARKETS})"
        print(f"[entry_filter] {wallet[:10]}… rejected — {reason}")
        return True, reason, {
            "trades_count": total_buys, "distinct_markets": distinct_markets,
            "sports_pct": 0.0, "has_history": True,
        }

    # Check sports % — fetch tags for each distinct market (at most INSIDER_MAX_MARKETS calls)
    sports_markets: set[str] = set()
    for market in buy_markets:
        tags = await _get_market_tags(market, session)
        if "Sports" in tags:
            sports_markets.add(market)

    sports_count = sum(
        1 for t in recent
        if t.get("side") == "BUY" and t.get("conditionId") in sports_markets
    )
    sports_pct = sports_count / total_buys if total_buys > 0 else 0.0

    # Filter 2: too many sports trades
    if sports_pct > INSIDER_MAX_SPORTS_PCT:
        reason = f"{sports_pct:.0%} sports trades in 90d (max {INSIDER_MAX_SPORTS_PCT:.0%})"
        print(f"[entry_filter] {wallet[:10]}… rejected — {reason}")
        return True, reason, {
            "trades_count": total_buys, "distinct_markets": distinct_markets,
            "sports_pct": sports_pct, "has_history": True,
        }

    # Build market label for summary
    market_list = list(buy_markets)
    print(
        f"[entry_filter] {wallet[:10]}… accepted — "
        f"{distinct_markets} markets, {sports_pct:.0%} sports, {total_buys} buys (90d)"
    )
    return False, "", {
        "trades_count": total_buys,
        "distinct_markets": distinct_markets,
        "sports_pct": sports_pct,
        "has_history": total_buys > 0,
        "markets": market_list,
    }

"""
Pre-caches active market metadata (token_ids, question, outcome names).

Refreshes every 30 minutes in the background.
Used by arb_scanner to skip the market HTTP fetch at resolution time (~50ms saved).

Cache hit  → token_id available instantly, no HTTP call needed.
Cache miss → falls back to HTTP fetch (no worse than current behavior).

Also pre-subscribes to orderbook WS for markets ending within 6 hours,
so the book is already in RAM when the resolution fires.
"""

import asyncio

import aiohttp

from src.arb.clob_ws import subscribe_orderbook_bulk

CLOB_API         = "https://clob.polymarket.com"
REFRESH_INTERVAL = 1800  # 30 minutes

# condition_id → {"tokens": [{"token_id": ..., "outcome": ...}, ...], "question": ...}
_cache: dict[str, dict] = {}

# token_ids already subscribed — avoid re-queuing on every 30min refresh
_subscribed: set[str] = set()


def get_cached_market(condition_id: str) -> dict | None:
    """Return cached market metadata or None if not cached."""
    return _cache.get(condition_id)


async def _refresh(session: aiohttp.ClientSession) -> tuple[int, int]:
    """
    Fetch active markets, populate cache, and pre-subscribe to books of
    markets ending within PRESUB_HOURS. Returns (cached_count, new_subscriptions).
    """
    try:
        async with session.get(
            f"{CLOB_API}/markets",
            params={"active": "true", "limit": "500"},
        ) as resp:
            data = await resp.json()
    except Exception as e:
        print(f"[market_cache] fetch failed: {e}")
        return 0, 0

    markets = data.get("data", []) if isinstance(data, dict) else data
    count = 0
    new_subs = 0

    for m in markets:
        cid = m.get("condition_id")
        if not cid:
            continue

        _cache[cid] = {
            "tokens":   m.get("tokens", []),
            "question": m.get("question", cid[:20]),
        }
        count += 1

    # Pre-subscribe to all token books in one bulk call (avoids yielding per token)
    new_tids = []
    for m in markets:
        for token in m.get("tokens", []):
            tid = token.get("token_id")
            if tid and tid not in _subscribed:
                new_tids.append(tid)
                _subscribed.add(tid)
    if new_tids:
        subscribe_orderbook_bulk(new_tids)
        new_subs = len(new_tids)

    return count, new_subs


async def run_market_cache() -> None:
    """Background task: refresh the market cache every 30 minutes."""
    async with aiohttp.ClientSession() as session:
        while True:
            n, s = await _refresh(session)
            print(f"[market_cache] cached {n} active markets | pre-subscribed {s} new token books")
            await asyncio.sleep(REFRESH_INTERVAL)

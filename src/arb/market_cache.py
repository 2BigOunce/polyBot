"""
Pre-caches active market metadata (token_ids, question, outcome names).

Refreshes every 30 minutes in the background.
Used by arb_scanner to skip the market HTTP fetch at resolution time (~50ms saved).

Cache hit  → token_id available instantly, no HTTP call needed.
Cache miss → falls back to HTTP fetch (no worse than current behavior).
"""

import asyncio

import aiohttp

CLOB_API         = "https://clob.polymarket.com"
REFRESH_INTERVAL = 1800  # 30 minutes

# condition_id → {"tokens": [{"token_id": ..., "outcome": ...}, ...], "question": ...}
_cache: dict[str, dict] = {}


def get_cached_market(condition_id: str) -> dict | None:
    """Return cached market metadata or None if not cached."""
    return _cache.get(condition_id)


async def _refresh(session: aiohttp.ClientSession) -> int:
    """Fetch one page of active markets and populate the cache. Returns count cached."""
    try:
        async with session.get(
            f"{CLOB_API}/markets",
            params={"active": "true", "limit": "500"},
        ) as resp:
            data = await resp.json()
    except Exception as e:
        print(f"[market_cache] fetch failed: {e}")
        return 0

    markets = data.get("data", []) if isinstance(data, dict) else data
    count = 0
    for m in markets:
        cid = m.get("condition_id")
        if not cid:
            continue
        _cache[cid] = {
            "tokens":   m.get("tokens", []),
            "question": m.get("question", cid[:20]),
        }
        count += 1

    return count


async def run_market_cache() -> None:
    """Background task: refresh the market cache every 30 minutes."""
    async with aiohttp.ClientSession() as session:
        while True:
            n = await _refresh(session)
            print(f"[market_cache] cached {n} active markets")
            await asyncio.sleep(REFRESH_INTERVAL)

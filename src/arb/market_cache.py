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
import json

import aiohttp

from src.arb.clob_ws import subscribe_orderbook_bulk

GAMMA_API        = "https://gamma-api.polymarket.com"
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
    Fetch active markets from Gamma API, populate cache, pre-subscribe to orderbooks.
    Returns (cached_count, new_subscriptions).
    """
    count    = 0
    new_tids = []
    offset   = 0

    while True:
        try:
            async with session.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false", "limit": "100", "offset": str(offset)},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
        except Exception as e:
            print(f"[market_cache] fetch failed: {e}")
            break

        if not isinstance(data, list) or not data:
            break

        for m in data:
            cid = m.get("conditionId") or m.get("condition_id")
            if not cid:
                continue

            tokens = m.get("tokens") or []
            # Gamma API stores token IDs in clobTokenIds (JSON string or list)
            clob_ids = m.get("clobTokenIds") or []
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except Exception:
                    clob_ids = []

            _cache[cid] = {
                "tokens":   [{"token_id": t.get("token_id") or t.get("tokenId", ""), "outcome": t.get("outcome", "")} for t in tokens],
                "question": m.get("question", cid[:20]),
            }
            count += 1

            # Collect token IDs from either source
            token_ids: list[str] = [t.get("token_id") or t.get("tokenId", "") for t in tokens]
            if not any(token_ids):
                token_ids = [str(t) for t in clob_ids if t]

            for tid in token_ids:
                if tid and tid not in _subscribed:
                    new_tids.append(tid)
                    _subscribed.add(tid)

        if len(data) < 100:
            break
        offset += 100
        await asyncio.sleep(0.2)

    if new_tids:
        subscribe_orderbook_bulk(new_tids)

    return count, len(new_tids)


async def run_market_cache() -> None:
    """Background task: refresh the market cache every 30 minutes."""
    async with aiohttp.ClientSession() as session:
        while True:
            n, s = await _refresh(session)
            print(f"[market_cache] cached {n} active markets | pre-subscribed {s} new token books")
            await asyncio.sleep(REFRESH_INTERVAL)

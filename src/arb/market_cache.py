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
from datetime import datetime, timezone

import aiohttp

from src.arb.clob_ws import subscribe_orderbook

CLOB_API         = "https://clob.polymarket.com"
REFRESH_INTERVAL = 1800  # 30 minutes
PRESUB_HOURS     = 6     # subscribe to books of markets ending within this window

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
    now = datetime.now(timezone.utc)
    count = 0
    new_subs = 0

    if markets:
        sample = markets[0]
        date_fields = {k: v for k, v in sample.items() if "date" in k.lower() or "time" in k.lower() or "end" in k.lower()}
        print(f"[market_cache] date fields in API: {date_fields}")

    for m in markets:
        cid = m.get("condition_id")
        if not cid:
            continue

        _cache[cid] = {
            "tokens":   m.get("tokens", []),
            "question": m.get("question", cid[:20]),
        }
        count += 1

        # Pre-subscribe to both token books for markets ending within PRESUB_HOURS
        end_date_str = m.get("end_date_iso") or m.get("game_start_time")
        if not end_date_str:
            continue
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            delta_h = (end_dt - now).total_seconds() / 3600
            if 0 < delta_h <= PRESUB_HOURS:
                for token in m.get("tokens", []):
                    tid = token.get("token_id")
                    if tid and tid not in _subscribed:
                        await subscribe_orderbook(tid)
                        _subscribed.add(tid)
                        new_subs += 1
        except Exception:
            pass

    return count, new_subs


async def run_market_cache() -> None:
    """Background task: refresh the market cache every 30 minutes."""
    async with aiohttp.ClientSession() as session:
        while True:
            n, s = await _refresh(session)
            print(f"[market_cache] cached {n} active markets | pre-subscribed {s} new token books")
            await asyncio.sleep(REFRESH_INTERVAL)

"""
Pre-caches active market metadata (token_ids, question, outcome names).

Refreshes every 30 minutes in the background.
Used by arb_scanner to skip the market HTTP fetch at resolution time (~50ms saved).

Cache hit  → token_id available instantly, no HTTP call needed.
Cache miss → falls back to HTTP fetch (no worse than current behavior).

WS orderbook subscriptions are NOT done here — arb_scanner subscribes the
winning token immediately at resolution time. Stale orders persist for hours,
so the ~1s WS fill delay is irrelevant. Pre-subscribing 27k+ tokens was
causing RAM saturation (628MB+ with swap thrashing).
"""

import asyncio
import json

import aiohttp

GAMMA_API        = "https://gamma-api.polymarket.com"
CLOB_API         = "https://clob.polymarket.com"
REFRESH_INTERVAL = 1800  # 30 minutes

# condition_id → {"tokens": [{"token_id": ..., "outcome": ...}, ...], "question": ...}
_cache: dict[str, dict] = {}


def get_cached_market(condition_id: str) -> dict | None:
    """Return cached market metadata or None if not cached."""
    return _cache.get(condition_id)


async def _refresh(session: aiohttp.ClientSession) -> int:
    """
    Fetch active markets from Gamma API and populate metadata cache.
    Returns cached_count.
    """
    count  = 0
    offset = 0

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

            token_ids: list[str] = [t.get("token_id") or t.get("tokenId", "") for t in tokens]
            if not any(token_ids):
                token_ids = [str(t) for t in clob_ids if t]

            _cache[cid] = {
                "tokens":   [{"token_id": t.get("token_id") or t.get("tokenId", ""), "outcome": t.get("outcome", "")} for t in tokens],
                "question": m.get("question", cid[:20]),
            }
            count += 1

        if len(data) < 100:
            break
        offset += 100
        await asyncio.sleep(0.2)

    return count


async def run_market_cache() -> None:
    """Background task: refresh the market cache every 30 minutes."""
    async with aiohttp.ClientSession() as session:
        while True:
            n = await _refresh(session)
            print(f"[market_cache] cached {n} active markets")
            await asyncio.sleep(REFRESH_INTERVAL)

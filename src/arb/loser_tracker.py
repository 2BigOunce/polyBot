"""
Live tracker for known loser wallets — "desaru-style" sniping strategy.

Reads `watchlist_losers.txt` and polls each wallet's recent activity via
data-api. When a new trade is detected on one of these wallets:
  1. Fetch /book for the traded token.
  2. If crossed (best_bid > best_ask + MIN_SPREAD), queue a high-conviction
     mispricing event — skip REST validation since the signal source is
     already strong (a known defective bot just traded here).

Loads a live watchlist file — the tracker re-reads it every N seconds so
the BFS expansion can extend coverage without a restart.
"""

from __future__ import annotations

import asyncio
import os
import time
import urllib.parse

import aiohttp

from config.settings import (
    CLOB_MISPRICING_BID_RATIO,
    CLOB_MISPRICING_MIN_BID_DEPTH,
    CLOB_MISPRICING_MIN_SPREAD,
)

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
WATCHLIST_PATH = "watchlist_losers.txt"

POLL_INTERVAL_S       = 2.0    # per-wallet poll interval
WATCHLIST_RELOAD_S    = 300.0  # reload watchlist file every 5 min
SEEN_TX_MAX           = 200    # per-wallet cap on dedup history
MAX_CONCURRENT_POLLS  = 30     # safety cap


def _load_watchlist(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [line.strip().lower() for line in f if line.strip().startswith("0x")]


async def _fetch_json(session: aiohttp.ClientSession, url: str, timeout_s: float = 3.0):
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as r:
        if r.status != 200:
            return None
        return await r.json()


async def _check_book_and_queue(
    session: aiohttp.ClientSession, wallet: str, token_id: str, trade: dict
) -> None:
    """
    Immediate check: fetch /book for token_id. If crossed, queue a mispricing
    event. Also subscribe this token to lite WS tracking so any SUBSEQUENT
    stale ask from the same (or another) loser on this token is caught
    instantly via WS deltas, no REST needed.
    """
    # Subscribe to lite tracking for future deltas on this token
    from src.arb.clob_ws import subscribe_lite
    if subscribe_lite(token_id):
        print(f"[loser_tracker] + lite sub {token_id[:16]}... (seen via {wallet[:10]})")

    url = f"{CLOB_API}/book?token_id={token_id}"
    data = await _fetch_json(session, url, timeout_s=2.5)
    if not data:
        return
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    if not bids or not asks:
        return

    top_bid = max(
        (b for b in bids if float(b.get("size", 0)) > 0),
        key=lambda b: float(b["price"]), default=None,
    )
    cheapest_ask = min(
        (a for a in asks if float(a.get("size", 0)) > 0),
        key=lambda a: float(a["price"]), default=None,
    )
    if not top_bid or not cheapest_ask:
        return
    best_bid      = float(top_bid["price"])
    best_bid_size = float(top_bid.get("size", 0))
    ask_price     = float(cheapest_ask["price"])
    ask_size      = float(cheapest_ask.get("size", 0))

    bid_depth = best_bid * best_bid_size
    crossed = (
        best_bid > 0.05
        and ask_size > 0
        and ask_price < best_bid * CLOB_MISPRICING_BID_RATIO
        and ask_price < best_bid - CLOB_MISPRICING_MIN_SPREAD
        and bid_depth >= CLOB_MISPRICING_MIN_BID_DEPTH
    )
    if not crossed:
        return

    # Import here to avoid circular import at module load
    from src.arb.clob_ws import get_mispricing_queue
    t_detect = time.monotonic()
    get_mispricing_queue().put_nowait({
        "token_id":  token_id,
        "ask_price": ask_price,
        "ask_size":  ask_size,
        "best_bid":  best_bid,
        "t_detect":  t_detect,
        "t_queued":  t_detect,
        "skipped_rest": True,   # source already high-confidence
        "source":    f"loser_tracker:{wallet[:10]}",
    })
    print(
        f"[loser_tracker] ⚡ {wallet[:10]}... traded a crossed book — queued "
        f"token={token_id[:16]}... ask={ask_price:.3f} bid={best_bid:.3f} "
        f"spread={best_bid - ask_price:.3f}"
    )


async def _poll_wallet(
    session: aiohttp.ClientSession, wallet: str, seen_tx: set[str]
) -> None:
    """Single poll iteration for one wallet."""
    url = f"{DATA_API}/activity?" + urllib.parse.urlencode({
        "user": wallet,
        "limit": "5",
        "type":  "TRADE",
    })
    try:
        trades = await _fetch_json(session, url, timeout_s=3.0)
    except Exception:
        return
    if not trades:
        return

    for t in trades:
        tx = t.get("transactionHash")
        if not tx or tx in seen_tx:
            continue
        seen_tx.add(tx)
        token_id = t.get("asset")
        if not token_id:
            continue
        # Fire-and-forget book check; don't block the poll loop
        asyncio.create_task(_check_book_and_queue(session, wallet, token_id, t))

    # Trim seen_tx to cap
    if len(seen_tx) > SEEN_TX_MAX:
        # cheap trim: keep last half (arbitrary order but good enough)
        seen_tx.intersection_update(set(list(seen_tx)[-SEEN_TX_MAX // 2:]))


async def _bootstrap_seen(
    session: aiohttp.ClientSession, wallet: str
) -> tuple[set[str], set[str]]:
    """
    Fetch recent trades once to pre-populate dedup (avoid firing on history)
    AND return the set of recent token_ids this wallet has been trading on.
    The token set feeds preemptive lite subscriptions — so the VERY FIRST
    future stale ask from this wallet is caught via WS deltas without waiting
    for a new trade.
    """
    url = f"{DATA_API}/activity?" + urllib.parse.urlencode({
        "user": wallet,
        "limit": "50",
        "type":  "TRADE",
    })
    try:
        trades = await _fetch_json(session, url, timeout_s=5.0)
    except Exception:
        return set(), set()
    trades = trades or []
    seen_tx = {t["transactionHash"] for t in trades if t.get("transactionHash")}
    tokens  = {t["asset"] for t in trades if t.get("asset")}
    return seen_tx, tokens


async def _run_one(session: aiohttp.ClientSession, wallet: str) -> None:
    seen_tx, recent_tokens = await _bootstrap_seen(session, wallet)

    # Preemptive WS lite subscription on tokens this loser frequents.
    # Most will already be in lite tracking (market_cache covers active markets);
    # only truly new ones trigger a queue add (one shard restart cycle coalesces them).
    from src.arb.clob_ws import subscribe_lite
    added = sum(1 for t in recent_tokens if subscribe_lite(t))

    print(f"[loser_tracker] watching {wallet} "
          f"(seen_tx={len(seen_tx)} tokens={len(recent_tokens)} newly_lite={added})")

    while True:
        try:
            await _poll_wallet(session, wallet, seen_tx)
        except Exception as e:
            print(f"[loser_tracker] {wallet[:10]}... poll error: {e}")
        await asyncio.sleep(POLL_INTERVAL_S)


async def run_loser_tracker() -> None:
    """Main coroutine: manages per-wallet pollers, reloads watchlist periodically."""
    wallets = _load_watchlist(WATCHLIST_PATH)
    if not wallets:
        print(f"[loser_tracker] ⚠ {WATCHLIST_PATH} empty or missing — disabled")
        return
    print(f"[loser_tracker] starting with {len(wallets)} wallets from {WATCHLIST_PATH}")

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_POLLS)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks: dict[str, asyncio.Task] = {}
        for w in wallets:
            tasks[w] = asyncio.create_task(_run_one(session, w))

        while True:
            await asyncio.sleep(WATCHLIST_RELOAD_S)
            new_list = _load_watchlist(WATCHLIST_PATH)
            if not new_list:
                continue
            new_set = set(new_list)
            cur_set = set(tasks.keys())
            added = new_set - cur_set
            removed = cur_set - new_set
            if added:
                print(f"[loser_tracker] adding {len(added)} new wallet(s) from watchlist")
                for w in added:
                    tasks[w] = asyncio.create_task(_run_one(session, w))
            if removed:
                print(f"[loser_tracker] removing {len(removed)} wallet(s) from watchlist")
                for w in removed:
                    tasks[w].cancel()
                    tasks.pop(w, None)

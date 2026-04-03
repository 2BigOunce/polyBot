"""
CLOB WebSocket client for real-time market status push + in-memory orderbook cache.

Two responsibilities:
  1. Fire asyncio Events when markets transition to closed=true (replaces HTTP polling).
  2. Maintain live orderbook snapshots in memory via WS subscriptions.
     When a resolution is detected on-chain, arb_scanner subscribes immediately.
     The ~1-5s CLOB processing delay is used productively to build the book in RAM.
     By the time the market is confirmed closed, the book is already there — 0ms fetch.
"""

import asyncio
import csv
import os
import time

import orjson
import websockets

from config.settings import (
    ARB_MAX_STALE_PRICE,
    CLOB_MISPRICING_BID_RATIO,
    CLOB_MISPRICING_MIN_SPREAD,
    MAKER_SANDWICH_MAX_ASK,
    MAKER_SANDWICH_MIN_BID,
    MAKER_SANDWICH_MIN_PROFIT,
)

CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# condition_id → asyncio.Event (set when CLOB confirms market is closed)
_closed_events: dict[str, asyncio.Event] = {}

# token_id → {"asks": [...], "bids": [...]}  — live orderbook snapshots
_books: dict[str, dict] = {}

# token_id → pre-computed stale asks (filtered + sorted, ready to use at resolution)
_stale: dict[str, list[dict]] = {}

# Queue of token_ids waiting to be subscribed on the WS
_subscription_queue: asyncio.Queue[str] = asyncio.Queue()

# Queue of mispricing events for the clob_mispricing consumer
_mispricing_queue: asyncio.Queue[dict] = asyncio.Queue()

# Cooldown: avoid re-queuing the same (token_id, price) within N seconds
_mispricing_cooldown: dict[tuple[str, str], float] = {}
_MISPRICING_COOLDOWN_S = 30.0

# Sandwich arb queue: ask << bid on same token → buy ask + sell bid immediately
_sandwich_queue: asyncio.Queue[dict] = asyncio.Queue()
_sandwich_cooldown: dict[str, float] = {}
_SANDWICH_COOLDOWN_S = 60.0

# Spread observation log — records all extreme spreads seen for calibration
_SPREAD_OBS_FILE = "logs/spread_observations.csv"
_spread_obs_cooldown: dict[str, float] = {}
_SPREAD_OBS_COOLDOWN_S = 10.0   # log same token at most every 10s
_SPREAD_OBS_MIN_ASK   = 0.20    # same as sandwich threshold — log confirmed opportunities
_SPREAD_OBS_MIN_BID   = 0.85

def _log_spread(token_id: str, ask_price: float, bid_price: float, ask_size: float) -> None:
    """Append a spread observation to the CSV log file."""
    os.makedirs("logs", exist_ok=True)
    write_header = not os.path.exists(_SPREAD_OBS_FILE)
    with open(_SPREAD_OBS_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["timestamp", "token_id", "ask_price", "bid_price", "spread", "ask_size"])
        w.writerow([
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            token_id,
            round(ask_price, 4),
            round(bid_price, 4),
            round(bid_price - ask_price, 4),
            round(ask_size, 2),
        ])

# Bid watchers: token_id → (min_bid, asyncio.Event)
# Registered by maker_positioning after a fill; fired when best_bid >= min_bid.
_bid_watchers: dict[str, tuple[float, asyncio.Event]] = {}


def register_bid_watcher(token_id: str, min_bid: float) -> asyncio.Event:
    """
    Register interest in a high bid on token_id.
    Returns an Event that will be set when best_bid >= min_bid.
    If the book already has a qualifying bid, the Event is set immediately.
    """
    event = asyncio.Event()
    _bid_watchers[token_id] = (min_bid, event)
    # Fire immediately if a qualifying bid already exists in the cached book
    book = _books.get(token_id)
    if book:
        bids = book.get("bids", [])
        if bids and float(bids[0]["price"]) >= min_bid:
            event.set()
    return event


def unregister_bid_watcher(token_id: str) -> None:
    """Remove the bid watcher for a token (call after selling or at resolution).
    Sets the event to unblock any task waiting on event.wait()."""
    entry = _bid_watchers.pop(token_id, None)
    if entry:
        _, event = entry
        event.set()  # wake up _watch_bid_and_sell so it can exit cleanly


# Diagnostic counters
_book_update_count = 0
_BOOK_LOG_INTERVAL = 100
_total_messages = 0
_MSG_LOG_INTERVAL = 200


def get_mispricing_queue() -> asyncio.Queue:
    return _mispricing_queue


def get_sandwich_queue() -> asyncio.Queue:
    return _sandwich_queue


def get_closed_event(condition_id: str) -> asyncio.Event:
    """
    Register interest in a market closing.
    Returns an Event that will be set when the WS receives closed=true.
    Call BEFORE awaiting — so the event is in place when the push arrives.
    """
    if condition_id not in _closed_events:
        _closed_events[condition_id] = asyncio.Event()
    return _closed_events[condition_id]


async def subscribe_orderbook(token_id: str) -> None:
    """
    Request a live orderbook subscription for a token_id.
    The subscription is sent to the WS as soon as the sender task picks it up.
    Call this immediately after detecting an on-chain resolution to use the
    CLOB processing delay (~1-5s) to pre-fill the in-memory book.
    """
    _subscription_queue.put_nowait(token_id)


def subscribe_orderbook_bulk(token_ids: list[str]) -> None:
    """
    Queue multiple token_ids for orderbook subscription without yielding.
    Use this from market_cache to fill the queue in one shot so the sender
    can batch them efficiently.
    """
    for tid in token_ids:
        _subscription_queue.put_nowait(tid)


def get_cached_book(token_id: str) -> dict | None:
    """
    Return the latest in-memory orderbook snapshot for this token, or None.
    After calling subscribe_orderbook() and waiting ~1s, this should be populated.
    """
    return _books.get(token_id)


def get_cached_stale(token_id: str) -> list[dict] | None:
    """
    Return pre-computed stale asks (filtered + sorted) for this token, or None.
    Updated on every WS book event — zero computation at resolution time.
    """
    return _stale.get(token_id)


def _run_detection(token_id: str, asks: list, bids: list) -> None:
    """Run sandwich / mispricing / spread detection on a fresh book state."""
    if not bids or not asks:
        return

    best_bid     = float(bids[0]["price"])
    cheapest_ask = min(asks, key=lambda a: float(a["price"]))
    ask_price    = float(cheapest_ask["price"])
    ask_size     = float(cheapest_ask.get("size", 0))

    # Mispricing detection
    if (
        best_bid > 0.05
        and ask_size > 0
        and ask_price < best_bid * CLOB_MISPRICING_BID_RATIO
        and ask_price < best_bid - CLOB_MISPRICING_MIN_SPREAD
    ):
        key = (token_id, f"{ask_price:.4f}")
        now = time.monotonic()
        if now - _mispricing_cooldown.get(key, 0.0) >= _MISPRICING_COOLDOWN_S:
            _mispricing_cooldown[key] = now
            _mispricing_queue.put_nowait({
                "token_id":  token_id,
                "ask_price": ask_price,
                "ask_size":  ask_size,
                "best_bid":  best_bid,
            })

    # Spread observation logging (wider thresholds — for calibration)
    if ask_price <= _SPREAD_OBS_MIN_ASK and best_bid >= _SPREAD_OBS_MIN_BID:
        now = time.monotonic()
        if now - _spread_obs_cooldown.get(token_id, 0.0) >= _SPREAD_OBS_COOLDOWN_S:
            _spread_obs_cooldown[token_id] = now
            _log_spread(token_id, ask_price, best_bid, ask_size)
            print(
                f"[arb_ws] 👁 spread obs ask={ask_price:.3f} bid={best_bid:.3f} "
                f"spread={best_bid-ask_price:.3f} | {token_id[:14]}…"
            )

    # Sandwich arb
    if (
        ask_price <= MAKER_SANDWICH_MAX_ASK
        and best_bid  >= MAKER_SANDWICH_MIN_BID
        and best_bid - ask_price >= MAKER_SANDWICH_MIN_PROFIT
        and ask_size > 0
    ):
        now = time.monotonic()
        if now - _sandwich_cooldown.get(token_id, 0.0) >= _SANDWICH_COOLDOWN_S:
            _sandwich_cooldown[token_id] = now
            _sandwich_queue.put_nowait({
                "token_id":  token_id,
                "ask_price": ask_price,
                "ask_size":  ask_size,
                "bid_price": best_bid,
                "detected_at": time.monotonic(),
            })


def _store_book(token_id: str, asks: list, bids: list) -> None:
    """Replace cached book, recompute stale asks, trigger detection."""
    global _book_update_count
    _book_update_count += 1
    if _book_update_count % _BOOK_LOG_INTERVAL == 0:
        # Show distribution: how many books have high bids / low asks
        high_bids = sum(1 for b in _books.values() if b["bids"] and float(b["bids"][0]["price"]) >= 0.70)
        low_asks  = sum(1 for b in _books.values() if b["asks"] and float(min(b["asks"], key=lambda a: float(a["price"]))["price"]) <= 0.30)
        crossed   = sum(1 for b in _books.values() if b["bids"] and b["asks"] and float(b["bids"][0]["price"]) >= 0.80 and float(min(b["asks"], key=lambda a: float(a["price"]))["price"]) <= 0.20)
        print(f"[arb_ws] 📊 {_book_update_count} book updates | {len(_books)} books | bid≥0.70: {high_bids} | ask≤0.30: {low_asks} | crossed(0.80/0.20): {crossed}")

    _books[token_id] = {"asks": asks, "bids": bids}
    _stale[token_id] = sorted(
        [a for a in asks if float(a.get("price", 1.0)) <= ARB_MAX_STALE_PRICE],
        key=lambda a: float(a["price"]),
    )

    # Bid watcher
    if token_id in _bid_watchers:
        min_bid, event = _bid_watchers[token_id]
        if not event.is_set() and bids and float(bids[0]["price"]) >= min_bid:
            event.set()

    _run_detection(token_id, asks, bids)


def _apply_side_update(token_id: str, side: str, updates: list[dict]) -> None:
    """
    Apply incremental updates to one side of the cached book.
    The WS sends partial messages like {asset_id, bids: [{price, size}]}
    where each entry is a level to add/update (size>0) or remove (size=0).
    """
    book = _books.get(token_id)
    if book is None:
        return  # No snapshot yet — store first, then apply deltas

    level_map = {e["price"]: e for e in book[side]}
    for entry in updates:
        price = entry.get("price", "")
        if not price:
            continue
        if float(entry.get("size", 0)) == 0:
            level_map.pop(price, None)
        else:
            level_map[price] = entry

    global _book_update_count
    _book_update_count += 1
    if _book_update_count % _BOOK_LOG_INTERVAL == 0:
        print(f"[arb_ws] 📊 {_book_update_count} book updates | {len(_books)} books in memory")

    if side == "bids":
        updated = sorted(level_map.values(), key=lambda x: float(x["price"]), reverse=True)
        _books[token_id]["bids"] = updated
        # Bid watcher
        if token_id in _bid_watchers:
            min_bid, event = _bid_watchers[token_id]
            if not event.is_set() and updated and float(updated[0]["price"]) >= min_bid:
                event.set()
        _run_detection(token_id, book["asks"], updated)
    else:
        updated = sorted(level_map.values(), key=lambda x: float(x["price"]))
        _books[token_id]["asks"] = updated
        _stale[token_id] = sorted(
            [a for a in updated if float(a.get("price", 1.0)) <= ARB_MAX_STALE_PRICE],
            key=lambda a: float(a["price"]),
        )
        _run_detection(token_id, updated, book["bids"])


def _handle_message(msg: dict | list) -> None:
    """Process a WS message. Handles both list (batch) and dict (single) formats."""
    if isinstance(msg, list):
        for item in msg:
            _handle_message(item)
        return

    if not isinstance(msg, dict):
        return

    global _total_messages
    _total_messages += 1
    if _total_messages % _MSG_LOG_INTERVAL == 0:
        print(f"[arb_ws] 📨 {_total_messages} msgs | books: {len(_books)}")

    # --- Market closure — signal waiting scanners ---
    if msg.get("closed"):
        condition_id = (
            msg.get("condition_id") or msg.get("conditionId")
            or msg.get("market_id") or msg.get("market")
        )
        if condition_id:
            event = _closed_events.get(condition_id)
            if event and not event.is_set():
                print(f"[arb_ws] ✅ closed via WS push: {condition_id[:14]}…")
                event.set()
        return

    asset_id = msg.get("asset_id")

    # --- Book update: full snapshot OR partial side update ---
    if asset_id and ("bids" in msg or "asks" in msg):
        has_bids = "bids" in msg
        has_asks = "asks" in msg
        if has_bids and has_asks:
            # Full snapshot: replace both sides entirely
            _store_book(asset_id, msg["asks"], msg["bids"])
        elif has_bids:
            _apply_side_update(asset_id, "bids", msg["bids"])
        else:
            _apply_side_update(asset_id, "asks", msg["asks"])
        return

    # --- price_changes: contains asset_id + price only (no side/size) — skip delta,
    #     rely on the full book message that follows in the same burst ---

    # --- Best bid/ask tick: re-detect from cached book ---
    if asset_id and ("best_bid" in msg or "best_ask" in msg):
        book = _books.get(asset_id)
        if book:
            _run_detection(asset_id, book["asks"], book["bids"])
        return


async def _run_market_status_ws() -> None:
    """
    WS connection #1: subscribes to market closure events only (assets_ids=[]).
    One message per connection — no additional subscriptions sent.
    """
    retry_delay = 5
    while True:
        try:
            async with websockets.connect(
                CLOB_WS, ping_interval=20, ping_timeout=10, open_timeout=15,
            ) as ws:
                retry_delay = 5
                await ws.send(orjson.dumps({
                    "type": "market",
                    "assets_ids": [],
                    "custom_feature_enabled": True,
                }).decode())
                print("[arb_ws] Connected — listening for market closures...")
                async for raw in ws:
                    if not raw:
                        continue
                    try:
                        _handle_message(orjson.loads(raw))
                    except (orjson.JSONDecodeError, ValueError):
                        pass
                    except Exception as e:
                        print(f"[arb_ws] status error: {e}")
        except Exception as e:
            print(f"[arb_ws] status WS lost: {e}. Retrying in {retry_delay}s…")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


async def _run_orderbook_ws() -> None:
    """
    WS connection #2: subscribes to orderbook updates.
    Waits for tokens to be queued, then opens a NEW connection per batch
    (Polymarket only allows one subscription message per connection).
    On reconnect, re-subscribes all known tokens in one message.
    """
    retry_delay = 5

    # Collect all tokens queued so far before connecting
    while True:
        # Wait until we have at least one token to subscribe to
        token_id = await _subscription_queue.get()
        batch = [token_id]
        # Drain everything already in the queue
        while True:
            try:
                batch.append(_subscription_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        # Also wait a short moment for any stragglers (market_cache bulk adds)
        await asyncio.sleep(0.5)
        while True:
            try:
                batch.append(_subscription_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        # Remember all subscribed tokens for reconnect
        for tid in batch:
            _subscribed_tokens.add(tid)

        print(f"[arb_ws] 📖 orderbook WS — subscribing {len(_subscribed_tokens)} tokens…")

        # Open a fresh connection with all tokens in the FIRST (and only) message
        try:
            async with websockets.connect(
                CLOB_WS, ping_interval=20, ping_timeout=10, open_timeout=15,
            ) as ws:
                retry_delay = 5
                await ws.send(orjson.dumps({
                    "type": "market",
                    "assets_ids": list(_subscribed_tokens),
                    "custom_feature_enabled": True,
                }).decode())
                print(f"[arb_ws] 📖 orderbook WS connected | {len(_subscribed_tokens)} tokens")
                _raw_count = 0
                _last_queue_check = time.monotonic()
                async for raw in ws:
                    if not raw:
                        continue
                    _raw_count += 1
                    if _raw_count <= 100 or _raw_count % 500 == 0:
                        preview = raw[:200] if isinstance(raw, str) else repr(raw[:100])
                        print(f"[arb_ws] BOOK RAW[{_raw_count}]: {preview}")
                    try:
                        _handle_message(orjson.loads(raw))
                    except (orjson.JSONDecodeError, ValueError):
                        pass
                    except Exception as e:
                        print(f"[arb_ws] book error: {e}")
                    # Check every 30s if market_cache added new tokens — reconnect if so
                    now = time.monotonic()
                    if now - _last_queue_check >= 30 and not _subscription_queue.empty():
                        _last_queue_check = now
                        pending = _subscription_queue.qsize()
                        print(f"[arb_ws] 🔄 {pending} new tokens queued — reconnecting to include them")
                        break  # Exit inner loop → outer loop reconnects with full list
        except Exception as e:
            print(f"[arb_ws] orderbook WS lost: {e}. Retrying in {retry_delay}s…")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
            # Re-queue all tokens so the outer loop reconnects with full list
            for tid in _subscribed_tokens:
                _subscription_queue.put_nowait(tid)


# Tracks all token_ids ever subscribed — used to re-subscribe on reconnect
_subscribed_tokens: set[str] = set()


async def run_clob_ws() -> None:
    """
    Run both WS connections concurrently:
    - Market status WS (closures)
    - Orderbook WS (book updates for sandwich/mispricing detection)
    """
    await asyncio.gather(
        _run_market_status_ws(),
        _run_orderbook_ws(),
    )

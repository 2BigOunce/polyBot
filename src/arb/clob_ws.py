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
import json
import time

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


# Diagnostic counter — logs book update rate every N updates
_book_update_count = 0
_BOOK_LOG_INTERVAL = 500

# Debug: track all event_types seen
_seen_event_types: dict[str, int] = {}
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
    await _subscription_queue.put(token_id)


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
    event_type = msg.get("event_type", "")
    _seen_event_types[event_type] = _seen_event_types.get(event_type, 0) + 1
    if _total_messages % _MSG_LOG_INTERVAL == 0:
        print(f"[arb_ws] 📨 {_total_messages} msgs | types: {dict(_seen_event_types)}")

    # Orderbook snapshot — store in memory + pre-compute stale asks + detect mispricings
    if event_type == "book":
        global _book_update_count
        _book_update_count += 1
        if _book_update_count % _BOOK_LOG_INTERVAL == 0:
            print(f"[arb_ws] 📊 {_book_update_count} book updates received | {len(_books)} books in memory")
        token_id = msg.get("asset_id")
        if token_id:
            asks = msg.get("asks", [])
            bids = msg.get("bids", [])
            _books[token_id] = {"asks": asks, "bids": bids}
            _stale[token_id] = sorted(
                [a for a in asks if float(a.get("price", 1.0)) <= ARB_MAX_STALE_PRICE],
                key=lambda a: float(a["price"]),
            )

            # Bid watcher: notify maker_positioning when a qualifying bid appears
            if token_id in _bid_watchers:
                min_bid, event = _bid_watchers[token_id]
                if not event.is_set() and bids and float(bids[0]["price"]) >= min_bid:
                    event.set()

            # Mispricing detection: cheapest ask well below the best bid
            if bids and asks:
                best_bid = float(bids[0]["price"])
                cheapest_ask = min(asks, key=lambda a: float(a["price"]))
                ask_price = float(cheapest_ask["price"])
                ask_size  = float(cheapest_ask.get("size", 0))

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

                # Sandwich arb: extreme spread — buy ask taker + sell bid taker immediately
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
                        })

        return

    # Market closure — signal waiting scanners
    closed = msg.get("closed", False)
    if not closed:
        return

    condition_id = (
        msg.get("condition_id")
        or msg.get("conditionId")
        or msg.get("market_id")
        or msg.get("market")
    )
    if not condition_id:
        return

    event = _closed_events.get(condition_id)
    if event and not event.is_set():
        print(f"[arb_ws] ✅ closed via WS push: {condition_id[:14]}…")
        event.set()


async def _sender(ws) -> None:
    """Consume the subscription queue and send orderbook subscription messages."""
    # Small initial delay to let the market-status subscription be processed first
    await asyncio.sleep(0.5)
    while True:
        token_id = await _subscription_queue.get()
        try:
            await ws.send(json.dumps({
                "auth": {},
                "type": "Market",
                "assets_ids": [token_id],
                "markets": [],
            }))
            print(f"[arb_ws] 📖 subscribed orderbook: {token_id[:14]}…")
            # Rate-limit: ~20 subscriptions/second to avoid INVALID OPERATION
            await asyncio.sleep(0.05)
        except Exception as e:
            print(f"[arb_ws] subscription send failed: {e}")
            # Re-queue so the next connection attempt picks it up
            await _subscription_queue.put(token_id)
            return


async def run_clob_ws() -> None:
    """
    Maintain a persistent WS connection to the Polymarket CLOB.
    Spawns a sender task for dynamic subscriptions.
    Run via asyncio.gather() in main.py.
    """
    retry_delay = 5

    while True:
        try:
            async with websockets.connect(
                CLOB_WS,
                ping_interval=20,
                ping_timeout=10,
                open_timeout=15,
            ) as ws:
                retry_delay = 5

                # Subscribe to all market status events
                await ws.send(json.dumps({
                    "auth": {},
                    "type": "Market",
                    "markets": [],
                    "assets_ids": [],
                }))

                print("[arb_ws] Connected — listening for market closures...")

                # Sender task handles dynamic orderbook subscriptions concurrently
                sender = asyncio.create_task(_sender(ws))
                _raw_count = 0
                try:
                    async for raw in ws:
                        if not raw:
                            continue
                        _raw_count += 1
                        if _raw_count <= 20:
                            preview = raw[:300] if isinstance(raw, str) else repr(raw[:100])
                            print(f"[arb_ws] RAW[{_raw_count}]: {preview}")
                        try:
                            _handle_message(json.loads(raw))
                        except (json.JSONDecodeError, ValueError):
                            pass  # ACK / ping frames from CLOB WS are not JSON
                        except Exception as e:
                            print(f"[arb_ws] error: {e}")
                finally:
                    sender.cancel()

        except Exception as e:
            print(f"[arb_ws] Connection lost: {e}. Retrying in {retry_delay}s…")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

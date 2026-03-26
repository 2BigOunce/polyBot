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


def get_mispricing_queue() -> asyncio.Queue:
    return _mispricing_queue


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

    event_type = msg.get("event_type", "")

    # Orderbook snapshot — store in memory + pre-compute stale asks + detect mispricings
    if event_type == "book":
        token_id = msg.get("asset_id")
        if token_id:
            asks = msg.get("asks", [])
            bids = msg.get("bids", [])
            _books[token_id] = {"asks": asks, "bids": bids}
            _stale[token_id] = sorted(
                [a for a in asks if float(a.get("price", 1.0)) <= ARB_MAX_STALE_PRICE],
                key=lambda a: float(a["price"]),
            )

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
                try:
                    async for raw in ws:
                        if not raw:
                            continue
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

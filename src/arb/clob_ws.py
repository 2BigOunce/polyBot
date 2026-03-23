"""
CLOB WebSocket client for real-time market status push.

Maintains a persistent connection to wss://ws-subscriptions-clob.polymarket.com/ws/
and fires asyncio Events when markets transition to closed=true.

Used by arb_scanner.py to replace the HTTP polling retry loop with a push model,
reducing closed-detection latency from 0-300ms to ~20-50ms.
"""

import asyncio
import json

import websockets

CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# condition_id → asyncio.Event (set when CLOB confirms market is closed)
_closed_events: dict[str, asyncio.Event] = {}


def get_closed_event(condition_id: str) -> asyncio.Event:
    """
    Register interest in a market closing.
    Returns an Event that will be set when the WS receives closed=true for this market.
    Call this BEFORE waiting — so the event is in place when the WS message arrives.
    """
    if condition_id not in _closed_events:
        _closed_events[condition_id] = asyncio.Event()
    return _closed_events[condition_id]


def _handle_message(msg: dict | list) -> None:
    """
    Process a WS message. Handles both list (batch) and dict (single) formats.
    Looks for any field combination that indicates closed=true for a known market.
    """
    if isinstance(msg, list):
        for item in msg:
            _handle_message(item)
        return

    if not isinstance(msg, dict):
        return

    closed = msg.get("closed", False)
    if not closed:
        return

    # Try common field names for condition_id across Polymarket API versions
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


async def run_clob_ws() -> None:
    """
    Maintain a persistent WS connection to the Polymarket CLOB.
    Subscribes to all market status events and signals waiting arb scanners.
    Run via asyncio.gather() in main.py.
    """
    retry_delay = 5

    while True:
        try:
            async with websockets.connect(
                CLOB_WS,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                retry_delay = 5

                # Subscribe to all market events (empty lists = all markets)
                await ws.send(json.dumps({
                    "auth": {},
                    "type": "Market",
                    "markets": [],
                    "assets_ids": [],
                }))

                print("[arb_ws] Connected — listening for market closures...")

                async for raw in ws:
                    try:
                        _handle_message(json.loads(raw))
                    except Exception as e:
                        print(f"[arb_ws] parse error: {e}")

        except Exception as e:
            print(f"[arb_ws] Connection lost: {e}. Retrying in {retry_delay}s…")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

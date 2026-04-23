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
import random
import time
import urllib.request

import orjson
import websockets

from config.settings import (
    ARB_MAX_STALE_PRICE,
    CLOB_MISPRICING_BID_RATIO,
    CLOB_MISPRICING_MIN_BID_DEPTH,
    CLOB_MISPRICING_MIN_SPREAD,
    # MAKER_SANDWICH_MAX_ASK,   # sandwich arb disabled — crossed books don't exist in practice
    # MAKER_SANDWICH_MIN_BID,   # (desaru/notprivate confirmed: they do stale arb post-resolution)
    # MAKER_SANDWICH_MIN_PROFIT,
)

CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# High-conviction threshold: if absolute spread (best_bid - ask_price) is this
# wide or wider, skip REST validation and queue immediately. Such a crossed book
# is statistically extremely unlikely to be a stale-cache false positive, and
# the ~350ms we save is the difference between catching the stale ask and
# losing it to a faster taker.
HIGH_CONVICTION_SPREAD = 0.25

# condition_id → asyncio.Event (set when CLOB confirms market is closed)
_closed_events: dict[str, asyncio.Event] = {}

# token_id → {"asks": [...], "bids": [...]}  — live orderbook snapshots
_books: dict[str, dict] = {}

# token_id → pre-computed stale asks (filtered + sorted, ready to use at resolution)
_stale: dict[str, list[dict]] = {}

# token_id → list of pending (side, updates) deltas received before the first snapshot.
# Capped per-token to avoid unbounded growth if a subscription never returns a snapshot.
_pending_deltas: dict[str, list[tuple[str, list[dict]]]] = {}
_PENDING_DELTAS_CAP = 50

# Queue of token_ids waiting to be subscribed on the WS
_subscription_queue: asyncio.Queue[str] = asyncio.Queue()

# Queue of mispricing events for the clob_mispricing consumer
_mispricing_queue: asyncio.Queue[dict] = asyncio.Queue()

# Cooldown: avoid re-queuing the same (token_id, price) within N seconds
_mispricing_cooldown: dict[tuple[str, str], float] = {}
_MISPRICING_COOLDOWN_S = 30.0

# Lightweight tracking for open-market mispricing (all active markets from market_cache).
# Stores top-5 bids + top-5 asks (symmetric, both sides can go up and down via deltas).
# token_id → {"bids": [{"price": str, "size": str}, ...], "asks": [...]}
_price_signals: dict[str, dict] = {}
_lite_tokens: set[str] = set()  # tokens using lite tracking (not full book)

# ---------------------------------------------------------------------------
# SANDWICH ARB — disabled (2026-04-04)
# Hypothesis was: detect crossed orderbook (ask << bid on same token) via WS,
# buy the ask and immediately sell into the bid.
# Reality: CLOB matching engine pairs these orders instantly — crossed books
# never persist long enough to catch via WS polling.
# Confirmed by on-chain analysis of desaru/notprivate: they do stale arb
# HOURS after resolution, not real-time crossed-book arb.
# Code kept for reference in case market structure changes.
#
# _sandwich_queue: asyncio.Queue[dict] = asyncio.Queue()
# _sandwich_cooldown: dict[str, float] = {}
# _SANDWICH_COOLDOWN_S = 60.0
#
# _SPREAD_OBS_FILE = "logs/spread_observations.csv"
# _spread_obs_cooldown: dict[str, float] = {}
# _SPREAD_OBS_COOLDOWN_S = 10.0
# _SPREAD_OBS_MIN_ASK   = 0.20
# _SPREAD_OBS_MIN_BID   = 0.85
#
# def _log_spread(token_id, ask_price, bid_price, ask_size): ...
# ---------------------------------------------------------------------------

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


# Diagnostic heartbeats — time-based throttling (at most once every HEARTBEAT_SECS)
# Count-based intervals flooded journalctl at ~200 msg/s; time floor keeps logs readable.
_book_update_count = 0
_total_messages = 0
_HEARTBEAT_SECS = 30.0
_last_msg_log = 0.0
_last_book_log = 0.0

# Mispricing funnel telemetry: where do candidates die?
_det_calls_lite = 0
_det_calls_full = 0
_det_bid_ok = 0
_det_ask_ok = 0
_det_ratio_ok = 0
_det_spread_ok = 0
_det_depth_ok = 0
_det_best_spread_pct = -999.0   # max (bid-ask)/bid seen (can be negative)
_det_worst_spread_pct = 999.0   # min (bid-ask)/bid seen
_det_best_depth = 0.0
_last_funnel_log = 0.0


def get_mispricing_queue() -> asyncio.Queue:
    return _mispricing_queue


# def get_sandwich_queue() -> asyncio.Queue:  # sandwich arb disabled
#     return _sandwich_queue


def get_closed_event(condition_id: str) -> asyncio.Event:
    """
    Register interest in a market closing.
    Returns an Event that will be set when the WS receives closed=true.
    Call BEFORE awaiting — so the event is in place when the push arrives.
    """
    if condition_id not in _closed_events:
        _closed_events[condition_id] = asyncio.Event()
    return _closed_events[condition_id]


def subscribe_mispricing_bulk(token_ids: list[str]) -> None:
    """
    Register token_ids for lightweight mispricing monitoring.
    Only stores best_bid + top-5 asks per token (not full orderbook).
    Called by market_cache on each refresh to cover all active markets.
    """
    new_tokens = [t for t in token_ids if t and t not in _lite_tokens and t not in _subscribed_tokens]
    if not new_tokens:
        return
    for t in new_tokens:
        _lite_tokens.add(t)
        _subscription_queue.put_nowait(t)
    print(f"[arb_ws] 📡 {len(new_tokens)} new tokens queued for lite mispricing tracking ({len(_lite_tokens)} total lite)")


def cleanup_lite_tokens(active_token_ids: set[str]) -> None:
    """
    Remove lite-tracked tokens that are no longer in the active market set.
    Called by market_cache after each refresh. Tokens in full tracking
    (_subscribed_tokens) are kept regardless — they may be mid-arb.
    """
    stale = _lite_tokens - active_token_ids - _subscribed_tokens
    if not stale:
        return
    for t in stale:
        _lite_tokens.discard(t)
        _price_signals.pop(t, None)
    print(f"[arb_ws] 🧹 cleaned {len(stale)} stale lite tokens ({len(_lite_tokens)} remaining)")


async def subscribe_orderbook(token_id: str) -> None:
    """
    Request a live orderbook subscription for a token_id.
    The subscription is sent to the WS as soon as the sender task picks it up.
    Call this immediately after detecting an on-chain resolution to use the
    CLOB processing delay (~1-5s) to pre-fill the in-memory book.
    Promotes a lite-tracked token to full book tracking.
    """
    _lite_tokens.discard(token_id)  # promote to full tracking if was lite
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


def get_lite_stale(token_id: str, max_price: float) -> list[dict] | None:
    """
    Return stale asks from lite cache (_price_signals) if available.
    Used at resolution time to attempt a BUY without any HTTP call or new WS subscription.
    Returns None if token is not lite-tracked or has no data yet.
    """
    sig = _price_signals.get(token_id)
    if not sig:
        return None
    stale = [a for a in sig.get("asks", []) if float(a["price"]) <= max_price]
    return sorted(stale, key=lambda a: float(a["price"])) or None


async def _validate_and_queue(token_id: str, ws_ask_price: float, ws_ask_size: float, ws_best_bid: float, t_detect: float) -> None:
    """
    Re-fetch /book via REST and confirm the crossed state before queuing.
    If REST confirms: queue with REST values.
    If REST contradicts: refresh lite cache to self-heal.
    """
    loop = asyncio.get_running_loop()

    def _fetch():
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read())

    t_rest_start = time.monotonic()
    try:
        data = await asyncio.wait_for(loop.run_in_executor(None, _fetch), timeout=4)
    except Exception as e:
        print(f"[mispricing] REST validation failed {token_id[:20]}: {e}")
        return
    t_rest_done = time.monotonic()

    rest_bids = data.get("bids", []) or []
    rest_asks = data.get("asks", []) or []
    if not rest_bids or not rest_asks:
        return

    rest_top_bid = max(
        (b for b in rest_bids if float(b.get("size", 0)) > 0),
        key=lambda b: float(b["price"]), default=None,
    )
    rest_cheapest_ask = min(
        (a for a in rest_asks if float(a.get("size", 0)) > 0),
        key=lambda a: float(a["price"]), default=None,
    )
    if not rest_top_bid or not rest_cheapest_ask:
        return

    rest_bid = float(rest_top_bid["price"])
    rest_bid_size = float(rest_top_bid.get("size", 0))
    rest_ask = float(rest_cheapest_ask["price"])
    rest_ask_size = float(rest_cheapest_ask.get("size", 0))

    still_crossed = (
        rest_bid > 0.05
        and rest_ask_size > 0
        and rest_ask < rest_bid * CLOB_MISPRICING_BID_RATIO
        and rest_ask < rest_bid - CLOB_MISPRICING_MIN_SPREAD
        and rest_bid * rest_bid_size >= CLOB_MISPRICING_MIN_BID_DEPTH
    )

    if still_crossed:
        t_queued = time.monotonic()
        _mispricing_queue.put_nowait({
            "token_id":  token_id,
            "ask_price": rest_ask,
            "ask_size":  rest_ask_size,
            "best_bid":  rest_bid,
            "t_detect":  t_detect,
            "t_rest_start": t_rest_start,
            "t_rest_done":  t_rest_done,
            "t_queued":     t_queued,
        })
        print(
            f"[mispricing] ✅ REST confirmed {token_id[:20]}... ask={rest_ask:.3f} bid={rest_bid:.3f} "
            f"| ⏱ detect→rest={1000*(t_rest_start-t_detect):.0f}ms rest={1000*(t_rest_done-t_rest_start):.0f}ms"
        )
    else:
        # WS lied — refresh cache with REST truth so next detection is accurate
        if token_id in _lite_tokens:
            _price_signals[token_id] = {
                "bids": sorted(rest_bids, key=lambda b: float(b["price"]), reverse=True)[:5],
                "asks": sorted(rest_asks, key=lambda a: float(a["price"]))[:5],
            }
        print(
            f"[mispricing] ⚠ REST contradicts {token_id[:20]}... "
            f"WS ask={ws_ask_price:.3f}/bid={ws_best_bid:.3f} "
            f"REST ask={rest_ask:.3f}/bid={rest_bid:.3f} — cache refreshed"
        )


def _run_detection(token_id: str, asks: list, bids: list) -> None:
    """Run sandwich / mispricing / spread detection on a fresh book state."""
    if not bids or not asks:
        return

    best_bid     = float(bids[0]["price"])
    best_bid_size = float(bids[0].get("size", 0))
    cheapest_ask = min(asks, key=lambda a: float(a["price"]))
    ask_price    = float(cheapest_ask["price"])
    ask_size     = float(cheapest_ask.get("size", 0))

    # Mispricing detection — require min bid depth (USDC) to filter phantom/spoof bids
    bid_depth_usdc = best_bid * best_bid_size
    global _det_calls_lite, _det_calls_full, _det_bid_ok, _det_ask_ok, _det_ratio_ok, _det_spread_ok, _det_depth_ok
    global _det_best_spread_pct, _det_worst_spread_pct, _det_best_depth, _last_funnel_log
    if token_id in _lite_tokens:
        _det_calls_lite += 1
    else:
        _det_calls_full += 1
    spread_pct = (best_bid - ask_price) / best_bid if best_bid > 0 else -1.0
    if spread_pct > _det_best_spread_pct:
        _det_best_spread_pct = spread_pct
    if spread_pct < _det_worst_spread_pct:
        _det_worst_spread_pct = spread_pct

    bid_cond   = best_bid > 0.05
    ask_cond   = bid_cond and ask_size > 0
    ratio_cond = ask_cond and ask_price < best_bid * CLOB_MISPRICING_BID_RATIO
    spread_cond = ratio_cond and ask_price < best_bid - CLOB_MISPRICING_MIN_SPREAD
    depth_cond  = spread_cond and bid_depth_usdc >= CLOB_MISPRICING_MIN_BID_DEPTH

    if bid_cond:   _det_bid_ok    += 1
    if ask_cond:   _det_ask_ok    += 1
    if ratio_cond: _det_ratio_ok  += 1
    if spread_cond:
        _det_spread_ok += 1
        if bid_depth_usdc > _det_best_depth:
            _det_best_depth = bid_depth_usdc

    if depth_cond:
        _det_depth_ok += 1
        t_detect = time.monotonic()
        if t_detect - _mispricing_cooldown.get(token_id, 0.0) >= _MISPRICING_COOLDOWN_S:
            _mispricing_cooldown[token_id] = t_detect
            abs_spread = best_bid - ask_price
            if abs_spread >= HIGH_CONVICTION_SPREAD:
                # Skip REST — such a wide spread is almost certainly real,
                # and the ~350ms REST costs is enough to lose the race.
                t_queued = time.monotonic()
                _mispricing_queue.put_nowait({
                    "token_id":  token_id,
                    "ask_price": ask_price,
                    "ask_size":  ask_size,
                    "best_bid":  best_bid,
                    "t_detect":  t_detect,
                    "t_queued":  t_queued,
                    "skipped_rest": True,
                })
                print(
                    f"[mispricing] ⚡ high-conviction queue (spread={abs_spread:.3f} ≥ {HIGH_CONVICTION_SPREAD}) "
                    f"{token_id[:20]}... ask={ask_price:.3f} bid={best_bid:.3f} — REST skipped"
                )
            else:
                # Marginal detection — confirm via REST to filter stale-cache noise.
                try:
                    asyncio.get_running_loop().create_task(
                        _validate_and_queue(token_id, ask_price, ask_size, best_bid, t_detect)
                    )
                except RuntimeError:
                    _mispricing_queue.put_nowait({
                        "token_id":  token_id,
                        "ask_price": ask_price,
                        "ask_size":  ask_size,
                        "best_bid":  best_bid,
                        "t_detect":  t_detect,
                    })

    now_ = time.monotonic()
    total_calls = _det_calls_lite + _det_calls_full
    if now_ - _last_funnel_log >= _HEARTBEAT_SECS and total_calls > 0:
        _last_funnel_log = now_
        print(
            f"[mispricing] 🔬 funnel(30s): lite={_det_calls_lite} full={_det_calls_full} "
            f"bid>0.05={_det_bid_ok} ask>0={_det_ask_ok} "
            f"ratio<{CLOB_MISPRICING_BID_RATIO:.2f}={_det_ratio_ok} "
            f"spread>{CLOB_MISPRICING_MIN_SPREAD:.2f}={_det_spread_ok} "
            f"depth>={CLOB_MISPRICING_MIN_BID_DEPTH:.0f}={_det_depth_ok} "
            f"| spread_pct range=[{_det_worst_spread_pct*100:.1f}%, {_det_best_spread_pct*100:.1f}%] "
            f"| max depth near-miss={_det_best_depth:.2f} USDC"
        )
        _det_calls_lite = _det_calls_full = _det_bid_ok = _det_ask_ok = _det_ratio_ok = _det_spread_ok = _det_depth_ok = 0
        _det_best_spread_pct = -999.0
        _det_worst_spread_pct = 999.0
        _det_best_depth = 0.0

    # --- SANDWICH ARB (disabled) -------------------------------------------
    # if ask_price <= MAKER_SANDWICH_MAX_ASK and best_bid >= MAKER_SANDWICH_MIN_BID
    #         and best_bid - ask_price >= MAKER_SANDWICH_MIN_PROFIT and ask_size > 0:
    #     ...  # crossed books don't persist — see comment block above
    # -----------------------------------------------------------------------


def _store_book(token_id: str, asks: list, bids: list) -> None:
    """Replace cached book, recompute stale asks, trigger detection."""
    if token_id in _lite_tokens:
        sorted_bids = sorted(bids, key=lambda b: float(b["price"]), reverse=True)[:5]
        sorted_asks = sorted(asks, key=lambda a: float(a["price"]))[:5]
        _price_signals[token_id] = {
            "bids": sorted_bids,
            "asks": sorted_asks,
        }
        # Replay buffered deltas received before this snapshot
        pending = _pending_deltas.pop(token_id, None)
        if pending:
            for pside, pupdates in pending:
                _apply_side_update(token_id, pside, pupdates)
            sig = _price_signals.get(token_id, {})
            sorted_bids = sig.get("bids", sorted_bids)
            sorted_asks = sig.get("asks", sorted_asks)
        if sorted_asks and sorted_bids and float(sorted_bids[0]["price"]) > 0.05:
            _run_detection(token_id, sorted_asks, sorted_bids)
        return

    global _book_update_count, _last_book_log
    _book_update_count += 1
    now = time.monotonic()
    if now - _last_book_log >= _HEARTBEAT_SECS:
        _last_book_log = now
        high_bids = sum(1 for b in _books.values() if b["bids"] and float(b["bids"][0]["price"]) >= 0.70)
        low_asks  = sum(1 for b in _books.values() if b["asks"] and float(min(b["asks"], key=lambda a: float(a["price"]))["price"]) <= 0.30)
        crossed   = sum(1 for b in _books.values() if b["bids"] and b["asks"] and float(b["bids"][0]["price"]) >= 0.80 and float(min(b["asks"], key=lambda a: float(a["price"]))["price"]) <= 0.20)
        print(f"[arb_ws] 📊 {_book_update_count} book updates | {len(_books)} books | bid≥0.70: {high_bids} | ask≤0.30: {low_asks} | crossed(0.80/0.20): {crossed}")

    sorted_bids = sorted(bids, key=lambda b: float(b["price"]), reverse=True)
    sorted_asks = sorted(asks, key=lambda a: float(a["price"]))
    _books[token_id] = {"asks": sorted_asks, "bids": sorted_bids}
    _stale[token_id] = [a for a in sorted_asks if float(a.get("price", 1.0)) <= ARB_MAX_STALE_PRICE]

    # Replay any deltas that arrived before this snapshot.
    pending = _pending_deltas.pop(token_id, None)
    if pending:
        for pside, pupdates in pending:
            _apply_side_update(token_id, pside, pupdates)

    # Bid watcher — compare against best bid, i.e. sorted_bids[0]
    if token_id in _bid_watchers:
        min_bid, event = _bid_watchers[token_id]
        if not event.is_set() and sorted_bids and float(sorted_bids[0]["price"]) >= min_bid:
            event.set()

    _run_detection(token_id, sorted_asks, sorted_bids)


def _apply_side_update(token_id: str, side: str, updates: list[dict]) -> None:
    """
    Apply incremental updates to one side of the cached book.
    The WS sends partial messages like {asset_id, bids: [{price, size}]}
    where each entry is a level to add/update (size>0) or remove (size=0).
    """
    if token_id in _lite_tokens:
        signal = _price_signals.get(token_id)
        if signal is None:
            # Buffer until the first snapshot arrives for this lite token.
            buf = _pending_deltas.setdefault(token_id, [])
            if len(buf) < _PENDING_DELTAS_CAP:
                buf.append((side, updates))
            return
        level_map = {e["price"]: e for e in signal[side]}
        for entry in updates:
            price = entry.get("price", "")
            if not price:
                continue
            if float(entry.get("size", 0)) == 0:
                level_map.pop(price, None)
            else:
                level_map[price] = entry
        if side == "bids":
            signal["bids"] = sorted(level_map.values(), key=lambda b: float(b["price"]), reverse=True)[:5]
        else:
            signal["asks"] = sorted(level_map.values(), key=lambda a: float(a["price"]))[:5]
        bids = signal["bids"]
        asks = signal["asks"]
        if asks and bids and float(bids[0]["price"]) > 0.05:
            _run_detection(token_id, asks, bids)
        return

    book = _books.get(token_id)
    if book is None:
        # No snapshot yet — buffer this delta and apply when the snapshot arrives.
        buf = _pending_deltas.setdefault(token_id, [])
        if len(buf) < _PENDING_DELTAS_CAP:
            buf.append((side, updates))
        return

    level_map = {e["price"]: e for e in book[side]}
    for entry in updates:
        price = entry.get("price", "")
        if not price:
            continue
        if float(entry.get("size", 0)) == 0:
            level_map.pop(price, None)
        else:
            level_map[price] = entry

    global _book_update_count, _last_book_log
    _book_update_count += 1
    now = time.monotonic()
    if now - _last_book_log >= _HEARTBEAT_SECS:
        _last_book_log = now
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

    global _total_messages, _last_msg_log
    _total_messages += 1
    now = time.monotonic()
    if now - _last_msg_log >= _HEARTBEAT_SECS:
        _last_msg_log = now
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

    # --- Best bid/ask tick: re-detect from cached book (full or lite) ---
    if asset_id and ("best_bid" in msg or "best_ask" in msg):
        book = _books.get(asset_id)
        if book:
            _run_detection(asset_id, book["asks"], book["bids"])
            return
        signal = _price_signals.get(asset_id)
        if signal:
            _run_detection(asset_id, signal.get("asks", []), signal.get("bids", []))
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


_WS_CHUNK_SIZE = 5_000  # max tokens per WS connection (server rejects larger messages)


async def _run_orderbook_ws_chunk(chunk: list[str], chunk_id: int) -> None:
    """Run a single orderbook WS connection for a chunk of token_ids."""
    retry_delay = 5
    while True:
        try:
            async with websockets.connect(
                CLOB_WS, ping_interval=20, ping_timeout=10, open_timeout=15,
                max_size=10 * 1024 * 1024,  # 10MB receive limit
            ) as ws:
                retry_delay = 5
                await ws.send(orjson.dumps({
                    "type": "market",
                    "assets_ids": chunk,
                    "custom_feature_enabled": True,
                }).decode())
                print(f"[arb_ws] 📖 WS shard {chunk_id} connected | {len(chunk)} tokens")
                async for raw in ws:
                    if not raw:
                        continue
                    try:
                        _handle_message(orjson.loads(raw))
                    except (orjson.JSONDecodeError, ValueError):
                        pass
                    except Exception as e:
                        print(f"[arb_ws] shard {chunk_id} error: {e}")
        except Exception as e:
            print(f"[arb_ws] shard {chunk_id} lost: {e}. Retrying in {retry_delay}s…")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


async def _run_orderbook_ws() -> None:
    """
    WS connection #2: subscribes to orderbook updates.
    Collects all queued tokens, splits them into chunks of _WS_CHUNK_SIZE,
    and runs one WS connection per chunk in parallel.
    Reconnects all shards when new tokens are queued (every 30s check).
    """
    _shard_tasks: list[asyncio.Task] = []

    async def _restart_shards() -> None:
        nonlocal _shard_tasks
        # Cancel existing shards and wait for them to actually stop so the
        # new shards don't race against old ones feeding _books/_price_signals.
        for t in _shard_tasks:
            t.cancel()
        if _shard_tasks:
            await asyncio.gather(*_shard_tasks, return_exceptions=True)
        _shard_tasks = []
        tokens = list(_subscribed_tokens)
        chunks = [tokens[i:i + _WS_CHUNK_SIZE] for i in range(0, len(tokens), _WS_CHUNK_SIZE)]
        print(f"[arb_ws] 📖 {len(tokens)} tokens → {len(chunks)} WS shards")
        for i, chunk in enumerate(chunks):
            _shard_tasks.append(asyncio.create_task(_run_orderbook_ws_chunk(chunk, i)))

    while True:
        # Wait for at least one token
        token_id = await _subscription_queue.get()
        _subscribed_tokens.add(token_id)
        # Drain queue + short wait for bulk adds
        while True:
            try:
                _subscribed_tokens.add(_subscription_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        await asyncio.sleep(0.5)
        while True:
            try:
                _subscribed_tokens.add(_subscription_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        await _restart_shards()

        # Wait 30s then check if new tokens arrived — if so, restart all shards
        while True:
            await asyncio.sleep(30)
            if not _subscription_queue.empty():
                while True:
                    try:
                        _subscribed_tokens.add(_subscription_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                pending = len(_subscribed_tokens)
                print(f"[arb_ws] 🔄 new tokens queued — restarting shards ({pending} total)")
                await _restart_shards()


# Tracks all token_ids ever subscribed — used to re-subscribe on reconnect
_subscribed_tokens: set[str] = set()


async def _audit_lite_state_vs_rest(interval_s: int = 180, sample_size: int = 20) -> None:
    """
    Periodically sample _price_signals and compare to REST /book for the same tokens.
    Logs divergence count every interval. If systematic divergence, our lite cache is lying.
    """
    await asyncio.sleep(120)  # wait for subscriptions to warm up
    loop = asyncio.get_running_loop()

    def _fetch_book(token_id: str) -> dict | None:
        try:
            url = f"https://clob.polymarket.com/book?token_id={token_id}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except Exception:
            return None

    while True:
        try:
            tokens = list(_price_signals.keys())
            print(f"[audit] tick — _price_signals has {len(tokens)} tokens")
            if len(tokens) < sample_size:
                await asyncio.sleep(interval_s)
                continue
            sample = random.sample(tokens, sample_size)

            divergences = []
            for tid in sample:
                sig = _price_signals.get(tid)
                if not sig:
                    continue
                ws_bids_list = sig.get("bids", [])
                ws_bid = float(ws_bids_list[0]["price"]) if ws_bids_list else 0.0
                ws_asks = sig.get("asks", [])
                ws_ask = float(ws_asks[0]["price"]) if ws_asks else 0.0

                data = await loop.run_in_executor(None, _fetch_book, tid)
                if not data:
                    continue
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                rest_bid = max((float(b["price"]) for b in bids if float(b.get("size", 0)) > 0), default=0.0)
                rest_ask = min((float(a["price"]) for a in asks if float(a.get("size", 0)) > 0), default=0.0)

                if abs(ws_bid - rest_bid) > 0.005 or abs(ws_ask - rest_ask) > 0.005:
                    divergences.append((tid, ws_bid, ws_ask, rest_bid, rest_ask))

            print(f"[audit] sampled {sample_size} lite tokens: {len(divergences)} divergent (|Δ|>0.5¢)")
            for tid, wb, wa, rb, ra in divergences[:5]:
                print(f"[audit]   {tid[:20]}... WS=[{wb:.3f}/{wa:.3f}] REST=[{rb:.3f}/{ra:.3f}]")
        except Exception as e:
            print(f"[audit] error: {e}")
        await asyncio.sleep(interval_s)


async def run_clob_ws() -> None:
    """
    Run both WS connections concurrently:
    - Market status WS (closures)
    - Orderbook WS (book updates for sandwich/mispricing detection)
    - Audit loop: periodically compare internal state to REST
    """
    await asyncio.gather(
        _run_market_status_ws(),
        _run_orderbook_ws(),
        _audit_lite_state_vs_rest(),
    )

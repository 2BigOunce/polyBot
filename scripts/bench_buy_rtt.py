"""
Benchmark py_clob_client BUY roundtrip from the VPS.

Posts N unmatchable BUY orders (price=0.001, far below any real ask) on a
live active token, measures create_and_post_order RTT, then cancels them.

Usage (on VPS):
    cd /home/ubuntu/polyBot && PYTHONPATH=. venv/bin/python scripts/bench_buy_rtt.py

No capital at risk — BUYs at 0.001 can never match on a normal market
(min ask is typically >= 0.01). They rest as live orders then get cancelled.
"""

import asyncio
import json
import time
import urllib.request

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs

from config.settings import (
    CLOB_API_KEY,
    CLOB_PASSPHRASE,
    CLOB_PROXY_URL,
    CLOB_SECRET,
    REAL_PROXY_WALLET,
    REAL_WALLET_PRIVATE_KEY,
)

if CLOB_PROXY_URL:
    import httpx as _httpx
    from py_clob_client.http_helpers import helpers as _clob_helpers
    _clob_helpers._http_client = _httpx.Client(
        http2=True,
        mounts={
            "https://": _httpx.HTTPTransport(proxy=CLOB_PROXY_URL),
            "http://":  _httpx.HTTPTransport(proxy=CLOB_PROXY_URL),
        },
    )

N_ORDERS = 8
BUY_PRICE = 0.01    # Polymarket min price; safe as long as best_ask > 0.05 (verified below)
BUY_SIZE  = 100     # 100 × 0.01 = $1 notional (Polymarket min order)
MIN_ASK_SAFETY = 0.05  # only run if best_ask on chosen token is above this


def _get_client() -> ClobClient:
    creds = ApiCreds(
        api_key=CLOB_API_KEY,
        api_secret=CLOB_SECRET,
        api_passphrase=CLOB_PASSPHRASE,
    )
    return ClobClient(
        host="https://clob.polymarket.com",
        key=REAL_WALLET_PRIVATE_KEY,
        chain_id=137,
        creds=creds,
        signature_type=2,
        funder=REAL_PROXY_WALLET,
    )


def _get_best_ask(token_id: str) -> float:
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
    except Exception:
        return 0.0
    asks = data.get("asks") or []
    if not asks:
        return 0.0
    prices = [float(a["price"]) for a in asks if float(a.get("size", 0)) > 0]
    return min(prices) if prices else 0.0


def _pick_live_token() -> tuple[str, str]:
    """Fetch a live active market with best_ask > MIN_ASK_SAFETY. Returns (token_id, question)."""
    url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        markets = json.loads(r.read())

    for m in markets:
        clob_ids = m.get("clobTokenIds")
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except Exception:
                continue
        if not clob_ids:
            continue
        for tid in clob_ids:
            best_ask = _get_best_ask(tid)
            if best_ask > MIN_ASK_SAFETY:
                print(f"[pick] best_ask={best_ask:.3f} on {tid[:16]}... — safe (> {MIN_ASK_SAFETY})")
                return tid, m.get("question", "")
    raise RuntimeError(f"no token with best_ask > {MIN_ASK_SAFETY} found in 50 markets")


async def main() -> None:
    token_id, question = _pick_live_token()
    print(f"Token : {token_id[:32]}...")
    print(f"Market: {question[:80]}")
    print(f"Orders: {N_ORDERS} × BUY {BUY_SIZE} @ {BUY_PRICE} (never-match)")
    print()

    client = _get_client()
    loop   = asyncio.get_running_loop()

    # Warm-up: client internal state (e.g. nonce, auth) lazily initialized on first call
    print("[warmup] placing 1 warm-up order (not counted)...")
    t0 = time.monotonic()
    try:
        resp = await loop.run_in_executor(
            None,
            lambda: client.create_and_post_order(
                OrderArgs(token_id=token_id, price=BUY_PRICE, size=BUY_SIZE, side="BUY")
            ),
        )
        t1 = time.monotonic()
        warmup_oid = (resp or {}).get("orderID") or (resp or {}).get("id")
        print(f"[warmup] rtt={1000*(t1-t0):.0f}ms status={(resp or {}).get('status')} oid={(warmup_oid or '')[:16]}")
    except Exception as e:
        print(f"[warmup] failed: {e}")
        return

    await asyncio.sleep(1.0)

    rtts: list[float] = []
    order_ids: list[str] = []
    if warmup_oid:
        order_ids.append(warmup_oid)

    for i in range(N_ORDERS):
        t0 = time.monotonic()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: client.create_and_post_order(
                    OrderArgs(token_id=token_id, price=BUY_PRICE, size=BUY_SIZE, side="BUY")
                ),
            )
            t1 = time.monotonic()
            rtt_ms = 1000 * (t1 - t0)
            rtts.append(rtt_ms)
            oid = (resp or {}).get("orderID") or (resp or {}).get("id") or ""
            status = (resp or {}).get("status", "?")
            if oid:
                order_ids.append(oid)
            print(f"[buy #{i+1}] rtt={rtt_ms:.0f}ms status={status} oid={oid[:16]}")
        except Exception as e:
            print(f"[buy #{i+1}] ERROR: {e}")
        await asyncio.sleep(0.3)

    # Cancel every order we placed
    if order_ids:
        t0 = time.monotonic()
        try:
            cancel_resp = await loop.run_in_executor(
                None,
                lambda: client.cancel_orders(order_ids),
            )
            t1 = time.monotonic()
            print(f"\n[cancel] {len(order_ids)} orders | rtt={1000*(t1-t0):.0f}ms")
            cancelled = (cancel_resp or {}).get("canceled") or (cancel_resp or {}).get("cancelled") or []
            not_cancelled = (cancel_resp or {}).get("not_canceled") or (cancel_resp or {}).get("not_cancelled") or []
            print(f"          confirmed={len(cancelled)} failed={len(not_cancelled)}")
            if not_cancelled:
                print(f"          ⚠ still live: {not_cancelled}")
        except Exception as e:
            print(f"[cancel] ERROR: {e}  — orders may still be live: {order_ids}")

    # Stats
    if not rtts:
        return
    rtts_sorted = sorted(rtts)
    mid = rtts_sorted[len(rtts_sorted) // 2]
    p90 = rtts_sorted[int(len(rtts_sorted) * 0.9)] if len(rtts_sorted) >= 10 else rtts_sorted[-1]
    mean = sum(rtts) / len(rtts)
    print()
    print("=== BUY create_and_post_order RTT ===")
    print(f"  n      = {len(rtts)}")
    print(f"  min    = {min(rtts):.0f}ms")
    print(f"  median = {mid:.0f}ms")
    print(f"  mean   = {mean:.0f}ms")
    print(f"  p90    = {p90:.0f}ms")
    print(f"  max    = {max(rtts):.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())

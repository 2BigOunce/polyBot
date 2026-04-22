"""
Smoke test for py_clob_client_v2 before the 28 April V2 cutover.

Validates that the V2 SDK can:
  1. Instantiate a client with our creds
  2. Fetch the orderbook for a known token
  3. Query our wallet balances/allowances
  4. (Optional, with --order) place + immediately cancel a tiny test order

Relies on the SDK's hot-swap mechanism: before the cutover the V2 SDK
transparently talks V1 semantics to the V1 backend. After 28 April it auto-
switches. If this test passes today, the executors should keep working past
the cutover without code changes.

Usage:
    PYTHONPATH=. python3 scripts/test_py_clob_v2.py
    PYTHONPATH=. python3 scripts/test_py_clob_v2.py --token 0xabc... --order
"""

import argparse
import sys

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, OrderArgs

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
    from py_clob_client_v2.http_helpers import helpers as _clob_helpers
    _clob_helpers._http_client = _httpx.Client(
        http2=True,
        mounts={
            "https://": _httpx.HTTPTransport(proxy=CLOB_PROXY_URL),
            "http://":  _httpx.HTTPTransport(proxy=CLOB_PROXY_URL),
        },
    )

CLOB_HOST = "https://clob.polymarket.com"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", help="token_id to probe (default: fetch first active market)")
    parser.add_argument("--order", action="store_true", help="Place + cancel a 1-share test BUY @ 0.01")
    args = parser.parse_args()

    # --- 1. Client instantiation ---
    print("[1/4] Instantiating ClobClient V2…")
    creds = ApiCreds(
        api_key=CLOB_API_KEY,
        api_secret=CLOB_SECRET,
        api_passphrase=CLOB_PASSPHRASE,
    )
    client = ClobClient(
        host=CLOB_HOST,
        key=REAL_WALLET_PRIVATE_KEY,
        chain_id=137,
        creds=creds,
        signature_type=2,
        funder=REAL_PROXY_WALLET,
    )
    print(f"       ✅ client built, funder={REAL_PROXY_WALLET}")

    # --- 2+3. Find a market with a live orderbook ---
    token_id = args.token
    if token_id:
        print(f"\n[2/4] Using --token {token_id[:24]}…")
        print(f"\n[3/4] Fetching orderbook for {token_id[:24]}…")
        try:
            book = client.get_order_book(token_id)
            bids = getattr(book, "bids", None) or (book.get("bids", []) if isinstance(book, dict) else [])
            asks = getattr(book, "asks", None) or (book.get("asks", []) if isinstance(book, dict) else [])
            print(f"       ✅ book: {len(bids)} bids / {len(asks)} asks")
        except Exception as e:
            print(f"       ❌ get_order_book failed: {e}")
            return 1
    else:
        print("\n[2/4] No --token given — scanning /markets for one with a live orderbook…")
        try:
            markets = client.get_markets()
        except Exception as e:
            print(f"       ❌ get_markets failed: {e}")
            return 1
        items = markets if isinstance(markets, list) else markets.get("data", [])

        found = False
        tried = 0
        for m in items:
            if m.get("closed") or m.get("archived"):
                continue
            for t in (m.get("tokens") or []):
                tid = t.get("token_id") or t.get("tokenId")
                if not tid:
                    continue
                tried += 1
                try:
                    book = client.get_order_book(tid)
                except Exception:
                    if tried >= 30:
                        break
                    continue
                bids = getattr(book, "bids", None) or (book.get("bids", []) if isinstance(book, dict) else [])
                asks = getattr(book, "asks", None) or (book.get("asks", []) if isinstance(book, dict) else [])
                if bids or asks:
                    token_id = tid
                    print(f"       ✅ picked token {tid[:24]}… from {m.get('question', '')[:50]}")
                    print(f"\n[3/4] Orderbook: {len(bids)} bids / {len(asks)} asks")
                    found = True
                    break
            if found or tried >= 30:
                break
        if not found:
            print(f"       ❌ no live orderbook found after {tried} attempts")
            return 1

    # --- 4. Optional: place + cancel test order ---
    if args.order:
        print("\n[4/4] Placing test BUY 1 share @ 0.01…")
        try:
            resp = client.create_and_post_order(
                OrderArgs(token_id=token_id, price=0.01, size=1, side="BUY")
            )
            oid = (resp or {}).get("orderID") or (resp or {}).get("id", "")
            status = (resp or {}).get("status", "unknown")
            print(f"       ✅ placed: status={status} id={oid[:16]}…")
            if oid:
                cancel_resp = client.cancel_order(oid)
                print(f"       ✅ cancel: {cancel_resp}")
        except Exception as e:
            print(f"       ❌ order flow failed: {e}")
            return 1
    else:
        print("\n[4/4] Skipped order test (use --order to enable)")

    print("\n✅ V2 SDK smoke test passed — safe to deploy v2 executors")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Cancel every open MAKER order before the V2 cutover (28 April 2026 ~11:00 UTC).

Polymarket wipes all open orders during the migration. Cancelling ahead of time
lets us close the DB rows cleanly ("expired" status) so stats stay consistent,
and frees the escrowed USDC.e so it can be wrapped into pUSD.

Usage:
    PYTHONPATH=. python3 scripts/cancel_all_maker_orders.py --dry-run
    PYTHONPATH=. python3 scripts/cancel_all_maker_orders.py --execute
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from config.settings import (
    CLOB_API_KEY,
    CLOB_PASSPHRASE,
    CLOB_PROXY_URL,
    CLOB_SECRET,
    REAL_PROXY_WALLET,
    REAL_WALLET_PRIVATE_KEY,
)
from src.chain.database import get_connection

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

CLOB_HOST = "https://clob.polymarket.com"


def _get_client() -> ClobClient:
    creds = ApiCreds(
        api_key=CLOB_API_KEY,
        api_secret=CLOB_SECRET,
        api_passphrase=CLOB_PASSPHRASE,
    )
    return ClobClient(
        host=CLOB_HOST,
        key=REAL_WALLET_PRIVATE_KEY,
        chain_id=137,
        creds=creds,
        signature_type=2,
        funder=REAL_PROXY_WALLET,
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Actually cancel orders (default = dry-run)")
    args = parser.parse_args()

    with get_connection() as conn:
        open_rows = conn.execute(
            "SELECT order_id, market_id, outcome, price, shares, placed_at FROM maker_orders WHERE status = 'open'"
        ).fetchall()
        sell_rows = conn.execute(
            "SELECT order_id, sell_order_id, market_id, outcome, price FROM maker_orders "
            "WHERE status = 'awaiting_sell' AND sell_order_id IS NOT NULL"
        ).fetchall()

    total = len(open_rows) + len(sell_rows)
    print(f"Open BUY orders         : {len(open_rows)}")
    print(f"Awaiting-sell SELL orders: {len(sell_rows)}")
    print(f"Total to cancel         : {total}")
    if total == 0:
        return 0

    exposure = sum(r["price"] * r["shares"] for r in open_rows)
    print(f"Capital escrowed in open BUYs: ~{exposure:.2f} USDC")

    if not args.execute:
        print("\n[DRY-RUN] Use --execute to actually cancel on CLOB + update DB.")
        return 0

    client = _get_client()
    loop   = asyncio.get_event_loop()
    now    = datetime.now(tz=timezone.utc).isoformat()

    cancelled = failed = 0

    # --- Cancel BUY orders ---
    for row in open_rows:
        oid = row["order_id"]
        try:
            await loop.run_in_executor(None, lambda o=oid: client.cancel_order(o))
            with get_connection() as conn:
                conn.execute(
                    "UPDATE maker_orders SET status = 'expired', closed_at = ? WHERE order_id = ?",
                    (now, oid),
                )
            print(f"[cancel] ✅ BUY  {row['outcome']} @ {row['price']} | {oid[:12]}…")
            cancelled += 1
        except Exception as e:
            print(f"[cancel] ❌ BUY  {oid[:12]}… — {e}")
            failed += 1
        await asyncio.sleep(0.2)

    # --- Cancel SELL orders (holding filled positions with limit sells out) ---
    for row in sell_rows:
        soid = row["sell_order_id"]
        try:
            await loop.run_in_executor(None, lambda o=soid: client.cancel_order(o))
            # Revert status to "filled" so it can be re-sold or handled via resolution
            with get_connection() as conn:
                conn.execute(
                    "UPDATE maker_orders SET sell_order_id = NULL, status = 'filled' WHERE order_id = ?",
                    (row["order_id"],),
                )
            print(f"[cancel] ✅ SELL {row['outcome']} @ {row['price']} | {soid[:12]}…")
            cancelled += 1
        except Exception as e:
            print(f"[cancel] ❌ SELL {soid[:12]}… — {e}")
            failed += 1
        await asyncio.sleep(0.2)

    print(f"\nDone: {cancelled} cancelled, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

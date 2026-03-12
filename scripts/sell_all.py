"""
Sell all open real_trades and optionally wipe paper_trades.

Usage:
    python scripts/sell_all.py            # dry-run: show positions
    python scripts/sell_all.py --execute  # sell everything + mark DB closed
"""
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from py_clob_client.clob_types import ApiCreds, MarketOrderArgs
from py_clob_client.client import ClobClient
from eth_account import Account
from dotenv import load_dotenv

load_dotenv()

from config.settings import (
    CLOB_API_KEY, CLOB_PASSPHRASE, CLOB_PROXY_URL, CLOB_SECRET,
    REAL_PROXY_WALLET, REAL_TRADING_ENABLED, REAL_WALLET_PRIVATE_KEY,
)

if CLOB_PROXY_URL:
    import httpx as _httpx
    from py_clob_client.http_helpers import helpers as _clob_helpers
    _clob_helpers._http_client = _httpx.Client(
        http2=True,
        mounts={
            "https://": _httpx.HTTPTransport(proxy=CLOB_PROXY_URL),
            "http://": _httpx.HTTPTransport(proxy=CLOB_PROXY_URL),
        },
    )

DB_PATH = Path(__file__).parent.parent / "bot.db"
CLOB_HOST = "https://clob.polymarket.com"

EXECUTE = "--execute" in sys.argv


def get_client() -> ClobClient:
    creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASSPHRASE)
    funder = REAL_PROXY_WALLET or Account.from_key(REAL_WALLET_PRIVATE_KEY).address
    return ClobClient(host=CLOB_HOST, key=REAL_WALLET_PRIVATE_KEY, chain_id=137,
                      creds=creds, signature_type=2, funder=funder)


def load_open_trades():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM real_trades WHERE status = 'open' ORDER BY id"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def close_trade_in_db(trade_id: int, sell_price: float, pnl: float, status: str):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE real_trades SET status=?, pnl=?, closed_at=? WHERE id=?",
        (status, pnl, datetime.now(tz=timezone.utc).isoformat(), trade_id),
    )
    con.commit()
    con.close()


def wipe_paper_trades():
    con = sqlite3.connect(DB_PATH)
    n = con.execute("DELETE FROM paper_trades").rowcount
    con.commit()
    con.close()
    print(f"[sell_all] Deleted {n} paper_trades rows.")


async def sell_trade(client: ClobClient, trade: dict) -> tuple[bool, float]:
    shares = round(trade["size_usdc"] / trade["fill_price"], 4)
    if shares <= 0:
        return False, 0.0
    try:
        order_args = MarketOrderArgs(token_id=trade["token_id"], amount=shares, side="SELL")
        signed = await asyncio.to_thread(client.create_market_order, order_args)
        resp = await asyncio.to_thread(client.post_order, signed)
        sell_price = float(resp.get("price", 0) or 0)
        return True, sell_price
    except Exception as e:
        print(f"  [SELL FAILED] id={trade['id']} {trade['outcome']}: {e}")
        return False, 0.0


async def main():
    trades = load_open_trades()
    total_usdc = sum(t["size_usdc"] for t in trades)
    print(f"\n{'DRY RUN' if not EXECUTE else 'EXECUTING'} — {len(trades)} open positions, {total_usdc:.2f} USDC total\n")

    if not EXECUTE:
        for t in trades:
            shares = round(t["size_usdc"] / t["fill_price"], 4)
            print(f"  id={t['id']:3d} | {t['outcome']:<25} | entry={t['fill_price']:.3f} | {t['size_usdc']:.2f} USDC | ~{shares} shares")
        print("\nRun with --execute to sell all positions.")
        return

    if not REAL_TRADING_ENABLED:
        print("[sell_all] REAL_TRADING_ENABLED is False — aborting.")
        return

    client = get_client()
    sold, failed, total_pnl = 0, 0, 0.0

    for trade in trades:
        ok, sell_price = await sell_trade(client, trade)
        shares = round(trade["size_usdc"] / trade["fill_price"], 4)
        if ok and sell_price > 0:
            pnl = shares * sell_price - trade["size_usdc"]
            status = "won" if pnl > 0 else "lost"
            close_trade_in_db(trade["id"], sell_price, pnl, status)
            total_pnl += pnl
            sold += 1
            print(f"  SOLD id={trade['id']:3d} | {trade['outcome']:<25} | sell={sell_price:.3f} | P&L: {pnl:+.2f} USDC")
        else:
            # Market may be resolved or no liquidity — mark as closed with unknown P&L
            close_trade_in_db(trade["id"], 0.0, 0.0, "closed")
            failed += 1
            print(f"  FAILED id={trade['id']:3d} | {trade['outcome']:<25} | marked closed (no fill)")

    print(f"\n[sell_all] Done: {sold} sold, {failed} failed | Total P&L: {total_pnl:+.2f} USDC")

    # Wipe paper trades too
    wipe_paper_trades()
    print("[sell_all] Paper trades cleared. Restart the bot to apply changes.")


asyncio.run(main())

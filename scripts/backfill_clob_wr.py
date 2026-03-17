"""
Backfill clob_win_rate and clob_closed_trades for wallets already in the DB
that were added before this feature existed.

Usage:
  python3 scripts/backfill_clob_wr.py            # dry-run
  python3 scripts/backfill_clob_wr.py --execute  # apply
"""

import asyncio
import sys

import aiohttp

from src.chain.database import DB_PATH, get_connection
from src.analysis.clob_history import fetch_wallet_wr


async def backfill(execute: bool) -> None:
    print("=== CLOB WR BACKFILL ===" + (" — DRY RUN" if not execute else " — EXECUTING"))

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT wallet_address FROM deposits
            WHERE status NOT IN ('removed', 'unreliable')
              AND clob_win_rate IS NULL
            """
        ).fetchall()

    wallets = [r["wallet_address"] for r in rows]
    print(f"  {len(wallets)} wallets to process")

    if not wallets:
        print("  Nothing to do.")
        return

    async with aiohttp.ClientSession() as session:
        for i, wallet in enumerate(wallets, 1):
            wr = await fetch_wallet_wr(wallet, session)
            closed   = wr["closed"]
            win_rate = wr["win_rate"]
            label = f"WR {win_rate:.0%} / {closed} closed" if closed > 0 else "no history"
            print(f"  [{i}/{len(wallets)}] {wallet[:12]}… — {label}")

            if execute and closed > 0:
                with get_connection() as conn:
                    conn.execute(
                        """
                        UPDATE deposits
                        SET clob_win_rate = ?, clob_closed_trades = ?
                        WHERE lower(wallet_address) = lower(?)
                        """,
                        (win_rate, closed, wallet),
                    )

    if not execute:
        print("\nRun with --execute to apply.")
    else:
        print("\nDone. Restart the bot: sudo systemctl restart polybot")


if __name__ == "__main__":
    asyncio.run(backfill(execute="--execute" in sys.argv))

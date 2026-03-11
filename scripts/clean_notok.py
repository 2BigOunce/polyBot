"""
One-shot cleanup script: removes deposits, trades and paper_trades that were
created during the Polygonscan NOTOK period (wallet_age_days = 0.0 exactly).

Usage:
  python scripts/clean_notok.py            # dry-run — shows what would be deleted
  python scripts/clean_notok.py --execute  # performs the actual deletion
"""

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).parent.parent / "bot.db"


def main(execute: bool = False):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Identify wallets that only have age=0.0 deposits (NOTOK artifacts)
    cur.execute("""
        SELECT DISTINCT wallet_address FROM deposits
        WHERE wallet_age_days = 0.0
        AND wallet_address NOT IN (
            SELECT DISTINCT wallet_address FROM deposits WHERE wallet_age_days > 0.0
        )
    """)
    notok_wallets = [r["wallet_address"] for r in cur.fetchall()]

    if not notok_wallets:
        print("Nothing to clean — no NOTOK wallets found.")
        conn.close()
        return

    placeholders = ",".join("?" * len(notok_wallets))

    cur.execute(f"SELECT COUNT(*) FROM deposits WHERE wallet_address IN ({placeholders})", notok_wallets)
    n_deposits = cur.fetchone()[0]

    cur.execute(f"SELECT COUNT(*) FROM trades WHERE wallet IN ({placeholders})", [w.lower() for w in notok_wallets])
    n_trades = cur.fetchone()[0]

    cur.execute(f"SELECT COUNT(*) FROM paper_trades WHERE followed_wallet IN ({placeholders})", [w.lower() for w in notok_wallets])
    n_paper = cur.fetchone()[0]

    print("=== NOTOK CLEANUP — DRY RUN ===" if not execute else "=== NOTOK CLEANUP — EXECUTING ===")
    print(f"  Wallets to remove : {len(notok_wallets)}")
    print(f"  Deposits          : {n_deposits}")
    print(f"  Trades            : {n_trades}")
    print(f"  Paper trades      : {n_paper}")

    if not execute:
        print("\nRun with --execute to apply.")
        conn.close()
        return

    lower_wallets = [w.lower() for w in notok_wallets]

    cur.execute(f"DELETE FROM paper_trades WHERE followed_wallet IN ({placeholders})", lower_wallets)
    cur.execute(f"DELETE FROM trades WHERE wallet IN ({placeholders})", lower_wallets)
    cur.execute(f"DELETE FROM deposits WHERE wallet_address IN ({placeholders})", notok_wallets)
    conn.commit()

    print("\nDone. Restart the bot to reload watched_wallets from the clean DB.")
    conn.close()


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)

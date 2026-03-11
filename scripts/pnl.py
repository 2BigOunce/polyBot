import sqlite3
from pathlib import Path

DB = Path(__file__).parent.parent / "bot.db"
conn = sqlite3.connect(DB)
cur = conn.cursor()

print("=== P&L GLOBAL ===")
cur.execute(
    "SELECT status, COUNT(*), ROUND(SUM(simulated_size),2), ROUND(SUM(pnl),2)"
    " FROM paper_trades WHERE status IN ('won','lost') GROUP BY status"
)
for r in cur.fetchall():
    print(f"  {r[0].upper()}: {r[1]} trades | volume: {r[2]} USDC | P&L: {r[3]} USDC")

cur.execute("SELECT ROUND(SUM(pnl),2) FROM paper_trades WHERE status IN ('won','lost')")
total = cur.fetchone()[0] or 0.0
print(f"  NET: {total:+.2f} USDC")

cur.execute("SELECT COUNT(*), ROUND(SUM(simulated_size),2) FROM paper_trades WHERE status='open'")
r = cur.fetchone()
print(f"\n=== OPEN POSITIONS ===\n  {r[0]} positions | {r[1]} USDC exposed")

cur.execute(
    "SELECT followed_wallet, COUNT(*), ROUND(SUM(pnl),2) as net"
    " FROM paper_trades WHERE status IN ('won','lost')"
    " GROUP BY followed_wallet ORDER BY net DESC LIMIT 5"
)
print("\n=== TOP 5 WALLETS ===")
for r in cur.fetchall():
    print(f"  {r[0][:14]}... | {r[1]} trades | net: {r[2]:+.2f} USDC")

conn.close()

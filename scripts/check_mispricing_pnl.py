"""
Check current P&L for actual on-chain trades executed during the
2026-04-20 16:25-16:26 mispricing event.

Uses data-api /trades to get ground truth (ignores log-parsed data).
For each unique token_id we traded, fetches current CLOB orderbook
and computes hypothetical P&L if sold at best_bid now.

Usage:
    PYTHONPATH=. python3 scripts/check_mispricing_pnl.py
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp

from config.settings import REAL_PROXY_WALLET

CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# Time window (UTC) for the mispricing cascade
START_TS = int(datetime(2026, 4, 20, 16, 25, 0, tzinfo=timezone.utc).timestamp())
END_TS   = int(datetime(2026, 4, 20, 16, 30, 0, tzinfo=timezone.utc).timestamp())


async def fetch_trades(session: aiohttp.ClientSession, wallet: str) -> list:
    """Fetch all trades for the wallet, filter to target window."""
    all_trades: list = []
    offset = 0
    while True:
        async with session.get(
            f"{DATA_API}/trades",
            params={"user": wallet, "limit": "500", "offset": str(offset)},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                print(f"/trades HTTP {resp.status}: {await resp.text()}")
                return all_trades
            batch = await resp.json()
        if not batch:
            break
        all_trades.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
        # Stop once trades are older than our window
        oldest = batch[-1].get("timestamp", 0)
        if int(oldest) < START_TS - 3600:
            break
    return all_trades


async def get_book(session: aiohttp.ClientSession, token_id: str) -> dict | None:
    async with session.get(
        f"{CLOB_API}/book",
        params={"token_id": token_id},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        return data if "bids" in data else None


async def get_position(session: aiohttp.ClientSession, wallet: str, token_id: str) -> float:
    """Exact current balance for one token via positions API with asset filter."""
    async with session.get(
        f"{DATA_API}/positions",
        params={"user": wallet, "market": token_id, "limit": "10"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status != 200:
            return 0.0
        data = await resp.json()
    for p in data:
        if str(p.get("asset", "")) == token_id:
            return float(p.get("size", 0))
    return 0.0


async def main() -> None:
    if not REAL_PROXY_WALLET:
        print("REAL_PROXY_WALLET not set in .env — aborting")
        return

    async with aiohttp.ClientSession() as session:
        print(f"Wallet: {REAL_PROXY_WALLET}")
        print(f"Fetching trades in window {datetime.fromtimestamp(START_TS, tz=timezone.utc)} to {datetime.fromtimestamp(END_TS, tz=timezone.utc)}...\n")

        trades = await fetch_trades(session, REAL_PROXY_WALLET)
        print(f"Total trades fetched: {len(trades)}")

        # Filter to target window
        window_trades = [
            t for t in trades
            if START_TS <= int(t.get("timestamp", 0)) <= END_TS
        ]
        print(f"Trades in 16:25-16:30 window: {len(window_trades)}\n")

        if not window_trades:
            print("No trades found in window — either timestamps are off or no trades executed")
            return

        # Aggregate by (token_id, side)
        agg: dict[tuple[str, str], dict] = defaultdict(
            lambda: {"shares": 0.0, "cost": 0.0, "n": 0, "title": "", "outcome": ""}
        )
        for t in window_trades:
            tid     = str(t.get("asset", ""))
            side    = t.get("side", "")
            size    = float(t.get("size", 0))
            price   = float(t.get("price", 0))
            title   = t.get("title") or t.get("slug") or ""
            outcome = t.get("outcome", "")
            key     = (tid, side)
            agg[key]["shares"]  += size
            agg[key]["cost"]    += size * price
            agg[key]["n"]       += 1
            agg[key]["title"]    = title
            agg[key]["outcome"]  = outcome

        print(f"{len(agg)} distinct (token, side) pairs\n")
        print("=" * 70)

        total_cost = 0.0
        total_current_value = 0.0

        for (tid, side), info in agg.items():
            shares   = info["shares"]
            cost     = info["cost"]
            avg_px   = cost / shares if shares else 0
            title    = info["title"][:55]
            outcome  = info["outcome"]

            if side != "BUY":
                print(f"\nℹ️  SELL {shares:.2f} shares of {title} [{outcome}] @ avg {avg_px:.3f} = {cost:.2f} USDC received")
                total_current_value += cost
                continue

            total_cost += cost

            held = await get_position(session, REAL_PROXY_WALLET, tid)
            book = await get_book(session, tid)

            if not book:
                print(f"\n⚠️  {title} [{outcome}] — no orderbook (market closed?)")
                print(f"    Bought {shares:.2f} shares @ avg {avg_px:.3f} = {cost:.2f} USDC | Held: {held:.2f}")
                continue

            bids = sorted(book.get("bids", []), key=lambda b: float(b["price"]), reverse=True)
            asks = book.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0.0
            best_ask = float(min(asks, key=lambda a: float(a["price"]))["price"]) if asks else 0.0

            # Walk the bid book: how much can we actually sell from our 'held' balance
            remaining = held
            revenue   = 0.0
            for b in bids:
                if remaining <= 0:
                    break
                lvl_price = float(b["price"])
                lvl_size  = float(b["size"])
                take      = min(remaining, lvl_size)
                revenue  += take * lvl_price
                remaining -= take
            filled_now = held - remaining
            avg_sell   = revenue / filled_now if filled_now else 0

            pnl = revenue - cost * (filled_now / shares) if shares else 0
            total_current_value += revenue
            tag = "✅" if pnl > 0 else "❌"

            print(f"\n{tag} {title} [{outcome}]")
            print(f"   BUY: {shares:.2f} shares @ avg {avg_px:.3f} = {cost:.2f} USDC ({info['n']} fills)")
            print(f"   Held now: {held:.2f} | best_bid={best_bid:.3f} | best_ask={best_ask:.3f}")
            print(f"   If sold now: {filled_now:.2f} shares → {revenue:.2f} USDC (avg {avg_sell:.3f})")
            print(f"   P&L: {pnl:+.2f} USDC")

        print("\n" + "=" * 70)
        print(f"Total BUY cost: {total_cost:.2f} USDC")
        print(f"Total value (sold now + past SELLs): {total_current_value:.2f} USDC")
        print(f"Net realised+unrealised P&L if exited now: {total_current_value - total_cost:+.2f} USDC")


if __name__ == "__main__":
    asyncio.run(main())

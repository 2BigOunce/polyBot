"""
Check current P&L for positions bought during the 2026-04-20 16:25-16:26
mispricing event. Queries data-api for current wallet positions and the
CLOB orderbook to compute hypothetical P&L if sold at best_bid now.

Usage: python scripts/check_mispricing_pnl.py
"""

import asyncio
from collections import defaultdict

import aiohttp

from config.settings import REAL_PROXY_WALLET

CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# Cost basis from logs (Apr 20 16:25-16:26) — only BUYs with status="matched"
# Format: (token_prefix_14, cost_usdc, shares, buy_price)
TRADES = [
    ("78432530460990", 28.57,   39.68, 0.720),
    ("80888050988600",  9.00,   30.00, 0.300),
    ("31087225693172", 10.53,  339.83, 0.031),
    ("81634541236325", 21.77, 1088.32, 0.020),
    ("96129480874598",  7.35,  919.04, 0.008),
    ("31087225693172", 10.00,  200.00, 0.050),
    ("11259921361243", 30.94,   43.58, 0.710),
    ("11259921361243", 35.31,   49.04, 0.720),
    ("11259921361243", 33.38,   45.73, 0.730),
    ("11259921361243", 50.00,   67.57, 0.740),
]


async def get_positions(session: aiohttp.ClientSession, wallet: str) -> list:
    async with session.get(
        f"{DATA_API}/positions",
        params={"user": wallet, "limit": "500"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        if resp.status != 200:
            print(f"positions HTTP {resp.status}: {await resp.text()}")
            return []
        return await resp.json()


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


async def main() -> None:
    if not REAL_PROXY_WALLET:
        print("REAL_PROXY_WALLET not set in .env — aborting")
        return

    # Aggregate trades by token prefix (same market traded multiple times)
    agg: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "shares": 0.0, "trades": []})
    for prefix, cost, shares, price in TRADES:
        agg[prefix]["cost"]   += cost
        agg[prefix]["shares"] += shares
        agg[prefix]["trades"].append((shares, price))

    async with aiohttp.ClientSession() as session:
        positions = await get_positions(session, REAL_PROXY_WALLET)
        print(f"\n📊 P&L Check for 2026-04-20 16:25 mispricing trades")
        print(f"Wallet: {REAL_PROXY_WALLET}")
        print(f"Found {len(positions)} current positions in wallet\n")

        # Map token_id prefix → position info
        pos_by_prefix: dict[str, dict] = {}
        for p in positions:
            tid = str(p.get("asset", ""))
            if tid:
                pos_by_prefix[tid[:14]] = {
                    "token_id": tid,
                    "shares":   float(p.get("size", 0)),
                    "market":   p.get("title") or p.get("slug", ""),
                    "outcome":  p.get("outcome", ""),
                }

        total_cost   = 0.0
        total_value  = 0.0
        total_shares_bought = 0.0
        total_shares_held   = 0.0

        for prefix, data in agg.items():
            cost          = data["cost"]
            shares_bought = data["shares"]
            trades_str    = ", ".join(f"{s:.0f}@{p:.3f}" for s, p in data["trades"])
            avg_price     = cost / shares_bought if shares_bought else 0
            total_cost    += cost
            total_shares_bought += shares_bought

            pos = pos_by_prefix.get(prefix)
            if not pos:
                print(f"❓ {prefix}... NOT IN WALLET")
                print(f"   Trades: {trades_str} = {cost:.2f} USDC for {shares_bought:.2f} shares")
                print(f"   Either partial fill never settled, or already sold\n")
                continue

            held = pos["shares"]
            total_shares_held += held

            book = await get_book(session, pos["token_id"])
            if not book:
                print(f"⚠️  {pos['market'][:50]} [{pos['outcome']}] — no orderbook")
                continue

            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0.0
            bid_size = float(bids[0]["size"]) if bids else 0.0
            best_ask = float(asks[-1]["price"]) if asks else 0.0

            # Simulate sell: match through bids until shares_to_sell exhausted
            shares_to_sell = min(held, shares_bought)
            revenue        = 0.0
            remaining      = shares_to_sell
            for b in sorted(bids, key=lambda x: float(x["price"]), reverse=True):
                if remaining <= 0:
                    break
                lvl_price = float(b["price"])
                lvl_size  = float(b["size"])
                take      = min(remaining, lvl_size)
                revenue  += take * lvl_price
                remaining -= take
            filled = shares_to_sell - remaining
            avg_sell = revenue / filled if filled else 0

            pnl = revenue - (cost * (filled / shares_bought)) if shares_bought else 0
            status = "✅" if pnl > 0 else "❌"

            print(f"{status} {pos['market'][:50]} [{pos['outcome']}]")
            print(f"   Bought: {shares_bought:.2f} @ avg {avg_price:.3f} = {cost:.2f} USDC ({trades_str})")
            print(f"   Held now: {held:.2f} | Best bid: {best_bid:.3f} (depth {bid_size:.2f}) | Best ask: {best_ask:.3f}")
            print(f"   If sold now: {filled:.2f} shares → {revenue:.2f} USDC (avg {avg_sell:.3f})")
            print(f"   Unfilled: {remaining:.2f} shares (bid depth too thin)")
            print(f"   P&L: {pnl:+.2f} USDC\n")
            total_value += revenue

        print("━" * 60)
        print(f"Total bought: {total_shares_bought:.2f} shares for {total_cost:.2f} USDC")
        print(f"Total held:   {total_shares_held:.2f} shares")
        print(f"Total value (sold now): {total_value:.2f} USDC")
        print(f"Total realised P&L if we exit now: {total_value - total_cost:+.2f} USDC")


if __name__ == "__main__":
    asyncio.run(main())

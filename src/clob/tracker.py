import asyncio
from datetime import datetime, timezone

import aiohttp

from config.settings import EARLY_SELL_HOURS
from src.alerts.telegram import send_alert, send_paper_sell  # noqa: F401
from src.chain.database import (
    close_paper_trade,
    get_open_paper_trade,
    get_wallet_trade_ids,
    mark_wallet_status,
    save_trade,
)
from src.clob import watched_wallets
from src.trading.paper_trader import copy_trade

CLOB_API      = "https://clob.polymarket.com"
DATA_API      = "https://data-api.polymarket.com"
POLL_INTERVAL = 30   # seconds between each wallet check

# Tracks which wallets already have a polling task running
_active: set[str] = set()

# Cache market end dates to avoid repeated /markets/{id} calls
_market_end_date_cache: dict[str, datetime | None] = {}


async def _get_market_end_date(market: str, session: aiohttp.ClientSession) -> datetime | None:
    """Fetch the resolution date of a market from CLOB (public endpoint). Cached per market."""
    if market in _market_end_date_cache:
        return _market_end_date_cache[market]
    try:
        async with session.get(f"{CLOB_API}/markets/{market}") as resp:
            data = await resp.json()
        raw = data.get("end_date_iso") or data.get("end_date")
        result = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    except Exception:
        result = None
    _market_end_date_cache[market] = result
    return result


async def poll_wallet(wallet: str, session: aiohttp.ClientSession):
    """Poll the public Polymarket data API for new trades from a flagged wallet."""
    print(f"[clob] Started tracking {wallet}")

    # Pre-populate from DB so we don't re-alert on trades already processed
    seen_tx: set[str] = get_wallet_trade_ids(wallet)
    is_first_poll = True  # first fetch bootstraps seen_tx silently for any remaining history

    while True:
        try:
            async with session.get(
                f"{DATA_API}/activity",
                params={"user": wallet, "limit": 100},
            ) as resp:
                if resp.status == 429:
                    print(f"[clob] 429 on {wallet[:10]}… — backing off 60s")
                    await asyncio.sleep(60)
                    continue
                trades = await resp.json()

            if not isinstance(trades, list):
                await asyncio.sleep(POLL_INTERVAL)
                continue

            for trade in trades:
                tx_hash = trade.get("transactionHash", "")
                if not tx_hash or tx_hash in seen_tx:
                    continue
                seen_tx.add(tx_hash)

                # First poll: just record what exists, don't alert on historical data
                if is_first_poll:
                    continue

                market     = trade.get("conditionId", "")
                side       = trade.get("side", "")
                size       = float(trade.get("usdcSize", 0))
                price      = float(trade.get("price", 0))
                outcome    = trade.get("outcome", "")
                match_time = str(trade.get("timestamp", ""))

                save_trade(tx_hash, wallet, market, side, size, price, outcome, match_time)

                if side == "BUY" and market and price > 0:
                    await copy_trade(wallet, market, outcome, price, size, session)

                if side == "SELL" and market and price > 0:
                    # Follow the insider's exit: close our paper trade at their sell price
                    paper = get_open_paper_trade(wallet, market, outcome)
                    if paper:
                        pnl = paper["simulated_size"] * (price / paper["entry_price"] - 1.0)
                        status = "won" if pnl > 0 else "lost"
                        close_paper_trade(paper["id"], status, pnl)
                        print(f"[paper] SELL {outcome} @ {price:.3f} | P&L: {pnl:+.2f} USDC")
                        await send_paper_sell(market, outcome, status, pnl, paper["simulated_size"])

                    # Unreliable check: selling far before resolution = bad signal
                    end_date = await _get_market_end_date(market, session)
                    if end_date:
                        now = datetime.now(tz=timezone.utc)
                        hours_until_end = (end_date - now).total_seconds() / 3600
                        if hours_until_end > EARLY_SELL_HOURS and (paper is None or pnl < 0):
                            print(f"[clob] {wallet} sold at a loss with {hours_until_end:.1f}h left — unreliable")
                            mark_wallet_status(wallet, "unreliable")
                            watched_wallets.watched.discard(wallet)

        except Exception as e:
            print(f"[clob] Error polling {wallet}: {e}")

        is_first_poll = False
        await asyncio.sleep(POLL_INTERVAL)



async def run_tracker():
    """
    Coordinator: watches the shared wallet set and spawns
    one polling task per new wallet.
    """
    watched_wallets.load_from_db()
    print("[clob] Tracker started")

    async with aiohttp.ClientSession() as session:
        while True:
            for wallet in list(watched_wallets.watched):
                if wallet not in _active:
                    _active.add(wallet)
                    asyncio.create_task(poll_wallet(wallet, session))

            await asyncio.sleep(5)  # check for new wallets every 5 seconds

import asyncio
from datetime import datetime, timezone

import aiohttp

from config.settings import EARLY_SELL_HOURS, MAX_INSIDER_PRICE, MIN_INSIDER_PRICE, MIN_INSIDER_TRADE_SIZE
from src.alerts.telegram import send_alert, send_follow_alert, send_super_alert  # noqa: F401
from src.chain.database import (
    close_paper_trade,
    get_open_paper_trade,
    get_wallet_market_stats,
    get_wallet_status,
    get_wallet_trade_ids,
    mark_wallet_status,
    save_trade,
)
from src.trading.real_trader import close_trade
from src.clob import watched_wallets
from src.clob.onchain_tracker import get_wake_event
from src.trading.paper_trader import copy_trade

CLOB_API      = "https://clob.polymarket.com"
DATA_API      = "https://data-api.polymarket.com"
POLL_INTERVAL = 30   # seconds between each wallet check

# Tracks which wallets already have a polling task running
_active: set[str] = set()

# Cache market info (end_date + is_sports) to avoid repeated /markets/{id} calls
_market_info_cache: dict[str, dict] = {}

# Count early sells at a loss per wallet — 3 triggers unreliable status
_early_sell_counts: dict[str, int] = {}
EARLY_SELL_LIMIT = 3


async def _get_market_info(market: str, session: aiohttp.ClientSession) -> dict:
    """Fetch end_date, tags and question for a market from CLOB. Cached per market."""
    if market in _market_info_cache:
        return _market_info_cache[market]
    try:
        async with session.get(f"{CLOB_API}/markets/{market}") as resp:
            data = await resp.json()
        raw = data.get("end_date_iso") or data.get("end_date")
        end_date = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
        is_sports = "Sports" in (data.get("tags") or [])
        question = data.get("question") or market[:20]
    except Exception:
        end_date = None
        is_sports = False
        question = market[:20]
    result = {"end_date": end_date, "is_sports": is_sports, "question": question}
    _market_info_cache[market] = result
    return result


async def poll_wallet(wallet: str, session: aiohttp.ClientSession):
    """Poll the public Polymarket data API for new trades from a flagged wallet."""
    print(f"[clob] Started tracking {wallet}")

    # Pre-populate from DB so we don't re-alert on trades already processed
    seen_tx: set[str] = get_wallet_trade_ids(wallet)
    is_first_poll = True  # first fetch bootstraps seen_tx silently for any remaining history

    wallet_status = get_wallet_status(wallet)
    is_watch_only  = wallet_status == "watch_only"
    is_follow_only = wallet_status == "follow_only"

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

                market     = trade.get("conditionId", "")
                side       = trade.get("side", "")
                size       = float(trade.get("usdcSize", 0))
                price      = float(trade.get("price", 0))
                outcome    = trade.get("outcome", "")
                match_time = str(trade.get("timestamp", ""))

                # Always persist to DB so restarts don't re-process historical trades
                save_trade(tx_hash, wallet, market, side, size, price, outcome, match_time)

                # First poll: bootstrap only — don't trigger paper trades
                if is_first_poll:
                    continue

                # Super alert: concentrated wallet betting heavily on a single market
                if side == "BUY" and market and MIN_INSIDER_PRICE < price < MAX_INSIDER_PRICE:
                    stats = get_wallet_market_stats(wallet, market)
                    total_buys = stats["total_buys"]
                    concentration = stats["buys_on_market"] / total_buys if total_buys > 0 else 0
                    if stats["buys_on_market"] >= 2 and concentration >= 0.60:
                        print(f"[clob] 🚨 SUPER ALERT {wallet[:10]}… {outcome} @ {price:.3f} | {size:.0f} USDC | conc {concentration:.0%}")
                        await send_super_alert(
                            wallet, market, outcome, price, size,
                            stats["max_deposit"], stats["buys_on_market"],
                        )

                # follow_only: alert on every BUY, no copy trade
                if side == "BUY" and market and is_follow_only:
                    info = await _get_market_info(market, session)
                    print(f"[clob] 👁 FOLLOW {wallet[:10]}… {outcome} @ {price:.3f} | {size:.0f} USDC")
                    await send_follow_alert(wallet, info["question"], outcome, price, size)

                if side == "BUY" and market and price > 0 and not is_watch_only and not is_follow_only:
                    if size < MIN_INSIDER_TRADE_SIZE:
                        print(f"[clob] {wallet[:10]}… trade too small ({size:.0f} USDC) — skipped")
                        continue
                    if price < MIN_INSIDER_PRICE or price > MAX_INSIDER_PRICE:
                        print(f"[clob] {wallet[:10]}… price out of range ({price:.3f}) — skipped")
                        continue
                    info = await _get_market_info(market, session)
                    end_date = info["end_date"]
                    is_sports = info["is_sports"]
                    days_until = (
                        (end_date - datetime.now(tz=timezone.utc)).total_seconds() / 86400
                        if end_date else None
                    )
                    await copy_trade(wallet, market, outcome, price, size, session,
                                     days_until_resolution=days_until, is_sports=is_sports)

                if side == "SELL" and market and price > 0:
                    # Follow the insider's exit: close our paper trade at their sell price
                    paper = get_open_paper_trade(wallet, market, outcome)
                    if paper:
                        pnl = paper["simulated_size"] * (price / paper["entry_price"] - 1.0)
                        status = "won" if pnl > 0 else "lost"
                        close_paper_trade(paper["id"], status, pnl)
                        print(f"[paper] SELL {outcome} @ {price:.3f} | P&L: {pnl:+.2f} USDC")

                    # Follow the insider's exit: close our real trade
                    await close_trade(wallet, market, outcome, price, session)

                    # Unreliable check: selling far before resolution = bad signal
                    # Requires EARLY_SELL_LIMIT consecutive early-loss sells before marking unreliable
                    info = await _get_market_info(market, session)
                    end_date = info["end_date"]
                    if end_date:
                        now = datetime.now(tz=timezone.utc)
                        hours_until_end = (end_date - now).total_seconds() / 3600
                        if hours_until_end > EARLY_SELL_HOURS and (paper is None or pnl < 0):
                            _early_sell_counts[wallet] = _early_sell_counts.get(wallet, 0) + 1
                            count = _early_sell_counts[wallet]
                            print(f"[clob] {wallet} early loss sell #{count}/{EARLY_SELL_LIMIT} ({hours_until_end:.1f}h left)")
                            if count >= EARLY_SELL_LIMIT:
                                print(f"[clob] {wallet} reached {EARLY_SELL_LIMIT} early loss sells — unreliable")
                                mark_wallet_status(wallet, "unreliable")
                                watched_wallets.watched.discard(wallet)
                                _early_sell_counts.pop(wallet, None)

        except Exception as e:
            print(f"[clob] Error polling {wallet}: {e}")

        is_first_poll = False

        # Wait for an on-chain OrderFilled event (fast path) or fall back to the regular interval
        wake = get_wake_event(wallet)
        try:
            await asyncio.wait_for(wake.wait(), timeout=POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass  # no on-chain event — regular poll
        finally:
            wake.clear()



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

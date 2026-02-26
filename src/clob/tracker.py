import asyncio

import aiohttp

from src.alerts.telegram import send_alert
from src.clob import watched_wallets

CLOB_API = "https://clob.polymarket.com"
POLL_INTERVAL = 30   # seconds between each wallet check

# Tracks which wallets already have a polling task running
_active: set[str] = set()


async def poll_wallet(wallet: str, session: aiohttp.ClientSession):
    """Poll the CLOB API for new trades from a flagged wallet."""
    print(f"[clob] Started tracking {wallet}")
    seen_trade_ids: set[str] = set()

    while True:
        try:
            url = f"{CLOB_API}/trades"
            params = {"maker_address": wallet}
            async with session.get(url, params=params) as resp:
                data = await resp.json()

            trades = data.get("data", [])
            for trade in trades:
                trade_id = trade.get("id")
                if trade_id in seen_trade_ids:
                    continue
                seen_trade_ids.add(trade_id)

                outcome = trade["outcome"]
                size    = float(trade["size"])
                market  = trade["market"]
                await send_trade_alert(wallet, outcome, size, market)

        except Exception as e:
            print(f"[clob] Error polling {wallet}: {e}")

        await asyncio.sleep(POLL_INTERVAL)


async def send_trade_alert(wallet: str, outcome: str, size: float, market: str):
    from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    from telegram import Bot
    message = (
        f"Trade detected!\n"
        f"Wallet: {wallet}\n"
        f"Market: {market}\n"
        f"Outcome: {outcome} | Size: {size:.2f} USDC"
    )
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


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

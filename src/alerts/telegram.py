from telegram import Bot

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def format_age(wallet_age_days: float) -> str:
    # YOUR TURN:
    # If wallet_age_days < 1, return the age in hours (rounded to 1 decimal), e.g. "3.2h"
    # Otherwise return the age in days (rounded to 1 decimal), e.g. "4.5d"
    if wallet_age_days < 1:
        return f"{wallet_age_days * 24:.1f}h"
    else:
        return f"{wallet_age_days:.1f}d"


async def send_alert(
    level: str,
    sender: str,
    amount_usdc: float,
    wallet_age_days: float,
    tx_hash: str,
):
    age_str = format_age(wallet_age_days)
    message = (
        f"{level} Alert Detected!\n"
        f"{amount_usdc:.2f} USDC | {age_str}\n"
        f"Wallet: {sender}\n"
        f"Tx: {tx_hash}"
    )
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)

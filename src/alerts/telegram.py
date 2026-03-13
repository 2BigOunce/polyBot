import asyncio

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from src.chain.database import get_pnl_summary, get_real_pnl_summary


def format_age(wallet_age_days: float) -> str:
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


async def send_super_alert(
    wallet: str,
    market: str,
    outcome: str,
    price: float,
    trade_size: float,
    max_deposit: float,
    buys_on_market: int,
):
    ratio = (trade_size / max_deposit * 100) if max_deposit else 0
    message = (
        f"🚨 SUPER INSIDER ALERT\n"
        f"Wallet: {wallet}\n"
        f"Market: {market}\n"
        f"Outcome: {outcome} @ {price:.3f}\n"
        f"Trade: {trade_size:.0f} USDC ({ratio:.1f}% of deposit)\n"
        f"Deposit: {max_deposit:.0f} USDC\n"
        f"Buys on this market: {buys_on_market}"
    )
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


async def send_paper_buy(
    followed_wallet: str,
    outcome: str,
    market: str,
    entry_price: float,
    our_size: float,
    insider_size: float,
):
    message = (
        f"Paper BUY\n"
        f"Copying: {followed_wallet}\n"
        f"Market: {market}\n"
        f"Outcome: {outcome} @ {entry_price:.3f}\n"
        f"Our size: {our_size:.2f} USDC\n"
        f"Insider trade: {insider_size:.2f} USDC"
    )
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


async def send_paper_sell(
    market: str,
    outcome: str,
    status: str,
    pnl: float,
    our_size: float,
):
    emoji = "✅ WON" if status == "won" else "❌ LOST"
    message = (
        f"Paper SELL — {emoji}\n"
        f"Market: {market}\n"
        f"Outcome: {outcome}\n"
        f"Size: {our_size:.2f} USDC\n"
        f"P&L: {pnl:+.2f} USDC"
    )
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


async def send_real_buy(
    followed_wallet: str,
    outcome: str,
    market: str,
    insider_price: float,
    fill_price: float,
    size_usdc: float,
    insider_size: float = 0.0,
):
    drift = (fill_price - insider_price) / insider_price * 100 if insider_price else 0
    message = (
        f"💸 REAL BUY\n"
        f"Copying: {followed_wallet}\n"
        f"Market: {market}\n"
        f"Outcome: {outcome}\n"
        f"Insider trade: {insider_size:.0f} USDC\n"
        f"Insider: {insider_price:.3f} → Fill: {fill_price:.3f} ({drift:+.1f}%)\n"
        f"Size: {size_usdc:.2f} USDC"
    )
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


async def send_real_sell(
    market: str,
    outcome: str,
    status: str,
    pnl: float,
    size_usdc: float,
):
    emoji = "✅ WON" if status == "won" else "❌ LOST"
    message = (
        f"💸 REAL SELL — {emoji}\n"
        f"Market: {market}\n"
        f"Outcome: {outcome}\n"
        f"Size: {size_usdc:.2f} USDC\n"
        f"P&L: {pnl:+.2f} USDC"
    )
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


async def send_cluster_alert(wallet: str, cluster: list[str]):
    siblings = "\n".join(f"  • {w}" for w in cluster)
    message = (
        f"Cluster Detected!\n"
        f"Wallet: {wallet}\n"
        f"Shares a funder with {len(cluster)} other suspect wallet(s):\n"
        f"{siblings}"
    )
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


async def _pnl_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_pnl_summary()
    lines = [
        "📊 Paper Trading P&L",
        f"✅ WON : {s['won_count']} trades | {s['won_pnl']:+.2f} USDC",
        f"❌ LOST: {s['lost_count']} trades | {s['lost_pnl']:+.2f} USDC",
        "─────────────────────",
        f"NET   : {s['net_pnl']:+.2f} USDC",
        f"Win rate: {s['win_rate']:.1f}%",
        f"Open  : {s['open_count']} positions",
    ]
    if s["top_wallet"]:
        w = s["top_wallet"]
        addr = w["followed_wallet"]
        lines.append(f"\n🏆 Top wallet: {addr[:6]}…{addr[-4:]} | {w['total']:+.2f} USDC")
    if s["best_trade"]:
        t = s["best_trade"]
        lines.append(f"💰 Best trade: {t['outcome']} @ {t['entry_price']:.3f} | {t['pnl']:+.2f} USDC")
    await update.message.reply_text("\n".join(lines))


async def _realpnl_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_real_pnl_summary()
    lines = [
        "💸 Real Trading P&L",
        f"✅ WON : {s['won_count']} trades | {s['won_pnl']:+.2f} USDC",
        f"❌ LOST: {s['lost_count']} trades | {s['lost_pnl']:+.2f} USDC",
        "─────────────────────",
        f"NET   : {s['net_pnl']:+.2f} USDC",
        f"Win rate: {s['win_rate']:.1f}%",
        f"Open  : {s['open_count']} positions | {s['open_exposure']:.2f} USDC exposed",
    ]
    if s["best_trade"]:
        t = s["best_trade"]
        lines.append(f"💰 Best trade: {t['outcome']} @ {t['insider_price']:.3f} | {t['pnl']:+.2f} USDC")
    await update.message.reply_text("\n".join(lines))


async def run_bot() -> None:
    """Start the Telegram bot and listen for commands."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("pnl", _pnl_command))
    app.add_handler(CommandHandler("realpnl", _realpnl_command))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()  # run forever alongside other coroutines

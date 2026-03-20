import asyncio

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, ConversationHandler, MessageHandler, filters

from src.chain.database import (
    add_wallet_manually,
    get_follow_only_wallets,
    get_follow_wallet_open_positions,
    get_pnl_summary,
    get_real_pnl_summary,
    get_recent_super_alerts,
    get_wallet_alias,
    get_wallets_latest_buy,
    save_super_alert,
    set_wallet_alias,
)
from src.clob import watched_wallets


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
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)  # deposit alert — with sound


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
    save_super_alert(wallet, market, outcome, price, trade_size, max_deposit, buys_on_market)
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    msg = await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)  # super alert — with sound
    await bot.pin_chat_message(chat_id=TELEGRAM_CHAT_ID, message_id=msg.message_id, disable_notification=True)


async def send_paper_buy(*args, **kwargs):
    pass  # paper notifications disabled


async def send_paper_sell(*args, **kwargs):
    pass  # paper notifications disabled


async def send_real_buy(
    followed_wallet: str,
    outcome: str,
    market: str,
    insider_price: float,
    fill_price: float,
    size_usdc: float,
    insider_size: float = 0.0,
    p_hat: float = 0.0,
):
    drift = (fill_price - insider_price) / insider_price * 100 if insider_price else 0
    score_str = f"{p_hat:.2f}" if p_hat else "n/a"
    message = (
        f"💸 REAL BUY\n"
        f"Copying: {followed_wallet}\n"
        f"Market: {market}\n"
        f"Outcome: {outcome}\n"
        f"Insider trade: {insider_size:.0f} USDC\n"
        f"Insider: {insider_price:.3f} → Fill: {fill_price:.3f} ({drift:+.1f}%)\n"
        f"Size: {size_usdc:.2f} USDC\n"
        f"Score (p̂): {score_str}"
    )
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, disable_notification=True)


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
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, disable_notification=True)


async def send_follow_alert(
    wallet: str,
    question: str,
    outcome: str,
    price: float,
    size: float,
):
    alias = get_wallet_alias(wallet)
    short = alias or f"{wallet[:6]}…{wallet[-4:]}"
    message = (
        f"👁 FOLLOWED WALLET\n"
        f"Wallet: {short}\n"
        f"Market: {question}\n"
        f"Side: BUY | {outcome} @ {price:.3f}\n"
        f"Size: {size:.0f} USDC"
    )
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, disable_notification=True)


async def send_cluster_alert(wallet: str, cluster: list[str]):
    all_wallets = [wallet] + cluster
    latest_buys = get_wallets_latest_buy(all_wallets)

    def _wallet_line(w: str) -> str:
        info = latest_buys.get(w.lower())
        if info:
            market_short = info["market"][:12]
            return f"  • {w}\n    └ {info['outcome']} @ {info['price']:.3f} | {info['size']:.0f} USDC | {market_short}…"
        return f"  • {w}"

    siblings = "\n".join(_wallet_line(w) for w in cluster)
    message = (
        f"Cluster Detected!\n"
        f"Wallet: {wallet}\n"
        f"Shares a funder with {len(cluster)} other suspect wallet(s):\n"
        f"{siblings}"
    )
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, disable_notification=True)


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


async def _superalerts_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    alerts = get_recent_super_alerts(limit=5)
    if not alerts:
        await update.message.reply_text("No super alerts recorded yet.")
        return
    lines = ["🚨 Last super alerts"]
    for a in alerts:
        ratio = (a["trade_size"] / a["max_deposit"] * 100) if a["max_deposit"] else 0
        wallet = a["wallet"]
        lines.append(
            f"\n{a['detected_at'][:16]}\n"
            f"Wallet: {wallet[:6]}…{wallet[-4:]}\n"
            f"{a['outcome']} @ {a['price']:.3f} | {a['trade_size']:.0f} USDC ({ratio:.1f}%)\n"
            f"Buys on market: {a['buys_on_market']}"
        )
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


_AWAITING_RENAME = 1
_AWAITING_ADD_ADDRESS = 2
_AWAITING_ADD_MODE = 3

_STATUS_LABELS = {"follow": "follow_only", "copy": "active", "watch": "watch_only"}


def _do_add_wallet(wallet: str, status: str, alias: str | None = None) -> str:
    """Insert wallet in DB and add to live watchlist. Returns confirmation message."""
    add_wallet_manually(wallet, status)
    if alias:
        set_wallet_alias(wallet, alias)
    watched_wallets.add(wallet)
    label = {"follow_only": "follow only", "active": "copy trade", "watch_only": "watch only"}.get(status, status)
    name = alias or f"{wallet[:6]}…{wallet[-4:]}"
    return f"✅ Added {name} as {label}"


def _wallet_label(w: dict) -> str:
    return w["alias"] or f"{w['wallet'][:6]}…{w['wallet'][-4:]}"


async def _add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/add <address> [follow|copy|watch] ["name"]"""
    args = context.args or []
    if not args:
        await update.message.reply_text('Usage: /add <address> [follow|copy|watch] ["name"]\nDefault mode: follow')
        return
    wallet = args[0].strip()
    if not wallet.startswith("0x") or len(wallet) < 10:
        await update.message.reply_text("❌ Invalid address.")
        return
    rest = args[1:]
    mode = "follow"
    if rest and rest[0].lower() in _STATUS_LABELS:
        mode = rest[0].lower()
        alias_parts = rest[1:]
    else:
        alias_parts = rest
    alias = " ".join(alias_parts).strip('"\'') or None
    status = _STATUS_LABELS.get(mode, "follow_only")
    await update.message.reply_text(_do_add_wallet(wallet, status, alias))


def _followed_keyboard(wallets: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(_wallet_label(w), callback_data=f"fw:{w['wallet']}")] for w in wallets]
    rows.append([InlineKeyboardButton("➕ Add wallet", callback_data="fw_add")])
    return InlineKeyboardMarkup(rows)


async def _followed_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    wallets = get_follow_only_wallets()
    await update.message.reply_text("👁 Followed wallets:", reply_markup=_followed_keyboard(wallets))


async def _fw_wallet_cb(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    wallet = q.data.split(":", 1)[1]
    short = f"{wallet[:6]}…{wallet[-4:]}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Positions", callback_data=f"fw_pos:{wallet}")],
        [InlineKeyboardButton("✏️ Rename",    callback_data=f"fw_rename:{wallet}")],
        [InlineKeyboardButton("« Back",       callback_data="fw_back")],
    ])
    await q.edit_message_text(f"Wallet: {short}", reply_markup=keyboard)


async def _fw_positions_cb(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    wallet = q.data.split(":", 1)[1]
    positions = get_follow_wallet_open_positions(wallet)
    if not positions:
        await q.edit_message_text("No open positions.")
        return
    lines = [f"📊 Open positions — {wallet[:6]}…{wallet[-4:]}"]
    for p in positions:
        lines.append(
            f"\n{p['outcome']} @ {p['price']:.3f} | {p['size']:.0f} USDC\n"
            f"Market: {p['market'][:24]}…"
        )
    await q.edit_message_text("\n".join(lines))


async def _fw_rename_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    wallet = q.data.split(":", 1)[1]
    context.user_data["renaming_wallet"] = wallet
    await q.edit_message_text(f"Send the new name for {wallet[:6]}…{wallet[-4:]}:")
    return _AWAITING_RENAME


async def _fw_rename_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    wallet = context.user_data.pop("renaming_wallet", None)
    if not wallet:
        return ConversationHandler.END
    alias = update.message.text.strip()
    set_wallet_alias(wallet, alias)
    await update.message.reply_text(f"✅ Renamed to '{alias}'")
    return ConversationHandler.END


async def _fw_add_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    context.user_data["add_msg_id"] = q.message.message_id
    await q.edit_message_text("Send the wallet address to add:")
    return _AWAITING_ADD_ADDRESS


async def _fw_add_address_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    wallet = update.message.text.strip()
    if not wallet.startswith("0x") or len(wallet) < 10:
        await update.message.reply_text("❌ Invalid address. Try again:")
        return _AWAITING_ADD_ADDRESS
    context.user_data["adding_wallet"] = wallet
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("👁 Follow",  callback_data="fw_mode:follow_only"),
        InlineKeyboardButton("📋 Copy",    callback_data="fw_mode:active"),
        InlineKeyboardButton("🔭 Watch",   callback_data="fw_mode:watch_only"),
    ]])
    await update.message.reply_text(f"Mode for {wallet[:6]}…{wallet[-4:]}?", reply_markup=keyboard)
    return _AWAITING_ADD_MODE


async def _fw_add_mode_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    wallet = context.user_data.pop("adding_wallet", None)
    if not wallet:
        return ConversationHandler.END
    status = q.data.split(":", 1)[1]
    await q.edit_message_text(_do_add_wallet(wallet, status))
    return ConversationHandler.END


async def _fw_back_cb(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    wallets = get_follow_only_wallets()
    await q.edit_message_text("👁 Followed wallets:", reply_markup=_followed_keyboard(wallets))


async def run_bot() -> None:
    """Start the Telegram bot and listen for commands."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("pnl", _pnl_command))
    app.add_handler(CommandHandler("realpnl", _realpnl_command))
    app.add_handler(CommandHandler("superalerts", _superalerts_command))
    app.add_handler(CommandHandler("add", _add_command))
    app.add_handler(CommandHandler("followed", _followed_command))
    app.add_handler(CallbackQueryHandler(_fw_wallet_cb,    pattern=r"^fw:[^_]"))
    app.add_handler(CallbackQueryHandler(_fw_positions_cb, pattern=r"^fw_pos:"))
    app.add_handler(CallbackQueryHandler(_fw_back_cb,      pattern=r"^fw_back$"))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(_fw_rename_cb, pattern=r"^fw_rename:")],
        states={_AWAITING_RENAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, _fw_rename_received)]},
        fallbacks=[],
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(_fw_add_cb, pattern=r"^fw_add$")],
        states={
            _AWAITING_ADD_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, _fw_add_address_received)],
            _AWAITING_ADD_MODE:    [CallbackQueryHandler(_fw_add_mode_cb, pattern=r"^fw_mode:")],
        },
        fallbacks=[],
    ))
    await app.initialize()
    await app.bot.set_my_commands([
        ("pnl",         "Paper trading P&L summary"),
        ("realpnl",     "Real trading P&L summary"),
        ("superalerts", "Last 5 super alerts"),
        ("followed",    "Manage followed wallets"),
        ("add",         "Add a wallet: /add <address> [follow|copy|watch]"),
    ])
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()  # run forever alongside other coroutines

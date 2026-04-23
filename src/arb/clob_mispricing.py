"""
CLOB Mispricing Arbitrage

Detects stale limit orders priced significantly below the current best bid.
Strategy: buy the mispriced ask, immediately sell at best bid for instant profit.

Unlike post-resolution arb (triggered by on-chain CTF event), this runs 24/7
across all active markets by monitoring real-time orderbook WS updates.

Thresholds (configurable via .env):
  CLOB_MISPRICING_BID_RATIO  (default 0.80): ask < best_bid * ratio
  CLOB_MISPRICING_MIN_SPREAD (default 0.10): ask < best_bid - spread
  Both conditions must hold simultaneously.
"""

import asyncio
import time as _time

from telegram import Bot

from config.settings import (
    ARB_CAPITAL,
    CLOB_MISPRICING_ENABLED,
    CLOB_MISPRICING_MAX_TRADE,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from src.arb.clob_ws import get_mispricing_queue

_bot: Bot | None = None


def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return _bot


async def run_clob_mispricing() -> None:
    """
    Consume mispricing events from the clob_ws queue.
    Dry-run: sends a Telegram alert.
    Live:    calls src/trading/clob_mispricing_executor.py (VPS-only, not committed).
    """
    queue = get_mispricing_queue()

    while True:
        event = await queue.get()
        t_consume = _time.monotonic()

        token_id   = event["token_id"]
        ask_price  = event["ask_price"]
        ask_size   = event["ask_size"]
        best_bid   = event["best_bid"]
        t_detect   = event.get("t_detect", t_consume)
        t_queued   = event.get("t_queued", t_consume)
        print(
            f"[mispricing] ⏱ queue_wait={1000*(t_consume-t_queued):.0f}ms "
            f"(total detect→consume={1000*(t_consume-t_detect):.0f}ms)"
        )

        spread     = best_bid - ask_price
        spread_pct = spread / best_bid * 100

        # Sizing: aim to fill the entire order, capped by available capital.
        # CLOB_MISPRICING_MAX_TRADE acts as a safety cap during initial deployment.
        cost_to_fill    = ask_size * ask_price
        budget          = min(cost_to_fill, ARB_CAPITAL, CLOB_MISPRICING_MAX_TRADE)
        max_shares      = budget / ask_price
        cost_usdc       = max_shares * ask_price
        profit_est_usdc = max_shares * spread

        print(
            f"[mispricing] ask={ask_price:.3f} bid={best_bid:.3f} "
            f"spread={spread:.3f} ({spread_pct:.0f}%) | {token_id[:14]}... "
            f"| cost={cost_usdc:.0f} USDC profit~{profit_est_usdc:.0f} USDC"
        )

        if not CLOB_MISPRICING_ENABLED:
            msg = (
                f"📊 CLOB MISPRICING (dry run)\n"
                f"Token: {token_id[:24]}...\n"
                f"Buy at: {ask_price:.3f} | Sell at: {best_bid:.3f}\n"
                f"Spread: {spread:.3f} ({spread_pct:.0f}%)\n"
                f"Shares available: {ask_size:.0f}\n"
                f"Cost: {cost_usdc:.0f} USDC | Profit est.: {profit_est_usdc:.0f} USDC"
            )
            try:
                await _get_bot().send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
            except Exception as e:
                print(f"[mispricing] Telegram error: {e}")
            continue

        try:
            from src.trading.clob_mispricing_executor import execute_mispricing  # VPS-only
            await execute_mispricing(
                token_id=token_id,
                ask_price=ask_price,
                ask_size=ask_size,
                best_bid=best_bid,
                max_trade_usdc=CLOB_MISPRICING_MAX_TRADE,
                t_detect=t_detect,
            )
        except ImportError:
            print("[mispricing] executor not available — deploy src/trading/clob_mispricing_executor.py")

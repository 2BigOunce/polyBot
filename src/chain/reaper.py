import asyncio
from datetime import datetime, timedelta, timezone

from config.settings import WALLET_INACTIVITY_HOURS
from src.chain.database import get_active_wallets_flagged_before, has_traded, mark_wallet_status
from src.clob import watched_wallets

REAPER_INTERVAL = 3600  # run every hour


async def reaper():
    """
    Periodically removes wallets from the watchlist if they haven't traded
    within WALLET_INACTIVITY_HOURS after being flagged.
    """
    print("[reaper] Started")
    while True:
        await asyncio.sleep(REAPER_INTERVAL)

        cutoff = (datetime.now(tz=timezone.utc) - timedelta(hours=WALLET_INACTIVITY_HOURS)).isoformat()
        stale = get_active_wallets_flagged_before(cutoff)

        for wallet in stale:
            if not has_traded(wallet):
                watched_wallets.watched.discard(wallet)
                mark_wallet_status(wallet, "no_activity")
                print(f"[reaper] {wallet} removed — no trade in {WALLET_INACTIVITY_HOURS}h")

        if stale:
            print(f"[reaper] Checked {len(stale)} wallets, cycle complete")

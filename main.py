import asyncio

from src.alerts.telegram import run_bot
from src.arb.clob_ws import run_clob_ws
from src.arb.market_cache import run_market_cache
from src.arb.resolution_monitor import run_resolution_monitor
from src.chain.deposit_monitor import monitor_deposits
from src.chain.reaper import reaper
from src.clob.onchain_tracker import run_onchain_watcher
from src.clob.tracker import run_tracker
from src.trading.position_closer import position_closer


async def main():
    await asyncio.gather(
        monitor_deposits(),
        run_tracker(),
        run_onchain_watcher(),
        run_resolution_monitor(),
        run_clob_ws(),
        run_market_cache(),
        reaper(),
        position_closer(),
        run_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())

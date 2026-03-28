import asyncio

from src.alerts.telegram import run_bot
from src.arb.clob_mispricing import run_clob_mispricing
from src.arb.clob_ws import run_clob_ws
from src.arb.market_cache import run_market_cache
from src.arb.resolution_monitor import run_resolution_monitor
from src.chain.deposit_monitor import monitor_deposits
from src.chain.reaper import reaper
from src.clob.onchain_tracker import run_onchain_watcher
from src.clob.tracker import run_tracker
from src.trading.position_closer import position_closer

try:
    from src.trading.maker_positioning import run_maker_positioning
    _HAS_MAKER = True
except ImportError:
    _HAS_MAKER = False


async def main():
    tasks = [
        monitor_deposits(),
        run_tracker(),
        run_onchain_watcher(),
        run_resolution_monitor(),
        run_clob_ws(),
        run_market_cache(),
        run_clob_mispricing(),
        reaper(),
        position_closer(),
        run_bot(),
    ]
    if _HAS_MAKER:
        tasks.append(run_maker_positioning())
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())

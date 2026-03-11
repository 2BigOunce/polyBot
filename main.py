import asyncio

from src.alerts.telegram import run_bot
from src.chain.deposit_monitor import monitor_deposits
from src.chain.reaper import reaper
from src.clob.tracker import run_tracker
from src.trading.position_closer import position_closer


async def main():
    await asyncio.gather(
        monitor_deposits(),
        run_tracker(),
        reaper(),
        position_closer(),
        run_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())

import asyncio

from src.chain.deposit_monitor import monitor_deposits
from src.clob.tracker import run_tracker


async def main():
    await asyncio.gather(
        monitor_deposits(),
        run_tracker(),
    )


if __name__ == "__main__":
    asyncio.run(main())

import aiohttp

from config.settings import POLYGONSCAN_API_KEY
from src.chain.wallet_age import POLYGONSCAN_API, POLYGON_CHAIN_ID, _sem, _MIN_INTERVAL
import asyncio


async def get_funders(wallet: str, session: aiohttp.ClientSession) -> list[str]:
    """
    Return a list of unique addresses that sent MATIC to wallet.
    Uses Etherscan V2 API with chainid=137 (Polygon).
    """
    params = {
        "chainid": POLYGON_CHAIN_ID,
        "module":  "account",
        "action":  "txlist",
        "address": wallet,
        "sort":    "asc",
        "apikey":  POLYGONSCAN_API_KEY,
    }

    async with _sem:
        try:
            async with session.get(POLYGONSCAN_API, params=params) as resp:
                data = await resp.json()
        except Exception:
            return []
        finally:
            await asyncio.sleep(_MIN_INTERVAL)

    if data.get("status") != "1":
        return []

    funders = set()
    for tx in data["result"]:
        if tx["to"].lower() == wallet.lower() and tx["from"] != wallet:
            funders.add(tx["from"])

    return list(funders)

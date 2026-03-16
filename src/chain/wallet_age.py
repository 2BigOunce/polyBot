import asyncio
import aiohttp
from datetime import datetime, timezone

from config.settings import POLYGONSCAN_API_KEY

POLYGONSCAN_API = "https://api.polygonscan.com/api"

# Polygonscan free tier: 5 req/s — enforce at most 3/s to stay safe
_sem = asyncio.Semaphore(1)
_MIN_INTERVAL = 0.35  # seconds between releases (~3 req/s)


async def get_wallet_age(address: str, session: aiohttp.ClientSession) -> datetime | None:
    """
    Return the datetime of the wallet's first transaction on Polygon.
    Uses Polygonscan REST API — no archive node required.

    Returns:
        datetime  → first transaction timestamp (wallet exists on-chain)
        None      → wallet has no transactions yet (genuinely brand-new)

    Raises:
        LookupError  → API rate-limited or unreachable; caller should skip this deposit
    """
    params = {
        "module":  "account",
        "action":  "txlist",
        "address": address,
        "sort":    "asc",
        "page":    1,
        "offset":  1,   # only the first transaction
        "apikey":  POLYGONSCAN_API_KEY,
    }
    async with _sem:
        try:
            async with session.get(POLYGONSCAN_API, params=params) as resp:
                data = await resp.json()

            msg = data.get("message", "")

            if data.get("status") == "1" and data.get("result"):
                timestamp = int(data["result"][0]["timeStamp"])
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)

            if msg == "No transactions found":
                return None  # genuinely brand-new wallet — age 0 is correct

            # Anything else (NOTOK, rate limit, etc.) is an API error
            raise LookupError(f"Polygonscan: {msg!r}")

        except LookupError:
            raise
        except Exception as e:
            raise LookupError(f"Polygonscan request failed: {e}") from e
        finally:
            await asyncio.sleep(_MIN_INTERVAL)

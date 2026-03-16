import asyncio
import aiohttp
from datetime import datetime, timezone

from config.settings import HTTP_RPC_URL

# Polygon produces ~43,200 blocks per day (2s block time)
_BLOCKS_PER_DAY = 43_200

# Kept for backward-compatibility: funders.py imports these constants
POLYGONSCAN_API = None
POLYGON_CHAIN_ID = 137

_sem = asyncio.Semaphore(3)
_MIN_INTERVAL = 0.0  # no rate limit needed for RPC calls


async def _rpc(session: aiohttp.ClientSession, method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(HTTP_RPC_URL, json=payload) as resp:
        data = await resp.json()
    if "error" in data:
        raise LookupError(f"RPC error: {data['error']}")
    return data["result"]


async def get_wallet_age(address: str, session: aiohttp.ClientSession) -> datetime | None:
    """
    Estimate wallet age using threshold checks on Polygon RPC.
    No API key required — uses PublicNode HTTP RPC.

    Checks eth_getTransactionCount at 3 historical blocks (1d, 7d, 90d ago)
    to classify the wallet into an age bucket.

    Returns:
        datetime  → approximate first-activity timestamp
        None      → wallet has no outgoing transactions (brand-new or receive-only)

    Raises:
        LookupError  → RPC unreachable or query failed
    """
    async with _sem:
        try:
            current_hex = await _rpc(session, "eth_blockNumber", [])
            current_block = int(current_hex, 16)

            nonce_latest = int(await _rpc(session, "eth_getTransactionCount", [address, "latest"]), 16)
            if nonce_latest == 0:
                return None  # no outgoing transactions — treat as brand-new

            # Check thresholds from oldest to newest — stop at first match
            # age_days will be the lower bound of the age bucket found
            age_days = 0
            for days in (90, 7, 1):
                block = current_block - days * _BLOCKS_PER_DAY
                if block < 1:
                    continue
                nonce = int(await _rpc(session, "eth_getTransactionCount", [address, hex(block)]), 16)
                if nonce > 0:
                    age_days = days
                    break

            approx_ts = datetime.now(tz=timezone.utc).timestamp() - age_days * 86_400
            return datetime.fromtimestamp(approx_ts, tz=timezone.utc)

        except LookupError:
            raise
        except Exception as e:
            raise LookupError(f"RPC wallet age failed: {e}") from e

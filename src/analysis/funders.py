import aiohttp

# Etherscan V2 API is deprecated for Polygonscan keys.
# Funder lookup is disabled — cluster detection falls back to empty funder list.
# TODO: replace with RPC-based log scan when a suitable archive endpoint is available.


async def get_funders(_wallet: str, _session: aiohttp.ClientSession) -> list[str]:
    """
    Return a list of unique addresses that funded this wallet.
    Currently disabled (Etherscan API deprecated) — returns empty list.
    Cluster detection will not trigger until this is re-implemented.
    """
    return []

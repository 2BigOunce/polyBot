from web3 import AsyncWeb3
from datetime import datetime, timezone


async def get_wallet_age(w3: AsyncWeb3, address: str) -> datetime | None:
    address = w3.to_checksum_address(address)
    current_block = await w3.eth.block_number

    # If nonce is 0 at current block, the wallet has never sent a transaction
    current_nonce = await w3.eth.get_transaction_count(address, block_identifier=current_block)
    if current_nonce == 0:
        return None

    low = 0
    high = current_block

    while low < high:
        mid = (low + high) // 2
        nonce_at_mid = await w3.eth.get_transaction_count(address, block_identifier=mid)

        if nonce_at_mid == 0:
            low = mid + 1
        else:
            high = mid

    block = await w3.eth.get_block(low)
    return datetime.fromtimestamp(block["timestamp"], tz=timezone.utc)

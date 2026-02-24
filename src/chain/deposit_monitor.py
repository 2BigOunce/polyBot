import asyncio
from datetime import datetime, timezone

from web3 import AsyncWeb3

from config.settings import (
    ALERT_DEPOSIT_HIGH,
    ALERT_DEPOSIT_MEDIUM,
    POLYMARKET_CONTRACTS,
    RPC_URL,
    USDC_ADDRESS,
    WALLET_AGE_HIGH,
    WALLET_AGE_MEDIUM,
)
from src.chain.database import init_db, save_deposit
from src.chain.wallet_age import get_wallet_age

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
USDC_DECIMALS = 6


def decode_transfer(log: dict) -> tuple[str, str, float, str]:
    sender = "0x" + log["topics"][1].hex()[-40:]
    recipient = "0x" + log["topics"][2].hex()[-40:]
    raw_amount = int(log["data"].hex(), 16)
    amount_usdc = raw_amount / (10 ** USDC_DECIMALS)
    tx_hash = log["transactionHash"].hex()
    return sender, recipient, amount_usdc, tx_hash


async def handle_deposit(w3: AsyncWeb3, sender: str, amount_usdc: float, tx_hash: str):
    sender = w3.to_checksum_address(sender)
    first_tx_date = await get_wallet_age(w3, sender)

    if first_tx_date is None:
        wallet_age_days = 0
    else:
        now = datetime.now(tz=timezone.utc)
        wallet_age_days = (now - first_tx_date).total_seconds() / 86400

    print(f"[deposit] {sender} | age: {wallet_age_days:.1f}d | amount: {amount_usdc:.2f} USDC")

    if amount_usdc >= ALERT_DEPOSIT_HIGH and wallet_age_days <= WALLET_AGE_HIGH:
        save_deposit(tx_hash, sender, amount_usdc, wallet_age_days, "HIGH")
        print("High potential insider alert")
    elif amount_usdc >= ALERT_DEPOSIT_MEDIUM and wallet_age_days <= WALLET_AGE_MEDIUM:
        save_deposit(tx_hash, sender, amount_usdc, wallet_age_days, "MEDIUM")
        print("MEDIUM Alert !")


async def monitor_deposits():
    init_db()
    w3 = AsyncWeb3(AsyncWeb3.WebSocketProvider(RPC_URL))

    log_filter = {
        "address": USDC_ADDRESS,
        "topics": [TRANSFER_TOPIC, None, POLYMARKET_CONTRACTS],
    }

    print("[monitor] Connected — listening for deposits...")

    async with w3.eth.subscribe("logs", log_filter) as subscription:
        async for log in subscription:
            sender, recipient, amount_usdc, tx_hash = decode_transfer(log)
            asyncio.create_task(handle_deposit(w3, sender, amount_usdc, tx_hash))


if __name__ == "__main__":
    asyncio.run(monitor_deposits())

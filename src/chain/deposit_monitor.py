import asyncio
import json
from datetime import datetime, timezone

import websockets
from web3 import AsyncWeb3
from web3.middleware import ExtraDataToPOAMiddleware

from config.settings import (
    ALERT_DEPOSIT_HIGH,
    ALERT_DEPOSIT_MEDIUM,
    POLYMARKET_CONTRACTS,
    RPC_URL,
    USDC_ADDRESS,
    WALLET_AGE_HIGH,
    WALLET_AGE_MEDIUM,
)
from src.alerts.telegram import send_alert
from src.chain.database import init_db, save_deposit
from src.chain.wallet_age import get_wallet_age

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
USDC_DECIMALS = 6

# Max concurrent wallet age lookups (each does ~26 RPC calls via binary search)
_semaphore = asyncio.Semaphore(3)


def pad_address(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:].lower()


def decode_transfer(log: dict) -> tuple[str, str, float, str]:
    # Raw websocket logs have plain hex strings (no .hex() needed)
    sender = "0x" + log["topics"][1][-40:]
    recipient = "0x" + log["topics"][2][-40:]
    raw_amount = int(log["data"], 16)
    amount_usdc = raw_amount / (10 ** USDC_DECIMALS)
    tx_hash = log["transactionHash"]
    return sender, recipient, amount_usdc, tx_hash


async def handle_deposit(w3: AsyncWeb3, sender: str, amount_usdc: float, tx_hash: str):
    async with _semaphore:
        try:
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
                await send_alert("HIGH", sender, amount_usdc, wallet_age_days, tx_hash)
            elif amount_usdc >= ALERT_DEPOSIT_MEDIUM and wallet_age_days <= WALLET_AGE_MEDIUM:
                save_deposit(tx_hash, sender, amount_usdc, wallet_age_days, "MEDIUM")
                await send_alert("MEDIUM", sender, amount_usdc, wallet_age_days, tx_hash)

        except Exception as e:
            print(f"[error] {sender} — {e}")


async def monitor_deposits():
    init_db()
    async with AsyncWeb3(AsyncWeb3.WebSocketProvider(RPC_URL)) as w3:
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        async with websockets.connect(RPC_URL) as ws:
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": ["logs", {
                    "address": USDC_ADDRESS,
                    "topics": [TRANSFER_TOPIC, None, [pad_address(a) for a in POLYMARKET_CONTRACTS]],
                }],
            }))

            resp = json.loads(await ws.recv())
            sub_id = resp["result"]
            print(f"[monitor] Connected — listening for deposits...")

            async for raw_msg in ws:
                msg = json.loads(raw_msg)
                if msg.get("method") != "eth_subscription":
                    continue
                if msg["params"]["subscription"] != sub_id:
                    continue
                log = msg["params"]["result"]
                sender, _, amount_usdc, tx_hash = decode_transfer(log)
                asyncio.create_task(handle_deposit(w3, sender, amount_usdc, tx_hash))


if __name__ == "__main__":
    asyncio.run(monitor_deposits())

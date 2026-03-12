import asyncio
import json
from datetime import datetime, timezone

import websockets
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
import aiohttp

from src.alerts.telegram import send_cluster_alert
from src.analysis.funders import get_funders
from src.analysis.graph import add_funder_edges, is_cluster_alert
from src.chain.database import init_db, save_deposit
from src.chain.wallet_age import get_wallet_age
from src.clob import watched_wallets

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
USDC_DECIMALS = 6

_semaphore = asyncio.Semaphore(3)
_age_cache: dict[str, float] = {}
_seen_tx: set[str] = set()


def pad_address(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:].lower()


def decode_transfer(log: dict) -> tuple[str, str, float, str]:
    # Raw websocket logs have plain hex strings (no .hex() needed)
    sender      = "0x" + log["topics"][1][-40:]
    recipient   = "0x" + log["topics"][2][-40:]
    raw_amount  = int(log["data"], 16)
    amount_usdc = raw_amount / (10 ** USDC_DECIMALS)
    tx_hash     = log["transactionHash"]
    return sender, recipient, amount_usdc, tx_hash


async def handle_deposit(
    session: aiohttp.ClientSession,
    sender: str,
    amount_usdc: float,
    tx_hash: str,
):
    async with _semaphore:
        try:
            sender = AsyncWeb3.to_checksum_address(sender)

            if sender in _age_cache:
                wallet_age_days = _age_cache[sender]
            else:
                try:
                    first_tx_date = await get_wallet_age(sender, session)
                except LookupError as e:
                    # Polygonscan rate-limited or unreachable — skip deposit, don't cache
                    print(f"[deposit] skipped {sender[:10]}… ({e})")
                    return
                if first_tx_date is None:
                    wallet_age_days = 0.0  # genuinely brand-new wallet
                else:
                    now = datetime.now(tz=timezone.utc)
                    wallet_age_days = (now - first_tx_date).total_seconds() / 86400
                _age_cache[sender] = wallet_age_days  # only cache on success

            print(f"[deposit] {sender} | age: {wallet_age_days:.1f}d | amount: {amount_usdc:.2f} USDC")

            flagged = False
            if amount_usdc >= ALERT_DEPOSIT_HIGH and wallet_age_days <= WALLET_AGE_HIGH:
                save_deposit(tx_hash, sender, amount_usdc, wallet_age_days, "HIGH")
                watched_wallets.add(sender)
                flagged = True
            elif amount_usdc >= ALERT_DEPOSIT_MEDIUM and wallet_age_days <= WALLET_AGE_MEDIUM:
                save_deposit(tx_hash, sender, amount_usdc, wallet_age_days, "MEDIUM")
                watched_wallets.add(sender)
                flagged = True

            if flagged:
                funders = await get_funders(sender, session)
                add_funder_edges(sender, funders)
                if is_cluster_alert(sender):
                    from src.analysis.graph import get_cluster
                    cluster = get_cluster(sender)
                    print(f"[cluster] {sender} belongs to a cluster of {len(cluster)+1}")
                    await send_cluster_alert(sender, cluster)

        except Exception as e:
            print(f"[error] {sender} — {e}")


async def monitor_deposits():
    init_db()
    session = aiohttp.ClientSession()

    retry_delay = 30

    while True:
        try:
            async with websockets.connect(RPC_URL) as ws:
                retry_delay = 30
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
                if "result" not in resp:
                    print(f"[monitor] Infura error: {resp}")
                    raise KeyError("result")
                sub_id = resp["result"]
                print("[monitor] Connected — listening for deposits...")

                async for raw_msg in ws:
                    msg = json.loads(raw_msg)
                    if msg.get("method") != "eth_subscription":
                        continue
                    if msg["params"]["subscription"] != sub_id:
                        continue
                    log = msg["params"]["result"]
                    sender, _, amount_usdc, tx_hash = decode_transfer(log)
                    if tx_hash in _seen_tx:
                        continue
                    _seen_tx.add(tx_hash)
                    asyncio.create_task(handle_deposit(session, sender, amount_usdc, tx_hash))

        except Exception as e:
            print(f"[monitor] Connection lost: {e}. Retrying in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    asyncio.run(monitor_deposits())

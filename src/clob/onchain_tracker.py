import asyncio
import json

import websockets
from web3 import AsyncWeb3

from config.settings import CTF_EXCHANGE, NEG_RISK_EXCHANGE, RPC_URL_TRACKER
from src.clob import watched_wallets

ORDER_FILLED_TOPIC = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"
CLOB_CONTRACTS    = [CTF_EXCHANGE, NEG_RISK_EXCHANGE]

# One asyncio.Event per watched wallet — set when an on-chain trade is detected
_wake_events: dict[str, asyncio.Event] = {}


def get_wake_event(wallet: str) -> asyncio.Event:
    """Return (and create if needed) the wake event for a wallet."""
    if wallet not in _wake_events:
        _wake_events[wallet] = asyncio.Event()
    return _wake_events[wallet]


async def run_onchain_watcher():
    """
    Subscribe to OrderFilled events on both Polymarket CLOB contracts via PublicNode WebSocket.
    When the maker address belongs to a watched wallet, wake up its poll_wallet() task immediately
    instead of waiting for the 30s polling interval.
    """
    retry_delay = 30

    while True:
        try:
            async with websockets.connect(RPC_URL_TRACKER) as ws:
                retry_delay = 30
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_subscribe",
                    "params": ["logs", {
                        "address": CLOB_CONTRACTS,
                        "topics":  [ORDER_FILLED_TOPIC],
                    }],
                }))

                resp = json.loads(await ws.recv())
                if "result" not in resp:
                    print(f"[onchain] Subscribe error: {resp}")
                    raise KeyError("result")

                sub_id = resp["result"]
                print("[onchain] Connected — listening for OrderFilled events...")

                async for raw_msg in ws:
                    msg = json.loads(raw_msg)
                    if msg.get("method") != "eth_subscription":
                        continue
                    if msg["params"]["subscription"] != sub_id:
                        continue

                    log    = msg["params"]["result"]
                    topics = log.get("topics", [])
                    if len(topics) < 3:
                        continue

                    # topics[2] = maker address (indexed), padded to 32 bytes
                    maker = AsyncWeb3.to_checksum_address("0x" + topics[2][-40:])

                    if maker in watched_wallets.watched:
                        print(f"[onchain] OrderFilled for {maker[:10]}… — waking poller")
                        get_wake_event(maker).set()

        except Exception as e:
            print(f"[onchain] Connection lost: {e}. Retrying in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

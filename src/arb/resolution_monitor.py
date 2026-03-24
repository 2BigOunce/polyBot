"""
Resolution Arbitrage — Phase 6: Resolution Monitor

Subscribes to ConditionResolution events on the Gnosis CTF contract (Polygon).
When a market resolves, immediately triggers the arb scanner to find stale orders.

Works for both standard CTF markets and NegRisk markets (both resolve via CTF).
"""

import asyncio

import orjson

import websockets
from web3 import Web3

from config.settings import GNOSIS_CTF, NEG_RISK_ADAPTER, RPC_URL
from src.arb.arb_scanner import scan_resolved_market

# keccak256("ConditionResolution(address,bytes32,bytes32,uint256,uint256[])")
CONDITION_RESOLUTION_TOPIC = Web3.to_hex(Web3.keccak(
    text="ConditionResolution(address,bytes32,bytes32,uint256,uint256[])"
))


def _decode_resolution(log: dict) -> tuple[str, list[int]] | None:
    """
    Decode a ConditionResolution log into (conditionId, payoutNumerators).

    Log structure:
        topics[0] = event signature
        topics[1] = oracle address (indexed)
        topics[2] = questionId (indexed, bytes32)
        topics[3] = conditionId (indexed, bytes32)
        data      = abi-encoded (uint256 outcomeSlotCount, uint256[] payoutNumerators)

    ABI layout of data:
        [0:32]    = outcomeSlotCount
        [32:64]   = offset to payoutNumerators array (always 64 = 0x40)
        [64:96]   = length of payoutNumerators
        [96:...]  = payoutNumerators values (32 bytes each)
    """
    try:
        topics = log.get("topics", [])
        if len(topics) < 4:
            return None

        condition_id = topics[3]  # 0x-prefixed bytes32 hex string

        data = bytes.fromhex(log["data"].lstrip("0x"))
        if len(data) < 96:
            return None

        arr_length = int.from_bytes(data[64:96], "big")
        payouts = [
            int.from_bytes(data[96 + i * 32: 96 + (i + 1) * 32], "big")
            for i in range(arr_length)
        ]

        return condition_id, payouts

    except Exception as e:
        print(f"[arb] decode error: {e}")
        return None


async def run_resolution_monitor():
    """
    Main coroutine: subscribe to CTF ConditionResolution events via WebSocket.
    On each resolution, spawn a scan task to look for stale orders.
    """
    retry_delay = 5

    while True:
        try:
            async with websockets.connect(
                RPC_URL,
                ping_interval=10,
                ping_timeout=5,
            ) as ws:
                retry_delay = 5

                await ws.send(orjson.dumps({
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "eth_subscribe",
                    "params": ["logs", {
                        "address": [GNOSIS_CTF, NEG_RISK_ADAPTER],
                        "topics":  [CONDITION_RESOLUTION_TOPIC],
                    }],
                }))

                resp = orjson.loads(await ws.recv())
                if "result" not in resp:
                    print(f"[arb] Subscribe error: {resp}")
                    raise KeyError("result")

                print("[arb] Connected — listening for ConditionResolution events...")

                async for raw_msg in ws:
                    msg = orjson.loads(raw_msg)
                    if msg.get("method") != "eth_subscription":
                        continue

                    log = msg["params"]["result"]
                    result = _decode_resolution(log)
                    if result is None:
                        continue

                    condition_id, payouts = result

                    if not payouts:
                        continue

                    # Safety: reject ambiguous resolutions (multiple winners)
                    # A valid resolution has exactly one non-zero payout
                    winners = [i for i, p in enumerate(payouts) if p > 0]
                    if len(winners) != 1:
                        print(f"[arb] ⚠️  ambiguous payouts {payouts} — skipping {condition_id[:14]}…")
                        continue

                    winner_index = winners[0]

                    print(f"[arb] Resolution — {condition_id[:14]}… winner_index={winner_index} (of {len(payouts)})")
                    asyncio.create_task(scan_resolved_market(condition_id, winner_index))

        except Exception as e:
            print(f"[arb] Connection lost: {e}. Retrying in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)

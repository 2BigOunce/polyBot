"""
Resolution Arbitrage — Phase 6: Resolution Monitor

Subscribes to two resolution events on Polygon:
  1. ConditionResolution on Gnosis CTF  → standard binary markets
  2. OutcomeReported    on NegRisk adapter → NegRisk multi-outcome markets

When a market resolves, immediately triggers the arb scanner to find stale orders.
"""

import asyncio

import orjson

import websockets
from web3 import Web3

from config.settings import GNOSIS_CTF, NEG_RISK_ADAPTER, RPC_URL
from src.arb.arb_scanner import scan_resolved_market

try:
    from src.trading.maker_positioning import on_market_resolved as _maker_on_resolved
    def _notify_maker(condition_id: str, winner_index: int) -> None:
        asyncio.create_task(_maker_on_resolved(condition_id, winner_index))
except ImportError:
    def _notify_maker(condition_id: str, winner_index: int) -> None:
        pass

# keccak256("ConditionResolution(bytes32,address,bytes32,uint256,uint256[])")
# Standard Gnosis CTF: conditionId (bytes32) indexed first, then oracle (address), then questionId (bytes32)
CONDITION_RESOLUTION_TOPIC = Web3.to_hex(Web3.keccak(
    text="ConditionResolution(bytes32,address,bytes32,uint256,uint256[])"
))

# keccak256("OutcomeReported(bytes32,bytes32,bool)")
# NegRisk adapter: marketId (bytes32 indexed), questionId (bytes32 indexed), outcome (bool)
OUTCOME_REPORTED_TOPIC = Web3.to_hex(Web3.keccak(
    text="OutcomeReported(bytes32,bytes32,bool)"
))

# Pre-computed bytes for conditionId derivation from NegRisk questionId
# Formula: keccak256(abi.encodePacked(NEG_RISK_ADAPTER, questionId, uint256(2)))
_ADAPTER_BYTES = bytes.fromhex(NEG_RISK_ADAPTER[2:])   # 20 bytes, no checksum needed
_SLOT_COUNT_2  = (2).to_bytes(32, "big")               # uint256(2)


def _negrisk_condition_id(question_id_hex: str) -> str:
    """Derive the CTF conditionId from a NegRisk questionId.

    Mirrors NegRiskAdapter.getConditionId():
        keccak256(abi.encodePacked(address(this), questionId, uint256(2)))
    """
    q_bytes = bytes.fromhex(question_id_hex[2:])
    return "0x" + Web3.keccak(_ADAPTER_BYTES + q_bytes + _SLOT_COUNT_2).hex()


def _decode_outcome_reported(log: dict) -> tuple[str, int] | None:
    """
    Decode an OutcomeReported log from the NegRisk adapter.

    Event: OutcomeReported(bytes32 indexed marketId, bytes32 indexed questionId, bool outcome)
        topics[0] = event signature
        topics[1] = marketId  (indexed)
        topics[2] = questionId (indexed)
        data      = abi-encoded bool (32 bytes, last byte = 0 or 1)

    Returns (condition_id, winner_index):
        outcome=True  (YES wins) → winner_index=0
        outcome=False (NO wins)  → winner_index=1
    """
    try:
        topics = log.get("topics", [])
        if len(topics) < 3:
            return None

        question_id_hex = topics[2]  # 0x-prefixed bytes32
        condition_id    = _negrisk_condition_id(question_id_hex)

        data    = bytes.fromhex(log["data"][2:])
        outcome = bool(data[-1])           # last byte of 32-byte bool encoding
        winner_index = 0 if outcome else 1  # True=YES wins(0), False=NO wins(1)

        return condition_id, winner_index

    except Exception as e:
        print(f"[arb] decode OutcomeReported error: {e}")
        return None


def _decode_resolution(log: dict) -> tuple[str, list[int]] | None:
    """
    Decode a ConditionResolution log into (conditionId, payoutNumerators).

    Log structure (standard Gnosis CTF event order):
        topics[0] = event signature
        topics[1] = conditionId (indexed, bytes32)
        topics[2] = oracle address (indexed)
        topics[3] = questionId (indexed, bytes32)
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

        condition_id = topics[1]  # 0x-prefixed bytes32 hex string — conditionId is first indexed param

        data = bytes.fromhex(log["data"][2:])
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
                open_timeout=15,
            ) as ws:
                retry_delay = 5

                # Subscribe to both resolution events in a single filter (OR on topics[0]).
                # ConditionResolution is emitted by GNOSIS_CTF (standard markets).
                # OutcomeReported    is emitted by NEG_RISK_ADAPTER (NegRisk markets).
                await ws.send(orjson.dumps({
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "eth_subscribe",
                    "params": ["logs", {
                        "address": [GNOSIS_CTF, NEG_RISK_ADAPTER],
                        "topics":  [[CONDITION_RESOLUTION_TOPIC, OUTCOME_REPORTED_TOPIC]],
                    }],
                }))

                try:
                    resp = orjson.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                except asyncio.TimeoutError:
                    print("[arb] Subscription handshake timeout — retrying...")
                    raise ConnectionError("handshake timeout")
                if "result" not in resp:
                    print(f"[arb] Subscribe error: {resp}")
                    raise KeyError("result")

                print("[arb] Connected — listening for ConditionResolution + OutcomeReported events...")

                async for raw_msg in ws:
                    msg = orjson.loads(raw_msg)
                    if msg.get("method") != "eth_subscription":
                        continue

                    log    = msg["params"]["result"]
                    topic0 = (log.get("topics") or [""])[0]

                    if topic0 == OUTCOME_REPORTED_TOPIC:
                        # NegRisk market resolved
                        result = _decode_outcome_reported(log)
                        if result is None:
                            continue
                        condition_id, winner_index = result
                        print(f"[arb] OutcomeReported — {condition_id[:14]}… winner_index={winner_index}")
                        asyncio.create_task(scan_resolved_market(condition_id, winner_index))
                        _notify_maker(condition_id, winner_index)

                    elif topic0 == CONDITION_RESOLUTION_TOPIC:
                        # Standard CTF market resolved
                        result = _decode_resolution(log)
                        if result is None:
                            continue
                        condition_id, payouts = result
                        if not payouts:
                            continue
                        winners = [i for i, p in enumerate(payouts) if p > 0]
                        if len(winners) != 1:
                            print(f"[arb] ambiguous payouts {payouts} — skipping {condition_id[:14]}…")
                            continue
                        winner_index = winners[0]
                        print(f"[arb] ConditionResolution — {condition_id[:14]}… winner_index={winner_index} (of {len(payouts)})")
                        asyncio.create_task(scan_resolved_market(condition_id, winner_index))
                        _notify_maker(condition_id, winner_index)

        except Exception as e:
            print(f"[arb] Connection lost: {e}. Retrying in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)

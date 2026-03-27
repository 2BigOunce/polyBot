"""
Analyse timing precis : OutcomeReported -> BUY des bots arb

Pour chaque trade arb connu (BUY a prix bas), on recupere :
  1. Le block timestamp du BUY (via TX hash)
  2. Le OutcomeReported le plus proche avant ce block (via eth_getLogs sur ~500 blocs)
  3. Le delta en secondes

Beaucoup plus rapide que de scanner tous les blocs.
"""

import json
import time
import datetime
import urllib.request
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
    RPC = os.getenv("RPC_HTTP_URL", "https://polygon-bor-rpc.publicnode.com")
except Exception:
    RPC = "https://polygon-bor-rpc.publicnode.com"

DATA_API         = "https://data-api.polymarket.com"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
GNOSIS_CTF       = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

OUTCOME_REPORTED_TOPIC    = "0x9e9fa7fd355160bd4cd3f22d4333519354beff1f5689bde87f2c5e63d8d484b2"
CONDITION_RESOLUTION_TOPIC = "0xb44d84d3289691f71497564b85d4233648d9dbae8cbdbb4329f301c3a0185894"

BOTS = {
    "desaru":     "0x99558e8d51b707069ddd64e8583c699bda94e3e6",
    "notprivate": "0xD630400ea7D818a9A598F0db7E42bE7C4F08CE18",
    "0x199f9e":   "0x199f9e490b812d977c8549c993e2ce0cccfb3d76",
    "0x6f1bf8":   "0x6f1bf845ca86b8fa14027c0e93ea44509a13262a",
}

ARB_PRICE_THRESHOLD = 0.40


def ts_str(ts):
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def http_get(url, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "polyBot-analyzer/2.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.request.HTTPError as e:
            if e.code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            return {"error": e.code}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                return {"error": str(e)}
    return {"error": "max retries"}


def rpc_call(method, params):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=payload, headers={
        "Content-Type": "application/json",
        "User-Agent": "polyBot-analyzer/2.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("result")
    except Exception:
        return None


def get_tx_block(tx_hash):
    receipt = rpc_call("eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        return None, None
    block_hex = receipt["blockNumber"]
    block = rpc_call("eth_getBlockByNumber", [block_hex, False])
    if not block:
        return None, None
    return int(block_hex, 16), int(block["timestamp"], 16)


def get_resolution_before(block_num, search_window=600):
    """Find the most recent OutcomeReported or ConditionResolution before block_num."""
    from_block = max(0, block_num - search_window)

    # Try OutcomeReported on NegRisk adapter
    logs = rpc_call("eth_getLogs", [{
        "fromBlock": hex(from_block),
        "toBlock":   hex(block_num),
        "address":   NEG_RISK_ADAPTER,
        "topics":    [OUTCOME_REPORTED_TOPIC],
    }]) or []

    # Also try ConditionResolution on CTF
    logs2 = rpc_call("eth_getLogs", [{
        "fromBlock": hex(from_block),
        "toBlock":   hex(block_num),
        "address":   GNOSIS_CTF,
        "topics":    [CONDITION_RESOLUTION_TOPIC],
    }]) or []

    all_logs = logs + logs2
    if not all_logs:
        return None, None, None

    # Pick the one closest (but before) block_num
    best = max(all_logs, key=lambda l: int(l["blockNumber"], 16))
    res_block_num = int(best["blockNumber"], 16)
    res_block = rpc_call("eth_getBlockByNumber", [best["blockNumber"], False])
    if not res_block:
        return None, None, None

    res_ts = int(res_block["timestamp"], 16)
    event_type = "OutcomeReported" if best.get("address", "").lower() == NEG_RISK_ADAPTER.lower() else "ConditionResolution"
    return res_block_num, res_ts, event_type


def fetch_arb_trades(wallet, max_pages=30):
    trades = []
    limit, offset = 100, 0
    for _ in range(max_pages):
        url  = f"{DATA_API}/activity?user={wallet}&limit={limit}&offset={offset}"
        data = http_get(url)
        if not isinstance(data, list) or not data:
            break
        trades.extend(data)
        if len(data) < limit:
            break
        offset += limit
        time.sleep(0.3)
    return [t for t in trades
            if t.get("side") == "BUY"
            and float(t.get("price", 1.0)) < ARB_PRICE_THRESHOLD
            and t.get("transactionHash")]


def analyze_timing(name, wallet):
    print(f"\n{'='*65}")
    print(f"BOT: {name}")
    print(f"{'='*65}")

    arb_trades = fetch_arb_trades(wallet)
    print(f"  Arb BUY trades: {len(arb_trades)}")
    if not arb_trades:
        return

    deltas = []
    print(f"\n  {'Date':19s} | {'Price':5s} | {'USDC':6s} | {'Event':20s} | Delta")
    print(f"  {'-'*75}")

    for t in arb_trades:
        tx   = t["transactionHash"]
        price = float(t["price"])
        size  = float(t.get("usdcSize", 0))
        clob_ts = t.get("timestamp", 0)

        block_num, block_ts = get_tx_block(tx)
        if not block_num:
            print(f"  {ts_str(clob_ts):19s} | {price:.3f} | {size:6.0f} | TX not found")
            continue

        res_block, res_ts, event_type = get_resolution_before(block_num)
        if not res_ts:
            print(f"  {ts_str(block_ts):19s} | {price:.3f} | {size:6.0f} | no resolution found")
            continue

        delta = block_ts - res_ts
        deltas.append(delta)
        print(f"  {ts_str(block_ts):19s} | {price:.3f} | {size:6.0f} | {event_type:20s} | {delta}s")

        time.sleep(0.15)

    if deltas:
        deltas.sort()
        print(f"\n  Timing summary ({len(deltas)} trades):")
        print(f"    Min    : {min(deltas)}s")
        print(f"    Max    : {max(deltas)}s")
        print(f"    Median : {deltas[len(deltas)//2]}s")
        print(f"    Mean   : {sum(deltas)/len(deltas):.0f}s")

        from collections import defaultdict
        buckets = defaultdict(int)
        for d in deltas:
            if d < 30:    buckets["<30s"] += 1
            elif d < 60:  buckets["30-60s"] += 1
            elif d < 120: buckets["60-120s"] += 1
            elif d < 300: buckets["120-300s"] += 1
            else:         buckets[">300s"] += 1

        print(f"\n  Distribution:")
        for b, c in sorted(buckets.items()):
            print(f"    {b:10s} : {'#'*c} ({c})")

    return deltas


def main():
    print("=" * 65)
    print("TIMING ANALYSE : OutcomeReported -> BUY des bots arb")
    print(f"Date: {ts_str(time.time())} UTC")
    print("=" * 65)

    all_deltas = {}
    for name, wallet in BOTS.items():
        all_deltas[name] = analyze_timing(name, wallet) or []
        time.sleep(0.5)

    print(f"\n{'='*65}")
    print("RESUME COMPARATIF")
    print(f"{'='*65}")
    print(f"{'Bot':12s} | {'#trades':7s} | {'min':6s} | {'median':8s} | {'max':6s} | {'mean':6s}")
    print("-" * 60)
    for name, d in all_deltas.items():
        if not d:
            print(f"{name:12s} | no data")
            continue
        ds = sorted(d)
        print(f"{name:12s} | {len(d):7d} | {min(d):5d}s | {ds[len(ds)//2]:6d}s   | {max(d):5d}s | {sum(d)/len(d):5.0f}s")


if __name__ == "__main__":
    main()

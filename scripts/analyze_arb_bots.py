"""
Analyse approfondie des 4 bots arb : desaru, notprivate, 0x199f9e, 0x6f1bf8

Pour chaque bot :
  - Récupère tout l'historique de trades (pagination)
  - Identifie les trades arb (BUY à prix bas peu après OutcomeReported)
  - Analyse : prix d'achat, taille, delta OutcomeReported->fill, prix de vente

Usage : python scripts/analyze_arb_bots.py
"""

import json
import time
import datetime
import urllib.request
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Config ────────────────────────────────────────────────────────────────────

BOTS = {
    "desaru":     "0x99558e8d51b707069ddd64e8583c699bda94e3e6",
    "notprivate": "0xD630400ea7D818a9A598F0db7E42bE7C4F08CE18",
    "0x199f9e":   "0x199f9e490b812d977c8549c993e2ce0cccfb3d76",
    "0x6f1bf8":   "0x6f1bf845ca86b8fa14027c0e93ea44509a13262a",
}

# BUY trades sous ce prix = arb candidat (token qui devrait valoir 1$)
ARB_PRICE_THRESHOLD = 0.40

# Fenêtre après OutcomeReported pour considérer un trade comme arb
ARB_WINDOW_SEC = 600  # 10 minutes

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

try:
    from dotenv import load_dotenv
    load_dotenv()
    RPC = os.getenv("RPC_HTTP_URL", "https://polygon-bor-rpc.publicnode.com")
except Exception:
    RPC = "https://polygon-bor-rpc.publicnode.com"

print(f"RPC: {RPC[:60]}...")

# ── Helpers ───────────────────────────────────────────────────────────────────

def http_get(url, retries=3, delay=1.5):
    req = urllib.request.Request(url, headers={"User-Agent": "polyBot-analyzer/2.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.request.HTTPError as e:
            if e.code == 429:
                time.sleep(delay * (attempt + 1))
                continue
            return {"error": e.code}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay)
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
    except Exception as e:
        return None


def ts_str(ts):
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def get_block_ts(block_hex):
    b = rpc_call("eth_getBlockByNumber", [block_hex, False])
    return int(b["timestamp"], 16) if b else None


# ── Fetch all activity for a wallet (paginated) ───────────────────────────────

def fetch_all_activity(wallet, max_pages=20):
    trades = []
    limit  = 100
    offset = 0
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
    return trades


# ── Fetch OutcomeReported events from NegRisk adapter ────────────────────────

OUTCOME_REPORTED_TOPIC = None

def _compute_topic():
    global OUTCOME_REPORTED_TOPIC
    try:
        from web3 import Web3
        OUTCOME_REPORTED_TOPIC = Web3.to_hex(Web3.keccak(text="OutcomeReported(bytes32,bytes32,bool)"))
    except Exception:
        # Hardcoded fallback (precomputed)
        OUTCOME_REPORTED_TOPIC = "0x9e9fa7fd355160bd4cd3f22d4333519354beff1f5689bde87f2c5e63d8d484b2"

_compute_topic()


def fetch_outcome_reported_events(from_block, to_block, chunk=2000):
    """Fetch OutcomeReported logs in chunks to avoid RPC range limits."""
    events = []
    cur = from_block
    while cur <= to_block:
        end = min(cur + chunk - 1, to_block)
        logs = rpc_call("eth_getLogs", [{
            "fromBlock": hex(cur),
            "toBlock":   hex(end),
            "address":   NEG_RISK_ADAPTER,
            "topics":    [OUTCOME_REPORTED_TOPIC],
        }])
        if logs:
            events.extend(logs)
        cur = end + 1
        time.sleep(0.2)
    return events


def decode_outcome_reported(log):
    """Returns (questionId, outcome_bool) from log topics."""
    topics = log.get("topics", [])
    if len(topics) < 3:
        return None, None
    question_id = topics[2]
    data = bytes.fromhex(log["data"][2:]) if log.get("data", "0x") != "0x" else b"\x00" * 32
    outcome = bool(data[-1]) if data else False
    block_num = int(log["blockNumber"], 16)
    return question_id, outcome, block_num


# ── Main analysis ─────────────────────────────────────────────────────────────

def analyze_bot(name, wallet):
    print(f"\n{'='*70}")
    print(f"BOT: {name} ({wallet[:20]}...)")
    print(f"{'='*70}")

    # 1. Fetch all trades
    print("  Fetching trade history...")
    trades = fetch_all_activity(wallet, max_pages=30)
    print(f"  Total trades fetched: {len(trades)}")

    if not trades:
        print("  No trades found.")
        return {}

    # Date range
    timestamps = [t.get("timestamp", 0) for t in trades if t.get("timestamp")]
    if timestamps:
        print(f"  Date range: {ts_str(min(timestamps))} to {ts_str(max(timestamps))}")

    # 2. Identify arb candidates: BUY at low price
    arb_buys = [t for t in trades
                if t.get("side") == "BUY"
                and float(t.get("price", 1.0)) < ARB_PRICE_THRESHOLD]

    print(f"  Arb BUY candidates (price < {ARB_PRICE_THRESHOLD}): {len(arb_buys)}")

    if not arb_buys:
        print("  No arb candidates found.")
        return {}

    # 3. Price distribution of arb buys
    prices = [float(t["price"]) for t in arb_buys]
    sizes  = [float(t.get("usdcSize", 0)) for t in arb_buys]

    print(f"\n  --- PRIX D'ACHAT ---")
    print(f"  Min:    {min(prices):.4f}")
    print(f"  Max:    {max(prices):.4f}")
    print(f"  Median: {sorted(prices)[len(prices)//2]:.4f}")
    print(f"  Mean:   {sum(prices)/len(prices):.4f}")

    # Histogram by price bucket
    buckets = defaultdict(int)
    for p in prices:
        if p < 0.02:   buckets["<0.02"] += 1
        elif p < 0.05: buckets["0.02-0.05"] += 1
        elif p < 0.10: buckets["0.05-0.10"] += 1
        elif p < 0.20: buckets["0.10-0.20"] += 1
        elif p < 0.30: buckets["0.20-0.30"] += 1
        else:          buckets["0.30-0.40"] += 1

    print(f"\n  Distribution des prix :")
    for bucket, count in sorted(buckets.items()):
        bar = "#" * count
        print(f"    {bucket:12s} : {bar} ({count})")

    # 4. Size distribution
    print(f"\n  --- TAILLE (USDC) ---")
    print(f"  Min:    {min(sizes):.1f}")
    print(f"  Max:    {max(sizes):.1f}")
    print(f"  Median: {sorted(sizes)[len(sizes)//2]:.1f}")
    print(f"  Mean:   {sum(sizes)/len(sizes):.1f}")
    print(f"  Total:  {sum(sizes):.1f}")

    # 5. Look for matching SELL trades (by market/outcome)
    # Group by conditionId
    sells_by_market = defaultdict(list)
    for t in trades:
        if t.get("side") == "SELL":
            cid = t.get("conditionId") or t.get("market", "")
            sells_by_market[cid].append(t)

    # Match buys to sells
    sell_prices = []
    profits     = []
    print(f"\n  --- PAIRES BUY/SELL ---")
    matched = 0
    for buy in arb_buys[:20]:  # Show first 20
        cid       = buy.get("conditionId") or buy.get("market", "")
        buy_price = float(buy["price"])
        buy_size  = float(buy.get("usdcSize", 0))
        buy_ts    = buy.get("timestamp", 0)
        outcome   = buy.get("outcome", "?")

        # Find sells on same market after this buy
        matching_sells = [
            s for s in sells_by_market.get(cid, [])
            if s.get("outcome") == outcome and s.get("timestamp", 0) >= buy_ts
        ]

        if matching_sells:
            sell = min(matching_sells, key=lambda s: s.get("timestamp", 0))
            sell_price  = float(sell["price"])
            sell_ts     = sell.get("timestamp", 0)
            delta_sell  = sell_ts - buy_ts
            profit_mult = sell_price / buy_price
            profit_usdc = buy_size * (profit_mult - 1)

            sell_prices.append(sell_price)
            profits.append(profit_usdc)
            matched += 1

            print(f"    {ts_str(buy_ts)} | BUY {buy_price:.3f} -> SELL {sell_price:.3f}"
                  f" | +{delta_sell}s | {buy_size:.0f} USDC -> x{profit_mult:.1f}"
                  f" | profit~{profit_usdc:.0f} USDC")
        else:
            print(f"    {ts_str(buy_ts)} | BUY {buy_price:.3f} | {buy_size:.0f} USDC | no sell found")

    if sell_prices:
        print(f"\n  Sell prices: min={min(sell_prices):.3f} max={max(sell_prices):.3f} mean={sum(sell_prices)/len(sell_prices):.3f}")
        print(f"  Profits:     min={min(profits):.0f} max={max(profits):.0f} total={sum(profits):.0f} USDC")

    # 6. Fetch OutcomeReported events to compute timing
    # Use the date range of arb buys to scope the block range
    print(f"\n  --- TIMING (OutcomeReported -> fill) ---")
    print(f"  Fetching OutcomeReported events from Polygon...")

    # Estimate block range from timestamps
    # Polygon: ~2s/block
    oldest_ts = min(t.get("timestamp", 0) for t in arb_buys)
    newest_ts = max(t.get("timestamp", 0) for t in arb_buys)

    # Get current block
    current_block_hex = rpc_call("eth_blockNumber", [])
    if not current_block_hex:
        print("  RPC unavailable — skipping timing analysis")
        return {}

    current_block = int(current_block_hex, 16)
    current_ts_block = get_block_ts(current_block_hex)

    if not current_ts_block:
        print("  Cannot get current block timestamp")
        return {}

    # Estimate blocks
    POLYGON_BLOCK_TIME = 2.2
    blocks_back_oldest = int((current_ts_block - oldest_ts) / POLYGON_BLOCK_TIME)
    blocks_back_newest = int((current_ts_block - newest_ts) / POLYGON_BLOCK_TIME)

    from_block = max(0, current_block - blocks_back_oldest - 500)
    to_block   = current_block - max(0, blocks_back_newest - 500)

    # Limit to 60 days
    max_blocks = int(60 * 24 * 3600 / POLYGON_BLOCK_TIME)
    from_block = max(from_block, current_block - max_blocks)

    print(f"  Block range: {from_block} -> {to_block} ({to_block-from_block} blocks)")
    events = fetch_outcome_reported_events(from_block, to_block)
    print(f"  Found {len(events)} OutcomeReported events")

    if not events:
        print("  No events — skipping timing analysis")
        return {}

    # Build timestamp map for events
    print("  Fetching block timestamps for events (sampling)...")
    event_timestamps = []
    seen_blocks = {}
    for ev in events:
        bn_hex = ev["blockNumber"]
        if bn_hex not in seen_blocks:
            ts = get_block_ts(bn_hex)
            seen_blocks[bn_hex] = ts
            time.sleep(0.05)
        ts = seen_blocks.get(bn_hex)
        if ts:
            event_timestamps.append((ts, ev))

    event_timestamps.sort(key=lambda x: x[0])
    print(f"  Timestamped {len(event_timestamps)} events")

    # For each arb buy, find closest OutcomeReported before it
    deltas = []
    for buy in arb_buys:
        buy_ts = buy.get("timestamp", 0)
        # Find latest OutcomeReported before this buy
        before = [(ts, ev) for ts, ev in event_timestamps if ts <= buy_ts]
        if not before:
            continue
        event_ts, event = before[-1]
        delta = buy_ts - event_ts
        if delta <= ARB_WINDOW_SEC:
            deltas.append(delta)

    if deltas:
        deltas.sort()
        print(f"\n  Deltas OutcomeReported -> fill ({len(deltas)} matches):")
        print(f"  Min:    {min(deltas)}s")
        print(f"  Max:    {max(deltas)}s")
        print(f"  Median: {deltas[len(deltas)//2]}s")
        print(f"  Mean:   {sum(deltas)/len(deltas):.0f}s")

        # Distribution
        d_buckets = defaultdict(int)
        for d in deltas:
            if d < 30:    d_buckets["<30s"] += 1
            elif d < 60:  d_buckets["30-60s"] += 1
            elif d < 120: d_buckets["60-120s"] += 1
            elif d < 300: d_buckets["120-300s"] += 1
            else:         d_buckets[">300s"] += 1

        print(f"\n  Distribution des deltas :")
        for bucket, count in sorted(d_buckets.items()):
            bar = "#" * count
            print(f"    {bucket:10s} : {bar} ({count})")
    else:
        print("  No timing matches found within window")

    return {
        "arb_buys":   len(arb_buys),
        "prices":     prices,
        "sizes":      sizes,
        "deltas":     deltas,
    }


def main():
    print("=" * 70)
    print("ANALYSE BOTS ARB POLYMARKET")
    print(f"Date: {ts_str(time.time())} UTC")
    print("=" * 70)

    results = {}
    for name, wallet in BOTS.items():
        results[name] = analyze_bot(name, wallet)
        time.sleep(1)

    # Summary
    print(f"\n{'='*70}")
    print("RESUME COMPARATIF")
    print(f"{'='*70}")
    print(f"{'Bot':12s} | {'#arb':5s} | {'prix moy':8s} | {'size moy':8s} | {'delta moy':9s}")
    print("-" * 55)
    for name, r in results.items():
        if not r or not r.get("prices"):
            print(f"{name:12s} | {'N/A':5s}")
            continue
        p    = r["prices"]
        s    = r["sizes"]
        d    = r["deltas"]
        print(f"{name:12s} | {len(p):5d} | {sum(p)/len(p):8.4f} | "
              f"{sum(s)/len(s):8.1f} | "
              f"{sum(d)/len(d):.0f}s" if d else f"{name:12s} | {len(p):5d} | {sum(p)/len(p):8.4f} | {sum(s)/len(s):8.1f} | N/A")


if __name__ == "__main__":
    main()

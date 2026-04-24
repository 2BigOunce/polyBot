"""
Discover losers by reverse-engineering the snipers' watchlist.

For each confirmed sniper in watchlist_snipers.txt:
  1. Fetch their recent BUY trades (= their snipes).
  2. For each BUY, resolve the counterparty via on-chain OrderFilled logs
     (maker address in topic[2]).
  3. Aggregate: count how many times each maker was the losing counterparty.

Maker wallets ranked by snipe count = the most-exploited losers.
No ratio/pnl/position heuristic — direct evidence that a confirmed sniper
took money from them. Sidesteps every classification false-positive mode.

Output: losers_by_snipe.csv, sorted by (distinct_snipers, total_snipes, total_usdc_lost).
"""

import csv
import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict

DATA_API    = "https://data-api.polymarket.com"
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"

ORDER_FILLED_TOPIC = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"

# Internal contract addresses to ignore (not real wallets)
INTERNAL_CONTRACTS = {
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",  # NegRisk Exchange
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",  # CTF Exchange
    "0x0000000000000000000000000000000000000000",
}

SNIPERS_FILE    = "watchlist_snipers.txt"
OUTPUT_FILE     = "losers_by_snipe.csv"
BUYS_PER_SNIPER = 500   # fetch up to 500 BUY trades per sniper
RPC_DELAY       = 0.08  # polite pacing on RPC


def _fetch_json(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _rpc(method: str, params: list, timeout: float = 15.0):
    req = urllib.request.Request(
        POLYGON_RPC,
        data=json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("result")


def load_snipers() -> list[str]:
    with open(SNIPERS_FILE) as f:
        return [line.strip().lower() for line in f if line.strip().startswith("0x")]


def fetch_buys(sniper: str) -> list[dict]:
    """Return the sniper's BUY trades (their snipes)."""
    url = f"{DATA_API}/activity?" + urllib.parse.urlencode({
        "user": sniper,
        "limit": str(BUYS_PER_SNIPER),
        "type":  "TRADE",
    })
    try:
        events = _fetch_json(url) or []
    except Exception as e:
        print(f"  [fetch] {sniper[:10]}... error: {e}")
        return []
    return [e for e in events if e.get("side") == "BUY"]


_receipt_cache: dict[str, dict | None] = {}


def makers_of_tx(tx_hash: str, taker: str) -> list[str]:
    """
    Parse OrderFilled logs in tx_hash. Return list of makers where taker matches.
    A single tx can fill multiple maker orders when sweeping.
    """
    if tx_hash in _receipt_cache:
        receipt = _receipt_cache[tx_hash]
    else:
        try:
            receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
        except Exception:
            receipt = None
        _receipt_cache[tx_hash] = receipt
    if not receipt:
        return []

    taker = taker.lower()
    makers: list[str] = []
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if not topics or topics[0].lower() != ORDER_FILLED_TOPIC or len(topics) < 4:
            continue
        maker = "0x" + topics[2][-40:].lower()
        evt_taker = "0x" + topics[3][-40:].lower()
        if evt_taker != taker:
            continue
        if maker in INTERNAL_CONTRACTS:
            continue
        makers.append(maker)
    return makers


def main() -> None:
    snipers = load_snipers()
    print(f"Loading {len(snipers)} snipers from {SNIPERS_FILE}\n")

    # Per-maker aggregation
    snipe_count:      dict[str, int]      = defaultdict(int)
    distinct_snipers: dict[str, set[str]] = defaultdict(set)
    usdc_lost:        dict[str, float]    = defaultdict(float)
    first_seen_ts:    dict[str, int]      = {}
    last_seen_ts:     dict[str, int]      = {}

    for i, sniper in enumerate(snipers):
        buys = fetch_buys(sniper)
        print(f"[{i+1}/{len(snipers)}] {sniper[:12]}... — {len(buys)} BUY trades")
        if not buys:
            continue

        unique_tx = {}
        for b in buys:
            tx = b.get("transactionHash")
            if tx and tx not in unique_tx:
                unique_tx[tx] = b

        for tx, trade in unique_tx.items():
            makers = makers_of_tx(tx, sniper)
            if not makers:
                continue
            ts = int(trade.get("timestamp", 0) or 0)
            # Approximate per-maker usdc: we can't perfectly split when multiple makers per tx;
            # use trade usdcSize divided by number of makers as a reasonable attribution.
            usdc = float(trade.get("usdcSize", 0) or 0)
            per_maker = usdc / max(1, len(makers))
            for m in makers:
                if m in {s.lower() for s in snipers}:
                    continue  # skip sniper-to-sniper
                snipe_count[m]    += 1
                usdc_lost[m]      += per_maker
                distinct_snipers[m].add(sniper)
                if m not in first_seen_ts or ts < first_seen_ts[m]:
                    first_seen_ts[m] = ts
                if m not in last_seen_ts or ts > last_seen_ts[m]:
                    last_seen_ts[m] = ts
            time.sleep(RPC_DELAY)

        print(f"    ... aggregate so far: {len(snipe_count)} distinct makers")

    # Build output rows
    rows = []
    for m, cnt in snipe_count.items():
        rows.append({
            "wallet":            m,
            "snipe_count":       cnt,
            "distinct_snipers":  len(distinct_snipers[m]),
            "total_usdc_lost":   round(usdc_lost[m], 2),
            "first_seen_ts":     first_seen_ts.get(m, 0),
            "last_seen_ts":      last_seen_ts.get(m, 0),
        })

    # Rank: hardest hit first (distinct_snipers * snipe_count carries the most signal)
    rows.sort(key=lambda r: (r["distinct_snipers"], r["snipe_count"], r["total_usdc_lost"]), reverse=True)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "wallet", "snipe_count", "distinct_snipers", "total_usdc_lost",
            "first_seen_ts", "last_seen_ts",
        ])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    print(f"\nWritten: {OUTPUT_FILE}")
    print(f"Total distinct losers: {len(rows)}")
    print(f"Sniped by 2+ distinct snipers: {sum(1 for r in rows if r['distinct_snipers'] >= 2)}")
    print(f"Sniped by 3+ distinct snipers: {sum(1 for r in rows if r['distinct_snipers'] >= 3)}")
    print()
    print("Top 20 most-sniped losers:")
    for r in rows[:20]:
        print(f"  {r['wallet']} | {r['distinct_snipers']} distinct snipers × {r['snipe_count']} snipes | {r['total_usdc_lost']:.2f} USDC lost")


if __name__ == "__main__":
    main()

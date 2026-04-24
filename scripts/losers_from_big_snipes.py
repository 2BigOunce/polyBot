"""
Find the BIG losers by looking at each sniper's most profitable BUYs.

For each sniper:
  1. Fetch their BUY trades
  2. Keep only those with potential profit > MIN_POTENTIAL_PROFIT (default 1000
     USDC) — estimated as size * (1 - price). Assumes a sniper's BUY in stale
     territory (price < 0.5) eventually resolves to $1.
  3. Resolve the maker counterparty via on-chain OrderFilled logs.
  4. Attribute the potential profit as "loss" to that maker.

Aggregate makers: total estimated loss + number of big snipes they were on.
Ranked by total estimated loss — the top wallets are the whales/bots who
actually lost serious money to the sniper ecosystem.

Unlike the earlier counterparty-count approach, this ignores wallets that
are merely sniped often with small sizes (those are arb/MM bots, not real
stale-posting losers). It surfaces wallets that LOST REAL MONEY on big
individual trades.

Output: losers_big_targets.csv
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

INTERNAL_CONTRACTS = {
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    "0x0000000000000000000000000000000000000000",
}

SNIPERS_FILE          = "watchlist_snipers.txt"
OUTPUT_FILE           = "losers_big_targets.csv"
BUYS_PER_SNIPER       = 500
MIN_POTENTIAL_PROFIT  = 1000.0   # only consider snipes with estimated profit >= 1k USDC
MAX_BUY_PRICE         = 0.5      # only BUYs in stale territory (prob << outcome $1)
RPC_DELAY             = 0.08


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
    url = f"{DATA_API}/activity?" + urllib.parse.urlencode({
        "user": sniper,
        "limit": str(BUYS_PER_SNIPER),
        "type":  "TRADE",
    })
    try:
        events = _fetch_json(url) or []
    except Exception:
        return []
    return [e for e in events if e.get("side") == "BUY"]


_receipt_cache: dict[str, dict | None] = {}


def makers_of_tx(tx_hash: str, taker: str) -> list[str]:
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
    makers = []
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if not topics or topics[0].lower() != ORDER_FILLED_TOPIC or len(topics) < 4:
            continue
        maker = "0x" + topics[2][-40:].lower()
        evt_taker = "0x" + topics[3][-40:].lower()
        if evt_taker != taker or maker in INTERNAL_CONTRACTS:
            continue
        makers.append(maker)
    return makers


def main() -> None:
    snipers = load_snipers()
    snipers_set = set(snipers)
    print(f"Loading {len(snipers)} snipers\n")

    total_loss:       dict[str, float] = defaultdict(float)
    big_snipe_count:  dict[str, int]   = defaultdict(int)
    distinct_snipers: dict[str, set]   = defaultdict(set)
    last_ts:          dict[str, int]   = {}
    sample_trade:     dict[str, tuple] = {}   # maker -> (price, size, est_loss, sniper)

    total_big_snipes = 0

    for i, sniper in enumerate(snipers):
        buys = fetch_buys(sniper)
        big_buys = []
        for b in buys:
            price = float(b.get("price", 0) or 0)
            size  = float(b.get("size", 0)  or 0)
            if price <= 0 or price >= MAX_BUY_PRICE or size <= 0:
                continue
            est_profit = size * (1.0 - price)
            if est_profit < MIN_POTENTIAL_PROFIT:
                continue
            big_buys.append((b, est_profit))

        print(f"[{i+1}/{len(snipers)}] {sniper[:12]}... — {len(big_buys)} big snipes (est. P&L > {MIN_POTENTIAL_PROFIT:.0f})")
        total_big_snipes += len(big_buys)

        for trade, est_profit in big_buys:
            tx = trade.get("transactionHash")
            if not tx:
                continue
            makers = makers_of_tx(tx, sniper)
            if not makers:
                continue

            # Split attribution if multiple makers per tx
            per_maker_loss = est_profit / len(makers)
            ts = int(trade.get("timestamp", 0) or 0)

            for m in makers:
                if m in snipers_set:
                    continue
                total_loss[m]        += per_maker_loss
                big_snipe_count[m]   += 1
                distinct_snipers[m].add(sniper)
                if m not in last_ts or ts > last_ts[m]:
                    last_ts[m] = ts
                if m not in sample_trade or per_maker_loss > sample_trade[m][2]:
                    sample_trade[m] = (
                        float(trade.get("price", 0)),
                        float(trade.get("size", 0)),
                        per_maker_loss,
                        sniper,
                    )
            time.sleep(RPC_DELAY)

    rows = []
    for m, loss in total_loss.items():
        p, s, biggest, sniper_here = sample_trade[m]
        rows.append({
            "wallet":           m,
            "total_est_loss":   round(loss, 2),
            "big_snipe_count":  big_snipe_count[m],
            "distinct_snipers": len(distinct_snipers[m]),
            "worst_trade_price":  round(p, 4),
            "worst_trade_size":   round(s, 2),
            "worst_trade_loss":   round(biggest, 2),
            "worst_trade_sniper": sniper_here,
            "last_seen_ts":     last_ts[m],
        })

    rows.sort(key=lambda r: r["total_est_loss"], reverse=True)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "wallet", "total_est_loss", "big_snipe_count", "distinct_snipers",
            "worst_trade_price", "worst_trade_size", "worst_trade_loss",
            "worst_trade_sniper", "last_seen_ts",
        ])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    print(f"\nWritten: {OUTPUT_FILE}")
    print(f"Total big snipes examined: {total_big_snipes}")
    print(f"Distinct makers who lost : {len(rows)}")
    print(f"  lost >= 10k USDC        : {sum(1 for r in rows if r['total_est_loss'] >= 10_000)}")
    print(f"  lost >=  5k USDC        : {sum(1 for r in rows if r['total_est_loss'] >=  5_000)}")
    print(f"  lost >=  1k USDC        : {sum(1 for r in rows if r['total_est_loss'] >=  1_000)}")
    print()
    print("Top 20 biggest losers:")
    for r in rows[:20]:
        print(f"  {r['wallet']} | est_loss=${r['total_est_loss']:>10,.0f} | "
              f"{r['big_snipe_count']} big snipes ({r['distinct_snipers']} snipers) | "
              f"worst: {r['worst_trade_size']:.0f} @ {r['worst_trade_price']:.3f}")


if __name__ == "__main__":
    main()

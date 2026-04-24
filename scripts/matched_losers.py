"""
Find losers by matching each big sniper-gain to a counterparty's realized loss
on the SAME asset.

Algorithm:
  For each sniper in watchlist_snipers.txt:
    For each BUY trade of the sniper where estimated gain = size * (1 - price) >= 1000:
      1. Parse the tx receipt on-chain → find the maker(s) (counterparty).
      2. For each maker, fetch /activity once (cached).
      3. Compute maker's realized loss on that specific asset:
            cost    = sum of usdcSize on maker's BUY on this asset
            revenue = sum of usdcSize on maker's SELL on this asset
            loss    = cost - revenue
      4. If loss >= sniper_gain_estimate * 0.5 → the maker actually lost
         money of a magnitude matching the sniper's gain. Real target.

Output: matched_losers.csv — ranked by total matched loss across all hits.
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

SNIPERS_FILE            = "watchlist_snipers.txt"
OUTPUT_FILE             = "matched_losers.csv"
BUYS_PER_SNIPER         = 500
MIN_SNIPER_GAIN         = 1000.0   # only snipes with estimated gain >= 1k USDC
MAX_SNIPER_BUY_PRICE    = 0.5      # must be in stale territory
LOSS_MATCH_RATIO        = 0.5      # maker must have lost >= 50% of sniper's gain
RPC_DELAY               = 0.08
ACTIVITY_DELAY          = 0.1


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


_activity_cache: dict[str, list[dict]] = {}


def fetch_activity(wallet: str) -> list[dict]:
    if wallet in _activity_cache:
        return _activity_cache[wallet]
    url = f"{DATA_API}/activity?" + urllib.parse.urlencode({
        "user": wallet,
        "limit": "500",
    })
    try:
        events = _fetch_json(url) or []
    except Exception:
        events = []
    _activity_cache[wallet] = events
    time.sleep(ACTIVITY_DELAY)
    return events


def realized_loss_on_asset(wallet: str, asset_id: str) -> tuple[float, float, float, int, int]:
    """
    Returns (cost, revenue, loss, n_buys, n_sells) for this wallet on this asset.
    cost = sum of BUY usdcSize, revenue = sum of SELL usdcSize. Loss = cost - revenue.
    """
    events = fetch_activity(wallet)
    cost = revenue = 0.0
    n_buys = n_sells = 0
    for e in events:
        if e.get("type") != "TRADE" or e.get("asset") != asset_id:
            continue
        usdc = float(e.get("usdcSize", 0) or 0)
        if e.get("side") == "BUY":
            cost += usdc
            n_buys += 1
        elif e.get("side") == "SELL":
            revenue += usdc
            n_sells += 1
    return cost, revenue, cost - revenue, n_buys, n_sells


def main() -> None:
    snipers = load_snipers()
    snipers_set = {s.lower() for s in snipers}
    print(f"Loaded {len(snipers)} snipers from {SNIPERS_FILE}")
    print(f"Filter: sniper_gain >= {MIN_SNIPER_GAIN:.0f} USDC, price < {MAX_SNIPER_BUY_PRICE}")
    print(f"        maker loss >= {LOSS_MATCH_RATIO:.0%} of sniper gain\n")

    hits: list[dict] = []  # per-trade hits

    total_big_snipes = 0
    for i, sniper in enumerate(snipers):
        buys = fetch_buys(sniper)
        big_buys = []
        for b in buys:
            price = float(b.get("price", 0) or 0)
            size  = float(b.get("size", 0) or 0)
            if price <= 0 or price >= MAX_SNIPER_BUY_PRICE or size <= 0:
                continue
            est_gain = size * (1.0 - price)
            if est_gain >= MIN_SNIPER_GAIN:
                big_buys.append((b, est_gain))
        total_big_snipes += len(big_buys)
        print(f"[{i+1}/{len(snipers)}] {sniper[:12]}... — {len(big_buys)} big snipes >= {MIN_SNIPER_GAIN:.0f}")

        for trade, est_gain in big_buys:
            tx      = trade.get("transactionHash")
            asset   = trade.get("asset")
            if not tx or not asset:
                continue
            makers = makers_of_tx(tx, sniper)
            time.sleep(RPC_DELAY)
            if not makers:
                continue

            # The gain is split across makers in the same tx
            per_maker_gain = est_gain / len(makers)
            threshold      = per_maker_gain * LOSS_MATCH_RATIO

            for maker in makers:
                if maker in snipers_set:
                    continue
                cost, revenue, loss, nb, ns = realized_loss_on_asset(maker, asset)
                if loss < threshold:
                    continue
                hits.append({
                    "maker":           maker,
                    "asset":           asset,
                    "sniper":          sniper,
                    "sniper_gain":     round(per_maker_gain, 2),
                    "maker_loss":      round(loss, 2),
                    "maker_cost":      round(cost, 2),
                    "maker_revenue":   round(revenue, 2),
                    "maker_n_buys":    nb,
                    "maker_n_sells":   ns,
                    "sniper_buy_price": round(float(trade.get("price", 0)), 4),
                    "sniper_buy_size":  round(float(trade.get("size", 0)), 2),
                    "tx":               tx,
                    "timestamp":        int(trade.get("timestamp", 0) or 0),
                    "market_title":     (trade.get("title") or "")[:80],
                })

    # Aggregate per maker
    per_maker: dict[str, dict] = defaultdict(lambda: {
        "total_loss": 0.0, "total_sniper_gain": 0.0,
        "n_hits": 0, "snipers": set(), "worst": None,
    })
    for h in hits:
        m = h["maker"]
        per_maker[m]["total_loss"]        += h["maker_loss"]
        per_maker[m]["total_sniper_gain"] += h["sniper_gain"]
        per_maker[m]["n_hits"]            += 1
        per_maker[m]["snipers"].add(h["sniper"])
        if per_maker[m]["worst"] is None or h["maker_loss"] > per_maker[m]["worst"]["maker_loss"]:
            per_maker[m]["worst"] = h

    rows = []
    for m, stats in per_maker.items():
        w = stats["worst"]
        rows.append({
            "wallet":            m,
            "total_maker_loss":  round(stats["total_loss"], 2),
            "n_matched_hits":    stats["n_hits"],
            "distinct_snipers":  len(stats["snipers"]),
            "total_sniper_gain": round(stats["total_sniper_gain"], 2),
            "worst_loss":        round(w["maker_loss"], 2),
            "worst_sniper":      w["sniper"],
            "worst_sniper_buy_price": w["sniper_buy_price"],
            "worst_sniper_buy_size":  w["sniper_buy_size"],
            "worst_market":      w["market_title"],
            "worst_tx":          w["tx"],
        })
    rows.sort(key=lambda r: r["total_maker_loss"], reverse=True)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "wallet", "total_maker_loss", "n_matched_hits", "distinct_snipers",
            "total_sniper_gain", "worst_loss", "worst_sniper",
            "worst_sniper_buy_price", "worst_sniper_buy_size",
            "worst_market", "worst_tx",
        ])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    print(f"\nWritten: {OUTPUT_FILE}")
    print(f"Big snipes scanned       : {total_big_snipes}")
    print(f"Matched hits             : {len(hits)}")
    print(f"Distinct matched losers  : {len(rows)}")
    print(f"  loss >= 10k USDC        : {sum(1 for r in rows if r['total_maker_loss'] >= 10_000)}")
    print(f"  loss >=  5k USDC        : {sum(1 for r in rows if r['total_maker_loss'] >=  5_000)}")
    print(f"  loss >=  1k USDC        : {sum(1 for r in rows if r['total_maker_loss'] >=  1_000)}")
    print()
    print("Top 20 matched losers:")
    for r in rows[:20]:
        print(f"  {r['wallet']} | loss=${r['total_maker_loss']:>10,.0f} | "
              f"{r['n_matched_hits']} hit(s) ({r['distinct_snipers']} snipers) | "
              f"worst: -${r['worst_loss']:.0f} on '{r['worst_market'][:40]}'")


if __name__ == "__main__":
    main()

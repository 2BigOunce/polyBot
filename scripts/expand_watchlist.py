"""
BFS expansion of sniper/loser watchlist.

Starts from a small seed of known snipers and losers, then:
  1. Fetches each seed's recent trades via /data-api/activity.
  2. For each trade, fetches the on-chain tx receipt via Polygon RPC and
     parses OrderFilled events to identify the REAL counterparty (Polymarket
     REST doesn't expose it — but the on-chain logs do).
  3. Classifies each counterparty via SELL/BUY ratio (>2× sniper, <0.5× loser).
  4. Adds confirmed wallets to the watchlist and iterates.

Outputs `watchlist_snipers.txt` and `watchlist_losers.txt` sorted + deduped.
"""

import json
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

DATA_API = "https://data-api.polymarket.com"
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"  # free, no auth

# CTFExchange OrderFilled event topic
ORDER_FILLED_TOPIC = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"

# Internal contract addresses that appear as maker/taker in OrderFilled but
# aren't real counterparty wallets — skip them.
INTERNAL_CONTRACTS = {
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",  # NegRisk Exchange
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",  # CTF Exchange
    "0x0000000000000000000000000000000000000000",
}

# Seeds now read from watchlist files (manually curated).
def _load_seeds(path: str) -> list[str]:
    try:
        with open(path) as f:
            return [line.strip().lower() for line in f if line.strip().startswith("0x")]
    except FileNotFoundError:
        return []

SEED_SNIPERS = _load_seeds("watchlist_snipers.txt")
SEED_LOSERS  = _load_seeds("watchlist_losers.txt")

LIMIT_TRADES_PER_WALLET = 60  # recent trades to inspect per seed
MAX_ITERATIONS          = 2
SNIPER_RATIO_MIN        = 2.0
LOSER_RATIO_MAX         = 0.5
MIN_TRADES_TO_CLASSIFY  = 30

_receipt_cache: dict[str, dict | None] = {}


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


def fetch_tx_receipt(tx_hash: str) -> dict | None:
    if tx_hash in _receipt_cache:
        return _receipt_cache[tx_hash]
    try:
        receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
    except Exception:
        receipt = None
    _receipt_cache[tx_hash] = receipt
    return receipt


def parse_counterparties_for(tx_hash: str, wallet: str) -> set[str]:
    """
    Parse OrderFilled logs in tx_hash. For each event where `wallet` is
    maker or taker, return the OTHER side (the counterparty).
    Excludes internal exchange contracts.
    """
    receipt = fetch_tx_receipt(tx_hash)
    if not receipt:
        return set()

    wallet = wallet.lower()
    counterparties: set[str] = set()
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if not topics or topics[0].lower() != ORDER_FILLED_TOPIC or len(topics) < 4:
            continue
        maker = "0x" + topics[2][-40:].lower()
        taker = "0x" + topics[3][-40:].lower()
        if wallet == maker:
            if taker not in INTERNAL_CONTRACTS:
                counterparties.add(taker)
        elif wallet == taker:
            if maker not in INTERNAL_CONTRACTS:
                counterparties.add(maker)
    return counterparties


def fetch_recent_trades(wallet: str) -> list[dict]:
    # Only TRADE events are useful for counterparty discovery
    url = f"{DATA_API}/activity?" + urllib.parse.urlencode({
        "user": wallet,
        "limit": str(LIMIT_TRADES_PER_WALLET),
        "type": "TRADE",
    })
    try:
        return _fetch_json(url) or []
    except Exception:
        return []


def classify(wallet: str) -> tuple[str, dict | None]:
    """
    Classify by (SELL+REDEEM)/BUY ratio. REDEEM is a direct on-chain payout
    from the protocol when a winning position resolves — not a SELL on the
    orderbook, but functionally equivalent (cashing in). Ignoring it causes
    retail buy-and-hold winners to be misclassified as losers.
    """
    wallet = wallet.lower()
    if len(wallet) != 42 or not wallet.startswith("0x"):
        return "invalid", None
    url = f"{DATA_API}/activity?" + urllib.parse.urlencode({
        "user": wallet,
        "limit": "500",
    })
    try:
        events = _fetch_json(url) or []
    except Exception:
        return "error", None
    trades  = [e for e in events if e.get("type") == "TRADE"]
    redeems = [e for e in events if e.get("type") == "REDEEM"]
    if not trades and not redeems:
        return "nodata", None
    buy      = sum(float(t.get("usdcSize", 0) or 0) for t in trades if t.get("side") == "BUY")
    sell     = sum(float(t.get("usdcSize", 0) or 0) for t in trades if t.get("side") == "SELL")
    redeem   = sum(float(r.get("usdcSize", 0) or 0) for r in redeems)
    out      = sell + redeem
    ratio    = (out / buy) if buy > 0 else float("inf")
    n        = len(trades)
    stats    = {"ratio": ratio, "n": n, "buy": buy, "sell": sell, "redeem": redeem}
    if n < MIN_TRADES_TO_CLASSIFY:
        return "tooshort", stats
    if ratio >= SNIPER_RATIO_MIN:
        return "sniper", stats
    if ratio <= LOSER_RATIO_MAX:
        return "loser", stats
    return "neutral", stats


def process_wallets(wallets: list[str], seen_candidates: set[str]) -> set[str]:
    """
    For each wallet, fetch recent trades + resolve counterparties via RPC.
    Returns the set of new candidate wallets not yet seen.
    """
    candidates: set[str] = set()
    for i, w in enumerate(wallets):
        trades = fetch_recent_trades(w)
        unique_tx = list({t["transactionHash"] for t in trades if t.get("transactionHash")})
        print(f"  [{i+1}/{len(wallets)}] {w[:12]}... — {len(trades)} trades, {len(unique_tx)} unique tx")
        for tx in unique_tx:
            cps = parse_counterparties_for(tx, w)
            for cp in cps:
                if cp not in seen_candidates:
                    candidates.add(cp)
        time.sleep(0.1)
    return candidates


def main() -> None:
    snipers    = {w.lower() for w in SEED_SNIPERS}
    losers     = {w.lower() for w in SEED_LOSERS}
    to_process = list(snipers | losers)
    seen_all   = set(to_process)

    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n{'=' * 60}")
        print(f"ITERATION {iteration} — processing {len(to_process)} wallets")
        print(f"{'=' * 60}")

        candidates = process_wallets(to_process, seen_all)
        print(f"\n  -> {len(candidates)} new candidate wallets to classify")

        if not candidates:
            print("  no new candidates — BFS converged.")
            break

        new_snipers: set[str] = set()
        new_losers:  set[str] = set()
        for i, cand in enumerate(sorted(candidates)):
            code, stats = classify(cand)
            seen_all.add(cand)
            if code == "sniper":
                new_snipers.add(cand)
                print(f"    [+SNIPER] {cand} — ratio={stats['ratio']:.2f}x n={stats['n']} (buy={stats['buy']:.0f} sell+redeem={stats['sell']+stats['redeem']:.0f})")
            elif code == "loser":
                new_losers.add(cand)
                print(f"    [+LOSER]  {cand} — ratio={stats['ratio']:.2f}x n={stats['n']} (buy={stats['buy']:.0f} sell={stats['sell']:.0f} redeem={stats['redeem']:.0f})")
            time.sleep(0.1)
            if (i + 1) % 25 == 0:
                print(f"    ... classified {i+1}/{len(candidates)}")

        snipers |= new_snipers
        losers  |= new_losers
        to_process = list(new_snipers | new_losers)

        print(f"\n  iteration {iteration} result:")
        print(f"    +{len(new_snipers)} snipers (total: {len(snipers)})")
        print(f"    +{len(new_losers)} losers  (total: {len(losers)})")
        if not to_process:
            break

    print(f"\n{'=' * 60}")
    print(f"FINAL: {len(snipers)} snipers, {len(losers)} losers")
    print(f"{'=' * 60}")

    # Write to *_expanded.txt so the curated watchlist_*.txt stays intact.
    # User can review and manually merge.
    with open("watchlist_snipers_expanded.txt", "w") as f:
        for s in sorted(snipers):
            f.write(s + "\n")
    with open("watchlist_losers_expanded.txt", "w") as f:
        for l in sorted(losers):
            f.write(l + "\n")
    print("Written: watchlist_snipers_expanded.txt, watchlist_losers_expanded.txt")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[interrupted]")
        sys.exit(1)

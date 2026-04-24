"""
Quick classifier: is this Polymarket wallet a sniper, a loser, or neither?

Signature:
  SELL/BUY ratio > 2.0 with >= 30 trades  -> SNIPER (buys rarely, dumps often)
  SELL/BUY ratio < 0.5 with >= 30 trades  -> LOSER  (keeps filling buys, rarely sells)
  else                                     -> neutral

Usage (from repo root):
  python scripts/classify_wallet.py <wallet> [wallet2 ...]
  echo 0xabc... | python scripts/classify_wallet.py -   # read one per line from stdin
"""

import json
import sys
import time
import urllib.parse
import urllib.request

DATA_API = "https://data-api.polymarket.com"


def _fetch(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _fmt_ratio(ratio: float) -> str:
    if ratio == float("inf"):
        return "inf"
    return f"{ratio:.2f}x"


def classify(wallet: str) -> str:
    wallet = wallet.strip().lower()
    if not wallet.startswith("0x") or len(wallet) != 42:
        print(f"[SKIP]   {wallet} (not a valid address)")
        return "invalid"

    # Fetch all activity (TRADE + REDEEM). Redeems = cashing in winning shares
    # directly from the protocol after resolution; they're effective sells and
    # must count in the ratio or retail winners get misclassified as losers.
    url = f"{DATA_API}/activity?" + urllib.parse.urlencode({
        "user": wallet,
        "limit": "500",
    })
    try:
        events = _fetch(url) or []
    except Exception as e:
        print(f"[ERROR]  {wallet} — {e}")
        return "error"

    trades = [e for e in events if e.get("type") == "TRADE"]
    redeems = [e for e in events if e.get("type") == "REDEEM"]
    if not trades and not redeems:
        print(f"[NODATA] {wallet} — no activity")
        return "nodata"

    buy_usdc    = sum(float(t.get("usdcSize", 0) or 0) for t in trades if t.get("side") == "BUY")
    sell_usdc   = sum(float(t.get("usdcSize", 0) or 0) for t in trades if t.get("side") == "SELL")
    redeem_usdc = sum(float(r.get("usdcSize", 0) or 0) for r in redeems)
    effective_out = sell_usdc + redeem_usdc
    n           = len(trades)
    ratio       = (effective_out / buy_usdc) if buy_usdc > 0 else float("inf")

    markets: set[str] = set()
    for t in trades:
        cid = t.get("conditionId") or t.get("market")
        if cid:
            markets.add(cid)

    if n >= 30 and ratio > 2.0:
        verdict = "[SNIPER]"
        code    = "sniper"
    elif n >= 30 and ratio < 0.5:
        verdict = "[LOSER] "
        code    = "loser"
    else:
        verdict = "[neutr]"
        code    = "neutral"

    print(f"{verdict} {wallet}")
    print(f"   trades : {n} ({len(redeems)} redeems)")
    print(f"   buy    : {buy_usdc:>10,.0f} USDC")
    print(f"   sell   : {sell_usdc:>10,.0f} USDC")
    print(f"   redeem : {redeem_usdc:>10,.0f} USDC")
    print(f"   ratio  : {_fmt_ratio(ratio):>8s} ((SELL+REDEEM)/BUY)")
    print(f"   markets: {len(markets)}")
    return code


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        print("Usage: python scripts/classify_wallet.py <wallet> [wallet2 ...]")
        print("       echo 0xabc | python scripts/classify_wallet.py -")
        sys.exit(1)

    if argv == ["-"]:
        wallets = [line.strip() for line in sys.stdin if line.strip()]
    else:
        wallets = argv

    for i, w in enumerate(wallets):
        classify(w)
        if i < len(wallets) - 1:
            print()
            time.sleep(0.3)


if __name__ == "__main__":
    main()

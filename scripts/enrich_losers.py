"""
Enrich watchlist_losers.txt with per-wallet stats and a bot-likeness score.

Bot-likeness heuristic (defective bots vs. whales who just lose):
  - Defective bots  = high trade count, many markets, moderate volume, small avg size
  - Losing whales   = low trade count, few markets, huge volume, big avg size

Score ranks wallets with bot-like patterns highest so you can triage from the top.

Output: losers_enriched.csv — open in Excel/LibreOffice, sort freely.

Usage:
    python scripts/enrich_losers.py            # reads watchlist_losers.txt
    python scripts/enrich_losers.py <input>    # custom file
"""

import csv
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request

DATA_API = "https://data-api.polymarket.com"
INPUT_FILE  = sys.argv[1] if len(sys.argv) > 1 else "watchlist_losers.txt"
OUTPUT_FILE = "losers_enriched.csv"
TRADE_LIMIT = 500

# Bot-likeness scoring: moderate volume = sweet spot, high trade count + markets = bot
BOT_VOLUME_LOW  = 5_000      # USDC — below this, volume too small to be worth tracking
BOT_VOLUME_HIGH = 80_000     # USDC — above this, likely a whale, not a bot
BOT_AVG_SIZE_MAX = 200.0     # USDC per trade — bots trade small and regular


def _fetch(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_stats(wallet: str) -> dict | None:
    # Fetch everything (TRADE + REDEEM) — REDEEM is cashing winning positions,
    # effectively a sell. Ignoring it labels retail winners as losers.
    url = f"{DATA_API}/activity?" + urllib.parse.urlencode({
        "user": wallet,
        "limit": str(TRADE_LIMIT),
    })
    try:
        events = _fetch(url) or []
    except Exception as e:
        return {"wallet": wallet, "error": str(e)}
    trades  = [e for e in events if e.get("type") == "TRADE"]
    redeems = [e for e in events if e.get("type") == "REDEEM"]
    if not trades and not redeems:
        return {"wallet": wallet, "n_trades": 0}

    buy_usdc    = sum(float(t.get("usdcSize", 0) or 0) for t in trades if t.get("side") == "BUY")
    sell_usdc   = sum(float(t.get("usdcSize", 0) or 0) for t in trades if t.get("side") == "SELL")
    redeem_usdc = sum(float(r.get("usdcSize", 0) or 0) for r in redeems)
    sizes       = [float(t.get("usdcSize", 0) or 0) for t in trades]
    markets     = {t.get("conditionId") or t.get("market") for t in trades if t.get("conditionId") or t.get("market")}

    total_volume  = buy_usdc + sell_usdc
    n_trades      = len(trades)
    n_redeems     = len(redeems)
    n_markets     = len(markets)
    median_size   = statistics.median(sizes) if sizes else 0.0
    # Ratio includes REDEEM as effective sell
    effective_out = sell_usdc + redeem_usdc
    ratio         = (effective_out / buy_usdc) if buy_usdc > 0 else float("inf")

    # Bot-likeness: trade count × markets × volume-in-sweet-spot × small-median-size
    # Normalised to be roughly in [0, ~5] range for typical bots.
    def _volume_score(v: float) -> float:
        if v < 500:                      return 0.0
        if v < BOT_VOLUME_LOW:           return v / BOT_VOLUME_LOW
        if v <= BOT_VOLUME_HIGH:         return 1.0
        # decay linearly above sweet spot
        decay = max(0.0, 1.0 - (v - BOT_VOLUME_HIGH) / BOT_VOLUME_HIGH)
        return decay

    def _size_score(m: float) -> float:
        # Smaller median size = more bot-like (up to a cap)
        if m <= 0:                       return 0.0
        if m <= BOT_AVG_SIZE_MAX:        return 1.0
        return BOT_AVG_SIZE_MAX / m      # decays 1 → 0 as m grows

    v_s   = _volume_score(total_volume)
    s_s   = _size_score(median_size)
    t_s   = min(2.0, n_trades / 100)     # trade-count score saturated at 2
    m_s   = min(2.0, n_markets / 30)     # markets score saturated at 2

    bot_score = round(t_s * m_s * v_s * s_s, 3)

    # is_loser_still: re-check with REDEEM-aware ratio. If ratio > 0.5 now,
    # the wallet was a false positive (probably buy-and-hold retail winner).
    is_loser_still = (ratio <= 0.5) if buy_usdc > 0 else False

    return {
        "wallet":        wallet,
        "n_trades":      n_trades,
        "n_redeems":     n_redeems,
        "n_markets":     n_markets,
        "buy_usdc":      round(buy_usdc, 2),
        "sell_usdc":     round(sell_usdc, 2),
        "redeem_usdc":   round(redeem_usdc, 2),
        "total_volume":  round(total_volume, 2),
        "median_size":   round(median_size, 2),
        "ratio_sbr":     round(ratio, 3) if ratio != float("inf") else None,
        "bot_score":     bot_score if is_loser_still else 0.0,
        "still_loser":   "yes" if is_loser_still else "no",
    }


def main() -> None:
    with open(INPUT_FILE) as f:
        wallets = [line.strip() for line in f if line.strip().startswith("0x")]

    print(f"Loading {len(wallets)} wallets from {INPUT_FILE}")
    print(f"Enriching — ~{len(wallets) * 0.4 / 60:.0f} min expected...\n")

    results: list[dict] = []
    for i, w in enumerate(wallets):
        stats = fetch_stats(w)
        if stats:
            results.append(stats)
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(wallets)} processed")
        time.sleep(0.15)

    # Sort by bot_score desc (most bot-like first)
    results.sort(key=lambda r: r.get("bot_score", 0), reverse=True)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        fieldnames = ["wallet", "still_loser", "bot_score", "n_trades", "n_redeems", "n_markets",
                      "total_volume", "buy_usdc", "sell_usdc", "redeem_usdc", "median_size",
                      "ratio_sbr", "error"]
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            writer.writerow(row)

    # Summary
    still_losers = [r for r in results if r.get("still_loser") == "yes"]
    false_pos    = [r for r in results if r.get("still_loser") == "no"]
    scored       = [r for r in still_losers if r.get("bot_score", 0) > 0]
    high_bot     = [r for r in still_losers if r.get("bot_score", 0) >= 1.0]
    very_high    = [r for r in still_losers if r.get("bot_score", 0) >= 2.0]
    print(f"\nWritten: {OUTPUT_FILE}")
    print(f"  total          : {len(results)}")
    print(f"  still losers   : {len(still_losers)} (ratio w/ REDEEM <= 0.5)")
    print(f"  false positives: {len(false_pos)} (were retail winners, now excluded)")
    print(f"  score >=1      : {len(high_bot)}  (likely defective bots)")
    print(f"  score >=2      : {len(very_high)} (strong bot signature)")
    print()
    print("Top 10 candidates:")
    for r in results[:10]:
        print(f"  {r['wallet']} score={r.get('bot_score', 0):.2f} "
              f"trades={r.get('n_trades', 0)} markets={r.get('n_markets', 0)} "
              f"vol={r.get('total_volume', 0):.0f} median={r.get('median_size', 0):.1f}")


if __name__ == "__main__":
    main()

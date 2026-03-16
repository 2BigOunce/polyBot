"""
Patch real_trader.py: replace the size_multiplier block with Half-Kelly sizing
driven by the Bayesian confidence scorer.

Usage:
  python3 scripts/patch_half_kelly.py            # dry-run
  python3 scripts/patch_half_kelly.py --execute  # apply patch
"""

import sys
from pathlib import Path

TARGET = Path(__file__).parent.parent / "src" / "trading" / "real_trader.py"

OLD = """    # Scale up size based on insider trade size (conviction signal)
    # 10k-25k → 1.0x, 25k-50k → 1.5x, 50k-75k → 2.0x, 75k+ → 3.0x (cap)
    if insider_size >= 75_000:
        size_multiplier = 3.0
    elif insider_size >= 50_000:
        size_multiplier = 2.0
    elif insider_size >= 25_000:
        size_multiplier = 1.5
    else:
        size_multiplier = 1.0
    remaining_exposure = REAL_MAX_MARKET_EXPOSURE - get_market_real_exposure(market, outcome)
    budget_usdc = round(min(balance * REAL_MAX_POSITION_PCT, REAL_MAX_TRADE * size_multiplier, remaining_exposure), 2)"""

NEW = """    # Half-Kelly sizing driven by Bayesian confidence score
    # p̂ = estimated probability this trade is profitable (scorer.py)
    # f  = (p̂ - price) / (2 * (1 - price))  — Half-Kelly fraction
    from src.analysis.scorer import compute_confidence
    p_hat = compute_confidence(followed_wallet, insider_size, limit_price)
    edge = p_hat - limit_price
    if edge <= 0:
        print(f"[real] No edge (p̂={p_hat:.3f}, price={limit_price:.3f}) — skipped")
        return
    half_kelly_fraction = edge / (2.0 * (1.0 - limit_price))
    remaining_exposure = REAL_MAX_MARKET_EXPOSURE - get_market_real_exposure(market, outcome)
    budget_usdc = round(min(balance * half_kelly_fraction, REAL_MAX_TRADE, remaining_exposure), 2)"""


def main(execute: bool = False) -> None:
    text = TARGET.read_text(encoding="utf-8")

    if OLD not in text:
        print("ERROR: target block not found — already patched or text mismatch.")
        print("Run: grep -n 'size_multiplier' " + str(TARGET))
        sys.exit(1)

    new_text = text.replace(OLD, NEW, 1)

    print("=== HALF-KELLY PATCH ===" + (" — DRY RUN" if not execute else " — EXECUTING"))
    print(f"  File   : {TARGET}")
    print(f"  Removed: size_multiplier block (4 lines)")
    print(f"  Added  : Half-Kelly fraction + edge check")

    if not execute:
        print("\nRun with --execute to apply.")
        return

    TARGET.write_text(new_text, encoding="utf-8")
    print("\nDone. Restart the bot: sudo systemctl restart polybot")


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)

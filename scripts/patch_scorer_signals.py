"""
Patch real_trader.py on the VPS:
  1. Add days_until_resolution parameter to execute_trade()
  2. Pass days_until_resolution and market_liquidity to compute_confidence()

Usage:
  python3 scripts/patch_scorer_signals.py            # dry-run
  python3 scripts/patch_scorer_signals.py --execute  # apply patch
"""

import sys
from pathlib import Path

TARGET = Path(__file__).parent.parent / "src" / "trading" / "real_trader.py"

PATCHES = [
    # 1. Add days_until_resolution param to execute_trade signature
    (
        "    insider_size: float = 0.0,\n) -> None:",
        "    insider_size: float = 0.0,\n    days_until_resolution: float | None = None,\n) -> None:",
    ),
    # 2. Pass both new args to compute_confidence
    (
        "    p_hat = compute_confidence(followed_wallet, insider_size, limit_price)",
        "    p_hat = compute_confidence(followed_wallet, insider_size, limit_price, days_until_resolution=days_until_resolution, market_liquidity=liquidity)",
    ),
]


def main(execute: bool = False) -> None:
    text = TARGET.read_text(encoding="utf-8")

    print("=== SCORER SIGNALS PATCH ===" + (" — DRY RUN" if not execute else " — EXECUTING"))
    print(f"  File: {TARGET}")

    new_text = text
    for old, new in PATCHES:
        if old not in new_text:
            print(f"\nERROR: target string not found:\n  {old!r}")
            sys.exit(1)
        new_text = new_text.replace(old, new, 1)
        print(f"  Patched: {old[:60].strip()}…")

    if not execute:
        print("\nRun with --execute to apply.")
        return

    TARGET.write_text(new_text, encoding="utf-8")
    print("\nDone. Restart the bot: sudo systemctl restart polybot")


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)

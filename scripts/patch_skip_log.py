"""
Patch real_trader.py: improve skip log when budget is too small —
distinguish "balance too low" from "market exposure nearly full".

Usage:
  python3 scripts/patch_skip_log.py            # dry-run
  python3 scripts/patch_skip_log.py --execute  # apply patch
"""

import sys
from pathlib import Path

TARGET = Path(__file__).parent.parent / "src" / "trading" / "real_trader.py"

OLD = """    if size_usdc < 5.0:
        print(f"[real] Balance too low ({balance:.2f} USDC) — skipped")
        return"""

NEW = """    if size_usdc < 5.0:
        if remaining_exposure < 5.0:
            print(f"[real] Market exposure nearly full ({get_market_real_exposure(market, outcome):.2f}/{REAL_MAX_MARKET_EXPOSURE} USDC) — skipped")
        else:
            print(f"[real] Budget too small (balance={balance:.2f}, kelly={half_kelly_fraction:.3f}) — skipped")
        return"""


def main(execute: bool = False) -> None:
    text = TARGET.read_text(encoding="utf-8")

    if OLD not in text:
        print("ERROR: target block not found — already patched or text mismatch.")
        print("Run: grep -n 'Balance too low' " + str(TARGET))
        sys.exit(1)

    new_text = text.replace(OLD, NEW, 1)

    print("=== SKIP LOG PATCH ===" + (" — DRY RUN" if not execute else " — EXECUTING"))
    print(f"  File   : {TARGET}")
    print(f"  Change : 'Balance too low' → distinguishes exposure cap vs low balance")

    if not execute:
        print("\nRun with --execute to apply.")
        return

    TARGET.write_text(new_text, encoding="utf-8")
    print("\nDone. Restart the bot: sudo systemctl restart polybot")


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)

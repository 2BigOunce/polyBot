"""
Patch real_trader.py: change Half-Kelly (÷2) to Third-Kelly (÷3).

Usage:
  python3 scripts/patch_third_kelly.py            # dry-run
  python3 scripts/patch_third_kelly.py --execute  # apply patch
"""

import sys
from pathlib import Path

TARGET = Path(__file__).parent.parent / "src" / "trading" / "real_trader.py"

OLD = "    half_kelly_fraction = edge / (2.0 * (1.0 - limit_price))"
NEW = "    half_kelly_fraction = edge / (3.0 * (1.0 - limit_price))  # Third-Kelly"


def main(execute: bool = False) -> None:
    text = TARGET.read_text(encoding="utf-8")

    if OLD not in text:
        print("ERROR: target line not found — already patched or text mismatch.")
        print("Run: grep -n 'kelly_fraction' " + str(TARGET))
        sys.exit(1)

    new_text = text.replace(OLD, NEW, 1)

    print("=== THIRD-KELLY PATCH ===" + (" — DRY RUN" if not execute else " — EXECUTING"))
    print(f"  File   : {TARGET}")
    print(f"  Change : Half-Kelly (÷2)  →  Third-Kelly (÷3)")

    if not execute:
        print("\nRun with --execute to apply.")
        return

    TARGET.write_text(new_text, encoding="utf-8")
    print("\nDone. Restart the bot: sudo systemctl restart polybot")


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)

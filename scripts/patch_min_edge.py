"""
Patch real_trader.py: replace edge <= 0 guard with edge < REAL_MIN_EDGE threshold.

Usage:
  python3 scripts/patch_min_edge.py            # dry-run
  python3 scripts/patch_min_edge.py --execute  # apply patch
"""

import sys
from pathlib import Path

TARGET = Path(__file__).parent.parent / "src" / "trading" / "real_trader.py"

OLD = """    edge = p_hat - limit_price
    if edge <= 0:
        print(f"[real] No edge (p̂={p_hat:.3f}, price={limit_price:.3f}) — skipped")
        return"""

NEW = """    edge = p_hat - limit_price
    if edge < REAL_MIN_EDGE:
        print(f"[real] Edge too small (p̂={p_hat:.3f}, price={limit_price:.3f}, edge={edge:.3f}) — skipped")
        return"""


def main(execute: bool = False) -> None:
    text = TARGET.read_text(encoding="utf-8")

    if OLD not in text:
        print("ERROR: target block not found — already patched or text mismatch.")
        print("Run: grep -n 'edge' " + str(TARGET))
        sys.exit(1)

    new_text = text.replace(OLD, NEW, 1)

    # Also ensure REAL_MIN_EDGE is imported from settings
    if "REAL_MIN_EDGE" not in text:
        # Add to the settings import line
        old_import = "from config.settings import ("
        if old_import not in new_text:
            print("WARNING: could not find settings import block — add REAL_MIN_EDGE import manually.")
        else:
            # Find the import block and add REAL_MIN_EDGE
            import_marker = "REAL_MAX_MARKET_EXPOSURE,"
            if import_marker in new_text:
                new_text = new_text.replace(
                    import_marker,
                    import_marker + "\n    REAL_MIN_EDGE,"
                )

    print("=== MIN EDGE PATCH ===" + (" — DRY RUN" if not execute else " — EXECUTING"))
    print(f"  File   : {TARGET}")
    print(f"  Change : edge <= 0  →  edge < REAL_MIN_EDGE (default 0.02)")

    if not execute:
        print("\nRun with --execute to apply.")
        return

    TARGET.write_text(new_text, encoding="utf-8")
    print("\nDone. Restart the bot: sudo systemctl restart polybot")


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)

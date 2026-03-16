"""
Patch real_trader.py: pass p_hat to send_real_buy so it appears in Telegram alerts.

Usage:
  python3 scripts/patch_phat_telegram.py            # dry-run
  python3 scripts/patch_phat_telegram.py --execute  # apply patch
"""

import sys
from pathlib import Path

TARGET = Path(__file__).parent.parent / "src" / "trading" / "real_trader.py"

OLD = "    await send_real_buy(followed_wallet, outcome, market, insider_price, fill_price, size_usdc, insider_size)"
NEW = "    await send_real_buy(followed_wallet, outcome, market, insider_price, fill_price, size_usdc, insider_size, p_hat=p_hat)"


def main(execute: bool = False) -> None:
    text = TARGET.read_text(encoding="utf-8")

    if OLD not in text:
        print("ERROR: target line not found — already patched or text mismatch.")
        print("Run: grep -n 'send_real_buy' " + str(TARGET))
        sys.exit(1)

    new_text = text.replace(OLD, NEW, 1)

    print("=== p̂ TELEGRAM PATCH ===" + (" — DRY RUN" if not execute else " — EXECUTING"))
    print(f"  File: {TARGET}")
    print(f"  Adds p_hat= kwarg to send_real_buy call")

    if not execute:
        print("\nRun with --execute to apply.")
        return

    TARGET.write_text(new_text, encoding="utf-8")
    print("\nDone. Restart the bot: sudo systemctl restart polybot")


if __name__ == "__main__":
    main(execute="--execute" in sys.argv)

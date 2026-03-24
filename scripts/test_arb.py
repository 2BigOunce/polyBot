"""
Test the arb scanner on an already-resolved market.

Usage:
    python scripts/test_arb.py <condition_id> <winner_index>

Example:
    python scripts/test_arb.py 0xabc123... 0

This bypasses dedup and the resolution monitor — calls scan_resolved_market directly.
Useful to validate the detection + sweep logic and Telegram alerts without waiting
for a live resolution event.

Note: PRE-CLOSE fast path cannot be tested this way (market already closed).
      The script will hit the fast path with closed=true (normal case).
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts/test_arb.py <condition_id> <winner_index>")
        print()
        print("Find a recently resolved market on polymarket.com, grab its conditionId")
        print("from the URL or DevTools, and pass winner_index=0 (Yes) or 1 (No).")
        sys.exit(1)

    condition_id  = sys.argv[1]
    winner_index  = int(sys.argv[2])

    print(f"[test] condition_id  : {condition_id}")
    print(f"[test] winner_index  : {winner_index}")
    print()

    # Start the WS CLOB listener (needed for clob_ws imports to work)
    from src.arb.clob_ws import run_clob_ws
    from src.arb.arb_scanner import _scanned, scan_resolved_market

    # Clear dedup so the test always runs
    _scanned.discard(condition_id)

    # Run scanner + WS listener concurrently
    # Scanner will finish quickly; WS listener runs until cancelled
    async def run_scanner():
        await asyncio.sleep(0.5)  # let WS connect first
        await scan_resolved_market(condition_id, winner_index)
        print("[test] scan complete — press Ctrl+C to exit")

    try:
        await asyncio.gather(run_clob_ws(), run_scanner())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())

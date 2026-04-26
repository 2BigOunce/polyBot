"""
Cluster paper-trade reconciliation — CLI wrapper.

Runs the reconcile logic from src/clob/cluster_reconcile.py and prints a
report. The same logic is used by the daily Telegram reporter
(cluster_pnl_reporter coroutine in src/clob/cluster_tracker.py).

Usage:
    python analysis/14_cluster_reconcile.py            # check + print summary
    python analysis/14_cluster_reconcile.py --refresh  # re-query already-resolved markets
"""

from __future__ import annotations

import os
import sys

# Make `src.*` importable when run as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.clob.cluster_reconcile import format_summary, reconcile  # noqa: E402


def main() -> int:
    refresh = "--refresh" in sys.argv
    records, pending, new_resolved = reconcile(refresh=refresh)
    print(f"[reconcile] loaded {len(records)} trades")
    print(f"[reconcile] {new_resolved} newly resolved this run, {pending} still pending")
    print()
    print(format_summary(records, pending, new_resolved))
    return 0


if __name__ == "__main__":
    sys.exit(main())

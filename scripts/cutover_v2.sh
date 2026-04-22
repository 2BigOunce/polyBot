#!/usr/bin/env bash
# Atomic V1 → V2 cutover for polybot on the VPS.
#
# Run at ~10:45 UTC on 28 April 2026, a few minutes before Polymarket's
# scheduled ~11:00 UTC CLOB V2 hard-switch (~1h downtime).
#
# Flow:
#   1. Stop polybot.service
#   2. Back up V1 executor files as *.v1.backup.py
#   3. Promote *_v2.py → canonical names (arb_executor.py etc.) so imports
#      like `from src.trading.arb_executor import ArbExecutor` resolve to V2
#   4. Sanity-import the 4 modules to catch any syntax/import error
#   5. Start polybot.service and tail logs for 60s
#
# Rollback (if V2 backend misbehaves after the downtime window):
#   scripts/cutover_v2.sh --rollback
#
# Prerequisites:
#   - scripts/deploy_v2_vps.sh already ran (V2 files present, SDK installed)
#   - scripts/cancel_all_maker_orders.py --execute ran earlier to clean DB
#   - scripts/wrap_usdc_to_pusd.py --execute ran to convert capital
#   - You are ssh'd onto the VPS as a sudoer, running this as
#       sudo bash scripts/cutover_v2.sh

set -euo pipefail

REPO="/home/ubuntu/polyBot"
TRADING="$REPO/src/trading"
VENV="$REPO/venv"

# (V1 canonical name, V2 source name)
MODULES=(
  "arb_executor"
  "clob_mispricing_executor"
  "maker_positioning"
  "real_trader"
)

_need_root() {
  [[ $EUID -eq 0 ]] || { echo "❌ Run with sudo"; exit 1; }
}

_rollback() {
  _need_root
  echo "=== Rolling back to V1 ==="
  systemctl stop polybot || true
  for m in "${MODULES[@]}"; do
    if [[ -f "$TRADING/$m.v1.backup.py" ]]; then
      mv -f "$TRADING/$m.v1.backup.py" "$TRADING/$m.py"
      echo "  ✅ restored $m.py from backup"
    else
      echo "  ⚠️  no backup for $m — skipping"
    fi
  done
  systemctl start polybot
  echo "✅ rolled back. Tail logs with: journalctl -u polybot -f"
  exit 0
}

[[ "${1:-}" == "--rollback" ]] && _rollback

_need_root

echo "=== [1/5] Pre-flight checks ==="
for m in "${MODULES[@]}"; do
  [[ -f "$TRADING/${m}_v2.py" ]] || { echo "❌ missing $TRADING/${m}_v2.py — run deploy_v2_vps.sh first"; exit 1; }
done
"$VENV/bin/python" -c "import py_clob_client_v2" || {
  echo "❌ py_clob_client_v2 not installed in venv"; exit 1;
}
echo "✅ all V2 files + SDK present"

echo ""
echo "=== [2/5] Stopping polybot.service ==="
systemctl stop polybot
sleep 2
systemctl is-active polybot && { echo "❌ service still active"; exit 1; } || echo "✅ stopped"

echo ""
echo "=== [3/5] Backing up V1 + promoting V2 ==="
for m in "${MODULES[@]}"; do
  if [[ -f "$TRADING/$m.py" ]]; then
    mv -f "$TRADING/$m.py" "$TRADING/$m.v1.backup.py"
    echo "  📦 $m.py → $m.v1.backup.py"
  fi
  cp -f "$TRADING/${m}_v2.py" "$TRADING/$m.py"
  chown ubuntu:ubuntu "$TRADING/$m.py"
  echo "  ✅ promoted ${m}_v2.py → $m.py"
done

echo ""
echo "=== [4/5] Sanity-importing V2 modules ==="
env PYTHONPATH="$REPO" "$VENV/bin/python" -c "
import importlib
for m in ['src.trading.arb_executor',
          'src.trading.clob_mispricing_executor',
          'src.trading.maker_positioning',
          'src.trading.real_trader']:
    importlib.import_module(m)
    print(f'  ✅ {m}')
"

echo ""
echo "=== [5/5] Starting polybot.service ==="
systemctl start polybot
sleep 3
systemctl is-active polybot || { echo "❌ service failed to start — check journalctl -u polybot"; exit 1; }
echo "✅ polybot running on V2. Tailing logs for 60s (Ctrl-C to stop)…"
echo ""
timeout 60 journalctl -u polybot -f --no-pager || true

echo ""
echo "✅ Cutover complete. Monitor with: journalctl -u polybot -f"
echo "   Rollback if needed: sudo bash scripts/cutover_v2.sh --rollback"

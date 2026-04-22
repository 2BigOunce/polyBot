#!/usr/bin/env bash
# Deploy V2 migration files to the VPS without touching the running V1 bot.
#
# What this does (idempotent, safe to re-run):
#   1. scp the 4 src/trading/*_v2.py files (gitignored — private trading code)
#   2. scp the 3 migration helper scripts (wrap, cancel, smoke test)
#   3. pip install py_clob_client_v2 into the polybot venv on the VPS
#   4. run the V2 SDK smoke test against the live V1 backend (hot-swap)
#
# The running polybot.service keeps using V1 files; V2 files sit alongside
# untouched until the 28 April cutover (see scripts/cutover_v2.sh).
#
# Usage:
#   VPS_HOST=polybot@1.2.3.4 SSH_KEY=./polybot-dublin.pem scripts/deploy_v2_vps.sh
#   VPS_HOST=polybot@1.2.3.4 SSH_KEY=./polybot-dublin.pem scripts/deploy_v2_vps.sh --skip-test

set -euo pipefail

: "${VPS_HOST:?Set VPS_HOST=polybot@<ip>}"
: "${SSH_KEY:?Set SSH_KEY=path/to/polybot-dublin.pem}"

REMOTE_DIR="/home/polybot/polyBot"
REMOTE_STAGE="/tmp/polybot_v2_deploy"
SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new)

V2_TRADING_FILES=(
  "src/trading/arb_executor_v2.py"
  "src/trading/clob_mispricing_executor_v2.py"
  "src/trading/maker_positioning_v2.py"
  "src/trading/real_trader_v2.py"
)

V2_SCRIPTS=(
  "scripts/wrap_usdc_to_pusd.py"
  "scripts/cancel_all_maker_orders.py"
  "scripts/test_py_clob_v2.py"
)

echo "=== [1/4] Verifying local V2 files exist ==="
for f in "${V2_TRADING_FILES[@]}" "${V2_SCRIPTS[@]}"; do
  [[ -f "$f" ]] || { echo "❌ missing: $f"; exit 1; }
done
echo "✅ all $((${#V2_TRADING_FILES[@]} + ${#V2_SCRIPTS[@]})) files present locally"

echo ""
echo "=== [2/4] scp V2 files to $VPS_HOST (stage in $REMOTE_STAGE) ==="
# Stage in /tmp (writable by the SSH user), then sudo-move + chown to polybot.
ssh "${SSH_OPTS[@]}" "$VPS_HOST" "mkdir -p $REMOTE_STAGE/trading $REMOTE_STAGE/scripts"
scp "${SSH_OPTS[@]}" "${V2_TRADING_FILES[@]}" "$VPS_HOST:$REMOTE_STAGE/trading/"
scp "${SSH_OPTS[@]}" "${V2_SCRIPTS[@]}"       "$VPS_HOST:$REMOTE_STAGE/scripts/"
ssh "${SSH_OPTS[@]}" "$VPS_HOST" bash <<REMOTE
set -euo pipefail
sudo mv $REMOTE_STAGE/trading/*.py $REMOTE_DIR/src/trading/
sudo mv $REMOTE_STAGE/scripts/*.py $REMOTE_DIR/scripts/
sudo chown polybot:polybot $REMOTE_DIR/src/trading/*_v2.py
sudo chown polybot:polybot $REMOTE_DIR/scripts/wrap_usdc_to_pusd.py \
                           $REMOTE_DIR/scripts/cancel_all_maker_orders.py \
                           $REMOTE_DIR/scripts/test_py_clob_v2.py
rm -rf $REMOTE_STAGE
REMOTE
echo "✅ files copied and chowned to polybot"

echo ""
echo "=== [3/4] Installing py_clob_client_v2 in VPS venv ==="
ssh "${SSH_OPTS[@]}" "$VPS_HOST" bash <<'REMOTE'
set -euo pipefail
cd /home/polybot/polyBot
# venv path used by the systemd unit (check with: systemctl cat polybot)
VENV="/home/polybot/polyBot/.venv"
[[ -d "$VENV" ]] || VENV="/home/polybot/venv"
echo "Using venv: $VENV"
sudo -u polybot "$VENV/bin/pip" install --upgrade py_clob_client_v2 2>&1 | tail -5
sudo -u polybot "$VENV/bin/python" -c "import py_clob_client_v2; print('py_clob_client_v2 version:', getattr(py_clob_client_v2, '__version__', 'unknown'))"
REMOTE
echo "✅ SDK installed"

if [[ "${1:-}" == "--skip-test" ]]; then
  echo ""
  echo "=== [4/4] Skipped smoke test (--skip-test) ==="
  exit 0
fi

echo ""
echo "=== [4/4] Running V2 SDK smoke test on VPS ==="
ssh "${SSH_OPTS[@]}" "$VPS_HOST" bash <<'REMOTE'
set -euo pipefail
cd /home/polybot/polyBot
VENV="/home/polybot/polyBot/.venv"
[[ -d "$VENV" ]] || VENV="/home/polybot/venv"
sudo -u polybot env PYTHONPATH=. "$VENV/bin/python" scripts/test_py_clob_v2.py
REMOTE

echo ""
echo "✅ V2 deployment complete. polybot.service still runs V1 — untouched."
echo "   Next: run scripts/cutover_v2.sh at ~10:45 UTC on 28 April."

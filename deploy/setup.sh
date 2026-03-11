#!/bin/bash
# One-time VPS setup script for polyBot
# Run as root on a fresh Ubuntu 22/24 LTS server
# Usage: bash deploy/setup.sh

set -e

BOT_USER="polybot"
BOT_DIR="/home/$BOT_USER/polyBot"
REPO_URL="https://github.com/<your-username>/polyBot.git"  # TODO: update

echo "=== [1/6] System update ==="
apt update && apt upgrade -y

echo "=== [2/6] Install Python 3.12+ and tools ==="
apt install -y python3 python3-pip python3-venv git

echo "=== [3/6] Create dedicated user ==="
id "$BOT_USER" &>/dev/null || useradd -m -s /bin/bash "$BOT_USER"

echo "=== [4/6] Clone repo ==="
sudo -u "$BOT_USER" git clone "$REPO_URL" "$BOT_DIR"

echo "=== [5/6] Install Python dependencies ==="
sudo -u "$BOT_USER" python3 -m venv "$BOT_DIR/.venv"
sudo -u "$BOT_USER" "$BOT_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$BOT_USER" "$BOT_DIR/.venv/bin/pip" install -r "$BOT_DIR/requirements.txt"

echo "=== [6/6] Install systemd service ==="
cp "$BOT_DIR/deploy/polybot.service" /etc/systemd/system/polybot.service
systemctl daemon-reload
systemctl enable polybot

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps (manual — never commit these files):"
echo "  1. Copy your .env file:"
echo "     scp .env $BOT_USER@<vps-ip>:$BOT_DIR/.env"
echo ""
echo "  2. Copy the private trading module:"
echo "     scp -r src/trading/ $BOT_USER@<vps-ip>:$BOT_DIR/src/trading/"
echo ""
echo "  3. Start the bot:"
echo "     systemctl start polybot"
echo "     journalctl -u polybot -f"

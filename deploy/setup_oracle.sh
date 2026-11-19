#!/usr/bin/env bash
# One-time setup on an Oracle Cloud Always Free Ubuntu VM (or any Ubuntu 22.04+ box).
# Run as the default "ubuntu" user:  bash deploy/setup_oracle.sh
set -euo pipefail

APP=/opt/market-updater
REPO_URL="${REPO_URL:-$(git -C "$(dirname "$0")/.." remote get-url origin 2>/dev/null || true)}"

echo "==> Installing system packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg git sqlite3 tzdata

echo "==> Putting the code in $APP"
sudo mkdir -p "$APP"
sudo chown "$USER":"$USER" "$APP"
if [ ! -d "$APP/.git" ]; then
  if [ -n "$REPO_URL" ]; then
    git clone "$REPO_URL" "$APP"
  else
    cp -r "$(dirname "$0")/.." "$APP"
  fi
fi
cd "$APP"
mkdir -p data logs

echo "==> Python environment"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "!! Fill in $APP/.env (see SETUP.md), then re-run this script."
  exit 0
fi

echo "==> Copying feed URLs into the Notion Sources table"
.venv/bin/python -m market_updater sync-sources || true

echo "==> Checking sources once (takes a few minutes)"
.venv/bin/python -m market_updater check-sources || true

echo "==> Installing timers (Europe/London times)"
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo sed -i "s/^User=ubuntu$/User=$USER/" /etc/systemd/system/market-updater-*.service
sudo systemctl daemon-reload
for t in collect daily health weekly backup; do
  sudo systemctl enable --now "market-updater-$t.timer"
done
systemctl list-timers 'market-updater-*'

echo "==> Done. Test now with:  cd $APP && .venv/bin/python -m market_updater dry-run --force"
echo "    Logs:  journalctl -u market-updater-daily -n 200"

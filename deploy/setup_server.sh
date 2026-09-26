#!/usr/bin/env bash
# Runs ON the Lightsail instance (Ubuntu 24.04), from ~/recallcare. Idempotent: safe to re-run on every deploy.
# Touches only: ~/recallcare, the recallcare systemd unit, and /etc/caddy/Caddyfile.
set -euo pipefail
cd "$HOME/recallcare"
HOST="${1:?usage: setup_server.sh <public-hostname>}"

if ! command -v caddy >/dev/null || ! python3 -c "import venv" 2>/dev/null; then
  sudo apt-get update -y
  sudo apt-get install -y python3-venv python3-pip caddy sqlite3
fi

test -d .venv || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p data evals/runs

sudo cp deploy/recallcare.service /etc/systemd/system/recallcare.service
sed "s/__HOST__/${HOST}/" deploy/Caddyfile.template | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo mkdir -p /var/log/caddy && sudo chown caddy:caddy /var/log/caddy || true
sudo systemctl daemon-reload
sudo systemctl enable --now recallcare
sudo systemctl restart recallcare
sudo systemctl reload caddy || sudo systemctl restart caddy

for i in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break
  sleep 1
done
echo "--- local health"; curl -fsS http://127.0.0.1:8000/healthz; echo

#!/usr/bin/env bash
# Runs on the LAPTOP. Pushes the repo + .env to the single Lightsail instance and (re)starts the service.
# Usage: deploy/deploy.sh            (reads DEPLOY_HOST / DEPLOY_USER / DEPLOY_SSH_KEY from .env)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
: "${DEPLOY_HOST:?set DEPLOY_HOST (static IP) in .env}"
USER_="${DEPLOY_USER:-ubuntu}"
KEY="${DEPLOY_SSH_KEY:-.secrets/lightsail_rsa}"
HOSTNAME_="$(echo "$DEPLOY_HOST" | tr '.' '-').sslip.io"
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"

echo "==> syncing code to $USER_@$DEPLOY_HOST:~/recallcare"
rsync -az --delete -e "$SSH" \
  --exclude .git --exclude .venv --exclude data --exclude .secrets --exclude vendor --exclude '__pycache__' \
  --exclude .env --exclude 'evals/runs' --exclude .claude --exclude .DS_Store \
  ./ "$USER_@$DEPLOY_HOST:~/recallcare/"

echo "==> writing server .env (laptop .env + server overrides; never committed)"
TMP="$(mktemp)"
grep -v -E '^(DEPLOY_|GITHUB_REPO_URL|PUBLIC_BASE_URL|DATABASE_PATH|CHECKPOINT_DB_PATH)=' .env > "$TMP"
{
  echo "PUBLIC_BASE_URL=https://$HOSTNAME_"
  echo "DATABASE_PATH=/home/ubuntu/recallcare/data/recallcare.db"
  echo "CHECKPOINT_DB_PATH=/home/ubuntu/recallcare/data/checkpoints.db"
} >> "$TMP"
scp -i "$KEY" -q "$TMP" "$USER_@$DEPLOY_HOST:~/recallcare/.env"
rm -f "$TMP"
$SSH "$USER_@$DEPLOY_HOST" "chmod 600 ~/recallcare/.env && chmod +x ~/recallcare/deploy/*.sh && ~/recallcare/deploy/setup_server.sh $HOSTNAME_"

echo "==> public check"
for i in $(seq 1 40); do
  curl -fsS "https://$HOSTNAME_/healthz" && { echo; break; }
  sleep 3
done
echo "Live URL: https://$HOSTNAME_   (webhook: https://$HOSTNAME_/webhook/whatsapp)"

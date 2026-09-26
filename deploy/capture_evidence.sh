#!/usr/bin/env bash
# Captures text evidence from the live server into docs/deployment_evidence/ (phone numbers stay masked).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
: "${DEPLOY_HOST:?set DEPLOY_HOST in .env}"
KEY="${DEPLOY_SSH_KEY:-.secrets/lightsail_rsa}"
HOST="$(echo "$DEPLOY_HOST" | tr '.' '-').sslip.io"
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new ${DEPLOY_USER:-ubuntu}@$DEPLOY_HOST"
OUT=docs/deployment_evidence
mkdir -p "$OUT"
{ echo "# GET https://$HOST/healthz  ($(date '+%Y-%m-%d %H:%M %Z'))"; curl -fsS "https://$HOST/healthz"; echo; } > "$OUT/03_healthz.json"
$SSH "hostnamectl | sed -n '1,8p'; echo; nproc; free -h | head -2; echo; systemctl --no-pager status recallcare caddy | head -30" > "$OUT/04_server_status.txt"
$SSH "journalctl -u recallcare --since '-24h' --no-pager | grep -E 'wa_inbound|\"agent\": \"guard\"|send_message|send_template' | tail -60" > "$OUT/05_whatsapp_roundtrip_log.txt" || true
$SSH "cd ~/recallcare && .venv/bin/python -m evals.run --mode mock --no-write 2>&1 | tail -40" > "$OUT/06_eval_on_server.md" || true
echo "Evidence written to $OUT"; ls -la "$OUT"

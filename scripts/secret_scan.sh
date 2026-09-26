#!/usr/bin/env bash
# Pre-commit secret scan. Blocks commits that stage secrets or secret-looking strings.
# Usage: scripts/secret_scan.sh            (scans staged changes; used by .githooks/pre-commit)
#        scripts/secret_scan.sh --all      (scans every tracked file; used by `make secret-scan`)
set -euo pipefail

PATTERNS=(
  'sk-or-v1-[A-Za-z0-9]{20,}'                 # OpenRouter
  'sk-ant-[A-Za-z0-9_-]{20,}'                 # Anthropic
  'EAA[A-Za-z0-9]{40,}'                       # Meta / WhatsApp access tokens
  'AKIA[0-9A-Z]{16}'                          # AWS access key id
  'aws_secret_access_key[[:space:]]*=[[:space:]]*[A-Za-z0-9/+]{30,}'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'ghp_[A-Za-z0-9]{30,}'                      # GitHub PAT
  'xox[abprs]-[A-Za-z0-9-]{10,}'              # Slack
  '(API_KEY|APP_SECRET|ACCESS_TOKEN|PASSWORD|SESSION_SECRET|VERIFY_TOKEN)=[^[:space:]#${<"][^[:space:]#]{7,}'   # skips $VAR / <placeholder>
)

fail=0
if [[ "${1:-}" == "--all" ]]; then
  files=$(git ls-files)
else
  files=$(git diff --cached --name-only --diff-filter=ACM)
fi

for f in $files; do
  case "$f" in
    .env|.env.*) [[ "$f" == ".env.example" ]] || { echo "BLOCKED: $f must never be committed"; fail=1; continue; } ;;
    .secrets/*|*.pem) echo "BLOCKED: $f looks like a key file"; fail=1; continue ;;
    scripts/secret_scan.sh) continue ;;
  esac
  [[ -f "$f" ]] || continue
  if [[ "${1:-}" == "--all" ]]; then content=$(cat "$f" 2>/dev/null || true); else content=$(git show ":$f" 2>/dev/null || true); fi
  for p in "${PATTERNS[@]}"; do
    if grep -E -n -e "$p" <<<"$content" >/dev/null 2>&1; then
      echo "BLOCKED: possible secret in $f (pattern: $p)"
      grep -E -n -e "$p" <<<"$content" | head -3 | sed -E 's/(.{12}).*/\1…[redacted]/'
      fail=1
    fi
  done
done

if [[ $fail -ne 0 ]]; then
  echo "Secret scan failed. Move secrets to .env (gitignored)."
  exit 1
fi
echo "secret scan: clean"

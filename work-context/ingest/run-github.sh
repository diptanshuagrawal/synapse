#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TOKEN_FILE="$HOME/.secrets/github_pat"
STATE_FILE="$ROOT/state/last_github_success.date"
TODAY="$(date +%Y-%m-%d)"

# Skip if already succeeded today (idempotent — cron retries until success)
if [[ -f "$STATE_FILE" && "$(cat "$STATE_FILE")" == "$TODAY" ]]; then
  exit 0
fi

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR token file not found: $TOKEN_FILE" >> "$ROOT/logs/ingest.log"
  exit 1
fi

export GITHUB_TOKEN
GITHUB_TOKEN="$(cat "$TOKEN_FILE")"

mkdir -p "$ROOT/state"
set +e
"$ROOT/.venv/bin/python" "$SCRIPT_DIR/github.py" "$@"
exit_code=$?
set -e

# Mark success only if exit 0 (no repo failures)
if [[ $exit_code -eq 0 ]]; then
  echo "$TODAY" > "$STATE_FILE"
fi

# Refresh validate cache (consumed by bin/cron-status.sh).
# Fail-soft: validator non-zero exit doesn't affect ingest exit code.
set +e
"$ROOT/.venv/bin/python" "$ROOT/derive/github_validate.py" --json \
  > "$ROOT/state/last_github_validate.json.tmp" 2>/dev/null \
  && mv "$ROOT/state/last_github_validate.json.tmp" "$ROOT/state/last_github_validate.json" \
  || rm -f "$ROOT/state/last_github_validate.json.tmp"

# Reconcile observed identity signals back into people.yaml.
"$ROOT/.venv/bin/python" "$ROOT/derive/identity_reconcile.py" \
  >> "$ROOT/logs/identity_reconcile.log" 2>&1 || true
set -e

exit $exit_code

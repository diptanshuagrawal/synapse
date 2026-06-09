#!/usr/bin/env bash
# Cron rollup — keyword-only classification. No LLM API calls.
# LLM classification happens in chat session via manual-rollup.sh workflow.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

STATE_FILE="$ROOT/state/last_rollup_success.date"
LOG_FILE="$ROOT/logs/rollup.log"
TODAY="$(date +%Y-%m-%d)"

mkdir -p "$ROOT/state" "$ROOT/logs"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG_FILE"; }

# Idempotent: if already succeeded today, exit 0. Cron can fire every 30 min.
if [[ -f "$STATE_FILE" && "$(cat "$STATE_FILE")" == "$TODAY" ]]; then
  exit 0
fi

log "auth: keyword-only (LLM classification in chat session)"

# ── run ──────────────────────────────────────────────────────────────────────
# Strip any Anthropic auth that might be in env so rollup.py never attempts LLM.
export ANTHROPIC_API_KEY=""
export ANTHROPIC_AUTH_TOKEN=""

set +e
"$ROOT/.venv/bin/python" "$SCRIPT_DIR/rollup.py" --days 240 --week --skip-narrative "$@"
exit_code=$?
set -e

if [[ $exit_code -eq 0 ]]; then
  echo "$TODAY" > "$STATE_FILE"
fi

exit $exit_code

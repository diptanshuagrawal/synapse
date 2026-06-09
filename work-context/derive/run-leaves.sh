#!/usr/bin/env bash
# Cron leaves — Phase 1 of the chat-classify leave pipeline.
# Runs regex dump + markdown render. Zero LLM calls.
# Phase 2 (chat classify + apply) is owner-driven via /leaves slash skill
# or via owner's autonomous-session routine.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

STATE_FILE="$ROOT/state/last_leaves_success.date"
LOG_FILE="$ROOT/logs/leaves.log"
TODAY="$(date +%Y-%m-%d)"

mkdir -p "$ROOT/state" "$ROOT/logs"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG_FILE"; }

# Idle gate: one success/day. Plist fires daily; retry-once semantics fine.
if [[ -f "$STATE_FILE" && "$(cat "$STATE_FILE")" == "$TODAY" ]]; then
  exit 0
fi

log "Leaves Phase 1 starting (dump + render, no LLM)"

# Fail-loud against accidental Anthropic auth (mirrors rollup-auth-strip).
export ANTHROPIC_API_KEY=""
export ANTHROPIC_AUTH_TOKEN=""

set +e
"$ROOT/.venv/bin/python" "$SCRIPT_DIR/leaves_dump.py" >> "$LOG_FILE" 2>&1
dump_rc=$?
"$ROOT/.venv/bin/python" "$SCRIPT_DIR/render_leaves.py" >> "$LOG_FILE" 2>&1
render_rc=$?
set -e

if [[ $dump_rc -eq 0 && $render_rc -eq 0 ]]; then
  echo "$TODAY" > "$STATE_FILE"
  log "Leaves Phase 1 done (dump=ok render=ok)"
  exit 0
fi

log "Leaves Phase 1 FAILED (dump=$dump_rc render=$render_rc)"
exit 1

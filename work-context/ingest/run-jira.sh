#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TOKEN_FILE="$HOME/.secrets/atlassian_token"
TODAY="$(date +%Y-%m-%d)"

# Two ingest windows/day (see LaunchAgent schedule):
#   morning (~11:00, 5-min retry) feeds the 11:45 standup with the FULL previous day;
#   evening (18:00–23:00, 30-min retry) is a same-day safety net so the day's data
#   through ~6pm is captured even if the next morning's run fails.
# Window is picked by clock hour; each window has its own success marker so both
# can succeed on the same calendar day. Boundary 17:00 is safe (no fires 12–16).
if (( 10#$(date +%H) >= 17 )); then
  STATE_FILE="$ROOT/state/last_jira_evening_success.date"
else
  STATE_FILE="$ROOT/state/last_jira_success.date"
fi

# Skip if this window already succeeded today (idempotent — cron retries until success)
if [[ -f "$STATE_FILE" && "$(cat "$STATE_FILE")" == "$TODAY" ]]; then
  exit 0
fi

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR token file not found: $TOKEN_FILE" >> "$ROOT/logs/ingest.log"
  exit 1
fi

export ATLASSIAN_TOKEN
ATLASSIAN_TOKEN="$(cat "$TOKEN_FILE")"

# Email is NOT read from a file — jira.py resolves it from config (org.owner_email
# in sources.yaml). A wrong/placeholder email + valid token makes Jira return
# HTTP 200 with an EMPTY list (not 401), which would silently freeze the data; the
# ingester's /myself identity check rejects that. Set ATLASSIAN_EMAIL here only to
# override config.

mkdir -p "$ROOT/state"
set +e
"$ROOT/.venv/bin/python" "$SCRIPT_DIR/jira.py" "$@"
exit_code=$?
set -e

# Mark success only if exit 0
if [[ $exit_code -eq 0 ]]; then
  echo "$TODAY" > "$STATE_FILE"
fi

# Refresh validate cache (consumed by bin/cron-status.sh).
# Fail-soft: validator non-zero exit doesn't affect ingest exit code.
set +e
"$ROOT/.venv/bin/python" "$ROOT/derive/jira_validate.py" --json \
  > "$ROOT/state/last_jira_validate.json.tmp" 2>/dev/null \
  && mv "$ROOT/state/last_jira_validate.json.tmp" "$ROOT/state/last_jira_validate.json" \
  || rm -f "$ROOT/state/last_jira_validate.json.tmp"

# Reconcile observed identity signals back into people.yaml.
# Fail-soft: never let reconciler errors affect ingest exit code.
"$ROOT/.venv/bin/python" "$ROOT/derive/identity_reconcile.py" \
  >> "$ROOT/logs/identity_reconcile.log" 2>&1 || true
set -e

exit $exit_code

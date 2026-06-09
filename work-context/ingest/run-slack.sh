#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# No daily-success gate: slack volume is high, every fire ingests cursor-delta.
# slack_ingest_app is idempotent (upsert skips dupes, cursor advances on success).
# SLACK_USER_TOKEN sourced from ~/context/.env by slack_api_client._load_env().

mkdir -p "$ROOT/state" "$ROOT/logs"
set +e
"$ROOT/.venv/bin/python" "$SCRIPT_DIR/slack_ingest_app.py"
exit_code=$?

# Daily alert-thread reconcile (once/day, date-gated). no_threads channels
# skip reply reconcile every fire; this captures team-involved replies on
# their alert threads once a day. Fail-soft: never affects ingest exit code.
ALERT_GATE="$ROOT/state/last_alert_threads.date"
TODAY="$(date +%Y-%m-%d)"
if [[ ! -f "$ALERT_GATE" || "$(cat "$ALERT_GATE" 2>/dev/null)" != "$TODAY" ]]; then
  "$ROOT/.venv/bin/python" "$ROOT/derive/slack_alert_thread_reconcile.py" \
    >> "$ROOT/logs/ingest.log" 2>&1 \
    && echo "$TODAY" > "$ALERT_GATE"
fi

# Refresh validate cache (consumed by bin/cron-status.sh).
# Fail-soft: validator non-zero exit doesn't affect ingest exit code.
"$ROOT/.venv/bin/python" "$ROOT/derive/slack_validate.py" --json \
  > "$ROOT/state/last_slack_validate.json.tmp" 2>/dev/null \
  && mv "$ROOT/state/last_slack_validate.json.tmp" "$ROOT/state/last_slack_validate.json" \
  || rm -f "$ROOT/state/last_slack_validate.json.tmp"
set -e

# slack_ingest_app writes STATE_FILE itself on any-channel success.
# Wrapper exit reflects script exit so launchd records failures.
exit $exit_code

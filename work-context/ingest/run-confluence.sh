#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TOKEN_FILE="$HOME/.secrets/atlassian_token"
TODAY="$(date +%Y-%m-%d)"

# Two ingest windows/day (see LaunchAgent schedule):
#   morning (~11:00, 5-min retry) feeds the 11:45 standup with the FULL previous day;
#     idempotent — skips once today's morning run has succeeded.
#   evening (18:00–23:00, 30-min retry) re-runs EVERY fire so the DB stays fresh
#     through the night (cursor-based incremental pull is cheap + idempotent, like
#     slack). Captures late-evening activity a next-morning standup would otherwise miss.
# Window is picked by clock hour; each window has its own success marker so both
# can succeed on the same calendar day. Boundary 17:00 is safe (no fires 12–16).
if (( 10#$(date +%H) >= 17 )); then
  STATE_FILE="$ROOT/state/last_confluence_evening_success.date"
  EVENING=1
else
  STATE_FILE="$ROOT/state/last_confluence_success.date"
  EVENING=0
fi

# Morning window only: skip once today's run has succeeded (idempotent retry-to-success).
# Evening window: fall through on EVERY fire so each 30-min tick pulls the latest delta;
# the marker is still stamped on success below (cron-status reads it for freshness).
if [[ "$EVENING" -eq 0 && -f "$STATE_FILE" && "$(cat "$STATE_FILE")" == "$TODAY" ]]; then
  exit 0
fi

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR token file not found: $TOKEN_FILE" >> "$ROOT/logs/ingest.log"
  exit 1
fi

export ATLASSIAN_TOKEN
ATLASSIAN_TOKEN="$(cat "$TOKEN_FILE")"

# Email is NOT read from a file — confluence.py resolves it from config
# (org.owner_email in sources.yaml). A wrong/placeholder email + valid token makes
# Jira/Confluence return HTTP 200 with an EMPTY result (not 401), which would
# silently freeze the data; the ingester's /user/current identity check rejects
# that. Set ATLASSIAN_EMAIL here only to override config.

mkdir -p "$ROOT/state"
set +e
"$ROOT/.venv/bin/python" "$SCRIPT_DIR/confluence.py" "$@"
exit_code=$?
set -e

# Mark success only if exit 0
if [[ $exit_code -eq 0 ]]; then
  echo "$TODAY" > "$STATE_FILE"

  # Refresh trd_owners materialised view after every successful ingest.
  # Failure is non-fatal — log + continue.
  set +e
  "$ROOT/.venv/bin/python" "$ROOT/derive/build_trd_owners.py" \
    >> "$ROOT/logs/ingest.log" 2>&1
  trd_exit=$?
  set -e
  if [[ $trd_exit -ne 0 ]]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WARN build_trd_owners failed exit=$trd_exit" \
      >> "$ROOT/logs/ingest.log"
  fi
fi

# Refresh validate cache (consumed by bin/cron-status.sh).
# Fail-soft: validator non-zero exit doesn't affect ingest exit code.
set +e
"$ROOT/.venv/bin/python" "$ROOT/derive/confluence_validate.py" --json \
  > "$ROOT/state/last_confluence_validate.json.tmp" 2>/dev/null \
  && mv "$ROOT/state/last_confluence_validate.json.tmp" "$ROOT/state/last_confluence_validate.json" \
  || rm -f "$ROOT/state/last_confluence_validate.json.tmp"

# Cross-cutting pipeline-integrity validate cache (all sources).
"$ROOT/ingest/refresh-pipeline-validate.sh" "$ROOT" || true

# Reconcile observed identity signals back into people.yaml.
"$ROOT/.venv/bin/python" "$ROOT/derive/identity_reconcile.py" \
  >> "$ROOT/logs/identity_reconcile.log" 2>&1 || true
set -e

exit $exit_code

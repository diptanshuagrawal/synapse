#!/usr/bin/env bash
# backfill-all-channels.sh — one-shot N-day backfill across EVERY channel in
# config/slack_channels.yaml.
#
# Sequential by design: a single Slack user token is rate-limit-bound (tier-3
# ~50/min, SlackClient self-throttles at 45/min), so parallel runs give zero
# throughput gain and only add sqlite write + cursor-file contention. Uses
# --cursor-mode force to re-fetch the full window regardless of the stored
# cursor — picks up late replies to old threads (the §10.9 fix) within window.
#
# Self-waits for any running ingest/discover cron fire so it never contends.
# Continues on per-channel failure. Usage: bin/backfill-all-channels.sh [DAYS]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

DAYS="${1:-60}"
LOG="state/backfill_all_$(date +%Y%m%d_%H%M%S).log"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

log "waiting for ingest/discover cron to clear..."
while pgrep -f 'slack_ingest_app.py' >/dev/null 2>&1 || pgrep -f 'slack_discover_channels.py' >/dev/null 2>&1; do
  sleep 5
done
log "cron clear — starting backfill (days=$DAYS, cursor-mode=force)"

# Channel-id list (post-discover) from yaml — bash 3.2 compatible (no mapfile).
IDS=()
while IFS= read -r id; do
  [ -n "$id" ] && IDS+=("$id")
done < <(.venv/bin/python -c "import yaml; print('\n'.join(c['id'] for c in yaml.safe_load(open('config/slack_channels.yaml')).get('channels', []) if c.get('id') and c['id'] != 'TODO'))")

TOTAL=${#IDS[@]}
log "$TOTAL channels to backfill"

ok=0; fail=0; i=0
for id in "${IDS[@]}"; do
  i=$((i + 1))
  log "($i/$TOTAL) $id ..."
  if .venv/bin/python ingest/slack_backfill_app.py "$id" --days "$DAYS" --cursor-mode force >>"$LOG" 2>&1; then
    ok=$((ok + 1))
  else
    fail=$((fail + 1))
    log "FAIL $id (continuing)"
  fi
done

log "DONE — ok=$ok fail=$fail total=$TOTAL  log=$LOG"

#!/usr/bin/env bash
set -euo pipefail

# Weekly slack channel discovery + auto-apply wrapper.
#
# Runs slack_discover_channels.py in --auto-mode + --apply + --prune: appends
# `auto_full` and `auto_team_involved` rows to config/slack_channels.yaml AND
# removes dead channels (archived, or stale auto-discovered with no team
# activity in 45d), writes proposals JSON for cron-status DISCOVERY block, and
# validates the resulting yaml. Next slack-ingest fire (every :00/:30 IST
# 12-22h) auto-bootstraps newly-added channels from now-365d via PAGE_CAP.
#
# Safety rails kept:
#   - Activity floor in the script drops candidates below 5 team msgs/90d
#     (1 for MPIMs) into `needs_review` — NOT applied.
#   - Prune only removes archived (any class) + stale `auto-discovered`
#     channels; hand-curated channels are never pruned for staleness. A
#     blast-radius cap (--prune-max-frac 0.30) aborts the prune if a Slack-API
#     blip would remove too many at once.
#   - Pre-apply yaml snapshot at state/slack_channels.yaml.bak.<ts>
#     so a bad batch (add OR prune) is recoverable.
#   - Validator runs after apply; non-zero exit doesn't mutate JSON cache.
#   - Stale MPIMs (>30d quiet) pruned weekly by housekeeping pruner step 7.
#
# Fired by launchagents/com.example.slack-discover.plist (Wed+Fri 13:00 IST).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

mkdir -p "$ROOT/state" "$ROOT/logs"

JSON_OUT="$ROOT/state/last_slack_discover.json"
JSON_TMP="$JSON_OUT.tmp"
YAML="$ROOT/config/slack_channels.yaml"
TS="$(date +%Y%m%d-%H%M%S)"
BAK="$ROOT/state/slack_channels.yaml.bak.$TS"

# Pre-apply snapshot for rollback.
cp "$YAML" "$BAK"

set +e
"$ROOT/.venv/bin/python" "$ROOT/derive/slack_discover_channels.py" \
    --auto-mode \
    --top 500 \
    --apply \
    --prune \
    --json-out "$JSON_TMP"
exit_code=$?
set -e

if [[ $exit_code -eq 0 && -s "$JSON_TMP" ]]; then
    mv "$JSON_TMP" "$JSON_OUT"
else
    rm -f "$JSON_TMP"
fi

# Post-apply yaml validate. Roll back if validator finds FAIL findings.
if [[ $exit_code -eq 0 ]]; then
    set +e
    "$ROOT/.venv/bin/python" "$ROOT/derive/slack_validate.py" --json \
        > "$ROOT/state/last_slack_validate.json.tmp" 2>/dev/null
    val_rc=$?
    set -e
    if [[ $val_rc -eq 0 ]]; then
        mv "$ROOT/state/last_slack_validate.json.tmp" "$ROOT/state/last_slack_validate.json"
    else
        # Validator non-zero: leave the prior cache, but DON'T roll back
        # yaml — non-FAIL findings (warnings) are normal post-apply.
        rm -f "$ROOT/state/last_slack_validate.json.tmp"
    fi
fi

# Keep only the 4 most-recent backups.
ls -1t "$ROOT/state/slack_channels.yaml.bak."* 2>/dev/null | tail -n +5 | xargs -I{} rm -f {} || true

exit $exit_code

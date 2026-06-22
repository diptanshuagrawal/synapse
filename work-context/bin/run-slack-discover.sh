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

# ── User-group (subteam) discovery — PROPOSE ONLY ──
# Scans usergroups.list for groups the owner / team belong to that aren't in
# config/team_subteams.yaml, buckets them into manager (owner_member) vs team
# layers, and writes a proposal JSON. It NEVER auto-writes config: manager-vs-
# team-vs-noise can't be decided from membership alone, so the owner applies
# the layers explicitly with:
#   python -m derive.slack_discover_usergroups --apply-manager <ids>
#   python -m derive.slack_discover_usergroups --apply-team    <ids>
#   python -m derive.slack_discover_usergroups --skip          <ids>   # silence noise
# Non-fatal: a usergroups API hiccup must not fail the channel job above.
set +e
"$ROOT/.venv/bin/python" "$ROOT/derive/slack_discover_usergroups.py" \
    --json-out "$ROOT/state/last_slack_discover_usergroups.json" \
    > "$ROOT/logs/slack_discover_usergroups.log" 2>&1

# Post the approve/reject card to Slack (Manager / Team / Reject per group).
# relay_bot needs slack_sdk/slack_bolt — pick a python that has them. Skips
# cleanly if nothing pending or no channel configured. Owner clicks are handled
# by the always-on relay-bot listener (com.diptanshu.relay-bot).
RELAY_PY="$(for p in /opt/homebrew/bin/python3 python3 "$ROOT/.venv/bin/python"; do
    "$p" -c 'import slack_sdk' 2>/dev/null && { echo "$p"; break; }; done)"
if [[ -n "$RELAY_PY" ]]; then
    "$RELAY_PY" "$ROOT/../bin/relay_bot.py" --post-usergroups "$(date +%Y-%m-%d)" \
        >> "$ROOT/logs/slack_discover_usergroups.log" 2>&1
else
    echo "relay post skipped: no python with slack_sdk" >> "$ROOT/logs/slack_discover_usergroups.log"
fi
set -e

exit $exit_code

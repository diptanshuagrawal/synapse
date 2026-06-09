#!/usr/bin/env bash
# Manual rollup with auto-classification when Claude Code OAuth is available.
#
# Usage:
#   manual-rollup.sh            # phase=dump (default)
#   manual-rollup.sh apply      # phase=apply (after chat writes verdicts.json)
#
# Workflow (auth present — Claude auto-classifies):
#   1. manual-rollup.sh → rollup.py classifies via LLM → dump finds 0 pending → done
#
# Workflow (auth absent — ⚠ warning printed upfront):
#   1. manual-rollup.sh → keyword-only pass → dumps uncached to pending_classification.json
#   2. Paste pending_classification.json into chat; save verdicts to state/verdicts.json
#   3. manual-rollup.sh apply → inserts verdicts → reruns rollup → done

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE="$ROOT/state"
PENDING="$STATE/pending_classification.json"
VERDICTS="$STATE/verdicts.json"
PENDING_N="$STATE/pending_narrative.json"
NARRATIVES="$STATE/narratives.json"
PENDING_SLUGS="$STATE/pending_slug_creation.json"
SLUG_VERDICTS="$STATE/verdicts.epic_slugs.json"
DAYS="${DAYS:-240}"
NARRATIVE="${NARRATIVE:-0}"   # 0 = skip rollup narrative (default), 1 = let rollup.py call Claude API
ACTOR="${ACTOR:-}"            # restrict narrate-dump to one handle
PY="$ROOT/.venv/bin/python"

mkdir -p "$STATE"

# ── auth resolution ──────────────────────────────────────────────────────────
# Priority: Claude Code OAuth > paid API key > none (keyword fallback, warned).
ANTHROPIC_KEY_FILE="$HOME/.secrets/anthropic_api_key"
KEYCHAIN_ITEM="Claude Code-credentials"
LINUX_CREDS="$HOME/.claude/.credentials.json"
GITHUB_TOKEN_FILE="$HOME/.secrets/github_pat"

AUTH_STATUS=""
CREDS_JSON=""
case "$(uname)" in
  Darwin)
    CREDS_JSON="$(security find-generic-password -s "$KEYCHAIN_ITEM" -w 2>/dev/null || true)"
    ;;
  Linux)
    [[ -f "$LINUX_CREDS" ]] && CREDS_JSON="$(cat "$LINUX_CREDS")"
    ;;
esac

if [[ -n "$CREDS_JSON" ]]; then
  ACCESS_TOKEN="$(echo "$CREDS_JSON" | jq -r '.accessToken // .claudeAiOauth.accessToken // empty')"
  EXPIRES_AT="$(echo "$CREDS_JSON" | jq -r '.expiresAt // .claudeAiOauth.expiresAt // 0')"
  NOW_MS=$(($(date +%s) * 1000))
  if [[ -n "$ACCESS_TOKEN" && "$EXPIRES_AT" -gt "$NOW_MS" ]]; then
    export ANTHROPIC_AUTH_TOKEN="$ACCESS_TOKEN"
    ttl_min=$(( (EXPIRES_AT - NOW_MS) / 60000 ))
    AUTH_STATUS="✓ Claude Code OAuth (ttl ${ttl_min}m)"
  elif [[ -f "$ANTHROPIC_KEY_FILE" ]]; then
    export ANTHROPIC_API_KEY
    ANTHROPIC_API_KEY="$(cat "$ANTHROPIC_KEY_FILE")"
    AUTH_STATUS="✓ API key (~/.secrets/anthropic_api_key) — OAuth expired"
  else
    AUTH_STATUS="⚠  MISSING — keyword fallback only; chat classification needed"
  fi
elif [[ -f "$ANTHROPIC_KEY_FILE" ]]; then
  export ANTHROPIC_API_KEY
  ANTHROPIC_API_KEY="$(cat "$ANTHROPIC_KEY_FILE")"
  AUTH_STATUS="✓ API key (~/.secrets/anthropic_api_key)"
else
  AUTH_STATUS="⚠  MISSING — keyword fallback only; chat classification needed"
fi

if [[ -f "$GITHUB_TOKEN_FILE" ]]; then
  export GITHUB_TOKEN
  GITHUB_TOKEN="$(cat "$GITHUB_TOKEN_FILE")"
  GH_STATUS="✓ ~/.secrets/github_pat"
elif [[ -n "${GITHUB_TOKEN:-}" ]]; then
  GH_STATUS="✓ env"
else
  GH_STATUS="⚠  MISSING — no diff fetch; low-conf PRs deferred to chat"
fi

preflight() {
  echo "─────────────────────────────────────────────────────────"
  echo "  manual-rollup  DAYS=$DAYS  NARRATIVE=$NARRATIVE"
  echo "  Anthropic auth : $AUTH_STATUS  (not used — chat classifies)"
  echo "  GitHub token   : $GH_STATUS  (used by ingest scripts only)"
  echo "─────────────────────────────────────────────────────────"
}

run_rollup() {
  # Strip Anthropic auth so classifier short-circuits to keyword fallback.
  # Avoids competing with the active chat session for OAuth quota.
  # Chat session handles LLM classification (step 2 below).
  local -x ANTHROPIC_API_KEY="" ANTHROPIC_AUTH_TOKEN=""
  rm -f "$STATE/last_rollup_success.date"
  local extra=()
  [[ "$NARRATIVE" == "1" ]] || extra+=(--skip-narrative)
  "$PY" "$SCRIPT_DIR/rollup.py" --days "$DAYS" "${extra[@]}"
}

phase_dump() {
  run_rollup
  # Gate: if rollup emitted unmapped-epic context, halt for chat-LLM slug creation.
  if [[ -f "$PENDING_SLUGS" ]]; then
    scnt=$(/usr/bin/env python3 -c "import json;print(len(json.load(open('$PENDING_SLUGS'))))")
    if [[ "$scnt" -gt 0 ]]; then
      cat <<EOF

→ $scnt epic(s) need slug creation: $PENDING_SLUGS

In the Claude Code chat session, run:
  /slug-epics

Then, after $SLUG_VERDICTS is written:
  $0 apply-slugs

(re-run \`$0 dump\` afterwards to continue with classification)
EOF
      exit 0
    fi
  fi
  "$PY" "$SCRIPT_DIR/dump_pending.py" --days "$DAYS" --out "$PENDING"
  cnt=$(/usr/bin/env python3 -c "import json,sys;print(len(json.load(open('$PENDING'))))")
  if [[ "$cnt" -eq 0 ]]; then
    echo "✓ no pending classifications — rollup complete"
    exit 0
  fi
  cat <<EOF

→ $cnt pending subjects dumped: $PENDING

In the Claude Code chat session, run:
  /classify

Then, after verdicts.json is written:
  $0 apply
EOF
}

phase_apply_slugs() {
  if [[ ! -f "$SLUG_VERDICTS" ]]; then
    echo "missing $SLUG_VERDICTS — emit verdicts.epic_slugs.json first" >&2
    exit 1
  fi
  "$PY" "$SCRIPT_DIR/apply_epic_slugs.py" --in "$SLUG_VERDICTS"
  echo "✓ epic slugs applied to projects.yaml — re-run \`$0 dump\` to continue."
}

phase_apply() {
  if [[ ! -f "$VERDICTS" ]]; then
    echo "missing $VERDICTS — emit verdicts JSON first" >&2
    exit 1
  fi
  "$PY" "$SCRIPT_DIR/apply_verdicts.py" --in "$VERDICTS" --pending "$PENDING"
  # Deterministic ownership post-pass (author/commenter overrides, channel-join
  # → external, pots-author attribution). Idempotent; re-applies every rollup.
  "$PY" "$SCRIPT_DIR/ownership_corrections.py"
  # Derive per-cluster owner distribution (home_team_owned_pct) from the
  # corrected subject-level ownership. Feeds /ask + /retro filters.
  "$PY" "$SCRIPT_DIR/cluster_ownership_rollup.py" >/dev/null
  run_rollup
  stamp="$(date +%Y%m%dT%H%M%S)"
  mv "$VERDICTS" "$STATE/verdicts.$stamp.json"
  rm -f "$PENDING"
  echo "✓ applied + corrected + cluster-rollup + rolled up. archived → $STATE/verdicts.$stamp.json"
}

phase_narrate_dump() {
  local extra=()
  [[ -n "$ACTOR" ]] && extra+=(--actor "$ACTOR")
  "$PY" "$SCRIPT_DIR/dump_pending_narrative.py" --days "$DAYS" --out "$PENDING_N" "${extra[@]}"
  cnt=$(/usr/bin/env python3 -c "import json;print(len(json.load(open('$PENDING_N'))))")
  if [[ "$cnt" -eq 0 ]]; then
    echo "✓ no pending narratives (all actors cache-hit)"
    exit 0
  fi
  cat <<EOF

→ $cnt actor(s) pending narrative: $PENDING_N

Next:
  1. In chat, ask Claude to write narratives following the rules at $PENDING_N.rules.md
  2. Save narratives JSON to: $NARRATIVES
     [{ "actor": <echo>, "content_hash": <echo>, "window_days": <echo>, "body": "<markdown>" }, ...]
  3. Re-run: $0 narrate-apply
EOF
}

phase_narrate_apply() {
  if [[ ! -f "$NARRATIVES" ]]; then
    echo "missing $NARRATIVES — emit narratives JSON first" >&2
    exit 1
  fi
  "$PY" "$SCRIPT_DIR/apply_narratives.py" --in "$NARRATIVES" --pending "$PENDING_N"
  stamp="$(date +%Y%m%dT%H%M%S)"
  mv "$NARRATIVES" "$STATE/narratives.$stamp.json"
  rm -f "$PENDING_N"
  echo "✓ narratives applied. archived → $STATE/narratives.$stamp.json"
  echo "  (rerun rollup to fold narratives into person profiles)"
}

preflight

case "${1:-dump}" in
  dump)            phase_dump          ;;
  apply)           phase_apply         ;;
  apply-slugs)     phase_apply_slugs   ;;
  narrate-dump)    phase_narrate_dump  ;;
  narrate-apply)   phase_narrate_apply ;;
  *)               echo "usage: $0 [dump|apply|apply-slugs|narrate-dump|narrate-apply]" >&2; exit 2 ;;
esac

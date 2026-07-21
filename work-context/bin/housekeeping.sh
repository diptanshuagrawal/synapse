#!/usr/bin/env bash
# Project housekeeping: prune old backups, verdicts, handoffs, logs, .DS_Store.
# Default: dry-run. Pass --apply to actually delete.
#
# Modes:
#   (no arg)   dry-run prune — list what --apply would delete
#   --apply    apply the deterministic prune (steps 1-7)
#   --scan     emit FURTHER cleanup candidates as JSON (no deletion); the Claude
#              classification layer + Slack Approve/Reject act on these. See
#              derive/housekeeping_scan.py and scheduled-tasks/housekeeping-review.

set -euo pipefail

ROOT="${CONTEXT_ROOT:-$HOME/context}"
WC="$ROOT/work-context"

MODE="dry-run"
case "${1:-}" in
  --apply) MODE="apply" ;;
  --scan)  MODE="scan" ;;
esac

# --scan: deterministic candidate emitter, no deletion. Runs the python scanner
# (facts only) and exits; classification + action happen downstream.
if [[ "$MODE" == "scan" ]]; then
  if [[ -x "$WC/.venv/bin/python" ]]; then
    (cd "$WC" && CONTEXT_ROOT="$ROOT" "$WC/.venv/bin/python" -m derive.housekeeping_scan \
      --out "$WC/state/housekeeping_candidates.json")
    exit $?
  else
    echo "SKIP: .venv/bin/python not found — cannot run housekeeping_scan" >&2
    exit 1
  fi
fi

# Thresholds in days
DAYS_BAK=60
DAYS_VERDICTS=15
DAYS_HANDOFF=15
DAYS_DERIVED_VERDICTS=15
DAYS_LOGS=60

bytes_to_human() {
  local b=$1
  if (( b > 1073741824 )); then
    awk -v b="$b" 'BEGIN{printf "%.1fG", b/1073741824}'
  elif (( b > 1048576 )); then
    awk -v b="$b" 'BEGIN{printf "%.1fM", b/1048576}'
  elif (( b > 1024 )); then
    awk -v b="$b" 'BEGIN{printf "%.1fK", b/1024}'
  else
    echo "${b}B"
  fi
}

TOTAL_BYTES=0
TOTAL_FILES=0

# Print + act
process() {
  local label="$1" path="$2"
  local size
  size=$(stat -f %z "$path" 2>/dev/null || echo 0)
  TOTAL_BYTES=$((TOTAL_BYTES + size))
  TOTAL_FILES=$((TOTAL_FILES + 1))
  if [[ "$MODE" == "apply" ]]; then
    rm -f "$path"
    printf "  [DELETED] %-30s %8s  %s\n" "$label" "$(bytes_to_human $size)" "$path"
  else
    printf "  [DRY-RUN] %-30s %8s  %s\n" "$label" "$(bytes_to_human $size)" "$path"
  fi
}

truncate_log() {
  local path="$1"
  local size
  size=$(stat -f %z "$path" 2>/dev/null || echo 0)
  TOTAL_BYTES=$((TOTAL_BYTES + size))
  TOTAL_FILES=$((TOTAL_FILES + 1))
  if [[ "$MODE" == "apply" ]]; then
    : > "$path"
    printf "  [TRUNCD ] %-30s %8s  %s\n" "log>60d" "$(bytes_to_human $size)" "$path"
  else
    printf "  [DRY-RUN] %-30s %8s  %s\n" "log>60d (truncate)" "$(bytes_to_human $size)" "$path"
  fi
}

echo "=== Housekeeping ($MODE) ==="
echo "Today: $(date +%Y-%m-%d)"
echo

# 1. DB backups > 60d
echo "[1] events.db backups > ${DAYS_BAK}d:"
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  process "bak>60d" "$f"
done < <(find "$WC/index" -maxdepth 1 -name "events.db.bak*" -type f -mtime +${DAYS_BAK} 2>/dev/null)

# 2. State verdicts > 15d
echo "[2] state/verdicts.*.json > ${DAYS_VERDICTS}d:"
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  process "verdict>15d" "$f"
done < <(find "$WC/state" -maxdepth 1 -name "verdicts.*.json" -type f -mtime +${DAYS_VERDICTS} 2>/dev/null)

# 3. Handoff files > 15d (root + work-context, NOT recursive)
echo "[3] handoff-*.md > ${DAYS_HANDOFF}d:"
for dir in "$ROOT" "$WC"; do
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    process "handoff>15d" "$f"
  done < <(find "$dir" -maxdepth 1 -name "handoff-*.md" -type f -mtime +${DAYS_HANDOFF} 2>/dev/null)
done

# 4. Derived verdicts > 15d, keep latest.json
echo "[4] derived/verdicts/*.json > ${DAYS_DERIVED_VERDICTS}d (keep latest.json):"
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  [[ "$(basename "$f")" == "latest.json" ]] && continue
  process "dverdict>15d" "$f"
done < <(find "$WC/derived/verdicts" -maxdepth 1 -name "*.json" -type f -mtime +${DAYS_DERIVED_VERDICTS} 2>/dev/null)

# 5. Logs > 60d (truncate)
echo "[5] logs/*.log > ${DAYS_LOGS}d (truncate to 0):"
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  truncate_log "$f"
done < <(find "$WC/logs" -maxdepth 1 -name "*.log" -type f -mtime +${DAYS_LOGS} 2>/dev/null)

# 6. .DS_Store everywhere (no age filter)
echo "[6] .DS_Store files:"
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  process ".DS_Store" "$f"
done < <(find "$ROOT" -name ".DS_Store" -type f 2>/dev/null)

# 7. Stale MPIM prune (quiet > 30d) — yaml row + cursor removed, events.db preserved.
echo "[7] stale MPIM prune (quiet > 30d):"
if [[ -x "$WC/.venv/bin/python" ]]; then
  if [[ "$MODE" == "apply" ]]; then
    (cd "$WC" && "$WC/.venv/bin/python" -m derive.slack_prune_stale_mpims --apply 2>&1) \
      | sed 's/^/  /' || echo "  WARN: pruner exited non-zero"
  else
    (cd "$WC" && "$WC/.venv/bin/python" -m derive.slack_prune_stale_mpims 2>&1) \
      | sed 's/^/  /' || echo "  WARN: pruner exited non-zero"
  fi
else
  echo "  SKIP: .venv/bin/python not found"
fi

# 8. Steno audio-retention — archived .m4a > 14d + orphan raw .wav streams.
#    Transcripts/notes/events.db are untouched (only heavy audio is reclaimed).
#    Starred meetings (management/meetings/<stem>.star) are NEVER auto-deleted.
#    Self-contained (stdlib only) → runs under venv python or system python3.
echo "[8] steno audio-retention (m4a > 14d + orphan wav; starred exempt):"
HK_PY="$WC/.venv/bin/python"; [[ -x "$HK_PY" ]] || HK_PY="$(command -v python3 || true)"
if [[ -n "$HK_PY" && -f "$ROOT/bin/meet_retention.py" ]]; then
  if [[ "$MODE" == "apply" ]]; then
    "$HK_PY" "$ROOT/bin/meet_retention.py" --apply 2>&1 | sed 's/^/  /' \
      || echo "  WARN: meet_retention exited non-zero"
  else
    "$HK_PY" "$ROOT/bin/meet_retention.py" 2>&1 | sed 's/^/  /' \
      || echo "  WARN: meet_retention exited non-zero"
  fi
else
  echo "  SKIP: python3 or bin/meet_retention.py not found"
fi

echo
echo "=== Summary ==="
echo "Files affected: $TOTAL_FILES"
echo "Bytes affected: $(bytes_to_human $TOTAL_BYTES)"
if [[ "$MODE" == "dry-run" ]]; then
  echo "Re-run with --apply to delete."
fi

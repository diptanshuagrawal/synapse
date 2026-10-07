#!/usr/bin/env bash
# Daily handover-readiness snapshot.
#
# Runs the program team's own readiness CLI (configured under handover_readiness in
# sprint_planning.yaml), caches the result for /api/handover, and appends one history row
# per cycle month.
#
# The snapshot is the point. The published average on its own is not a trend: initiatives
# join the cycle at score 1 and leave whenever they are retagged, so it falls while things
# improve and rises when a red row departs. Only rows + status counts recorded over time
# turn that into something you can read.
#
# Idempotent: re-running the same day overwrites that day's rows rather than duplicating.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
LOG="$ROOT/state/handover-refresh.log"
mkdir -p "$ROOT/state"

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') handover refresh ==="
  HANDOVER_ROOT="$ROOT" "$PY" - <<'EOF'
import json, os, sys
ROOT = os.environ["HANDOVER_ROOT"]
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "derive"))
import capacity_engine as ce

doc = ce.handover_readiness()
if doc.get("__error__"):
    print("FAILED:", doc["__error__"])
    raise SystemExit(1)

with open(os.path.join(ROOT, "derived", "handover.json"), "w") as fh:
    json.dump(doc, fh)

hist = ce.handover_snapshot(doc, os.path.join(ROOT, "state", "handover_history.jsonl"))
for r in ((doc.get("rollup") or {}).get("per_project") or []):
    print(f"  {r.get('project')} {r.get('month')}: {r.get('rows')} rows "
          f"avg {r.get('avg')} green {r.get('pct_green')}% "
          f"(missing epic {r.get('missing_epic')}, budget {r.get('missing_budget')})")
print(f"  history rows: {len(hist or [])}")
EOF
  echo "exit=$?"
} >> "$LOG" 2>&1

tail -20 "$LOG"

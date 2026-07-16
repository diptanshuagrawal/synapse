#!/usr/bin/env bash
# refresh-skeletons.sh — deterministic skeleton refresh + diff gate for the
# daily service-brief routine.
#
# For each target Go service: pin its mirror clone to the remote default branch
# (origin/HEAD — mirrors hold no human work, so reset --hard is safe), rebuild
# the skeleton JSON via go_extractor.py, then compare the new skeleton against
# the previous one IGNORING the volatile `commit` field. A service is reported
# CHANGED only when its structural content (endpoints / tables / kafka / etc.)
# actually differs — so the cron agent spends LLM tokens re-briefing real diffs
# only, never on a mere commit-hash bump.
#
# Output:
#   - last stdout line: "CHANGED: <svc> <svc>"  (space-sep, may be empty)
#   - state/service_brief_changed.json = {"date":..,"changed":[..]}
#
# Zero LLM, zero Anthropic, safe to run unattended. Needs headless git SSH
# (keychain key) to fetch the mirrors.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"   # work-context/
STATE_DIR="$ROOT/state"
LOG="$STATE_DIR/service_brief_$(date +%Y%m%d).log"

# Unattended launchd/routine fires get a bare PATH where python3 = the yaml-less
# system interpreter (/usr/bin/python3); sources_config needs yaml. Probe, fall
# back to the repo venv (which has yaml; slack_sdk not needed here).
PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import yaml' >/dev/null 2>&1; then
  PY="$ROOT/.venv/bin/python3"
fi
if ! "$PY" -c 'import yaml' >/dev/null 2>&1; then
  echo "FATAL: no yaml-capable python3 (tried PATH python3 and $ROOT/.venv/bin/python3)" >&2
  exit 1
fi

# Target Go services come from config (github.codegraph_repos) minus the
# brief-exclude list (graph-only repos); never hardcode.
SVCS=($("$PY" - "$ROOT" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from derive.sources_config import codegraph_repos, service_brief_exclude
skip = set(service_brief_exclude())
print(" ".join(r for r in codegraph_repos() if r not in skip))
PY
))

mkdir -p "$STATE_DIR" "$ROOT/derived/services"
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

resolve_mirror() {
  "$PY" - "$1" <<'PY'
import json, sys, pathlib
reg = json.load(open(pathlib.Path.home() / ".code-review-graph/registry.json"))
repos = reg.get("repos", reg) if isinstance(reg, dict) else reg
svc = sys.argv[1]
# Match by registry alias OR by the mirror dir's basename — config names repos by
# full GitHub repo name while the registry alias may be a short form; alias-only
# matching silently skipped such repos every run.
hit = next((r for r in repos
            if r.get("alias") == svc or pathlib.Path(r.get("path", "")).name == svc), None)
print(hit["path"] if hit else "")
PY
}

changed=()
log "=== service skeleton refresh start (svcs=${SVCS[*]}) ==="
for svc in "${SVCS[@]}"; do
  repo="$(resolve_mirror "$svc")"
  if [ -z "$repo" ] || [ ! -d "$repo/.git" ]; then
    log "ERROR no mirror for $svc ($repo) — skip"
    continue
  fi

  # Pin mirror to remote default branch (deterministic; mirrors hold no work).
  git -C "$repo" fetch --quiet origin 2>>"$LOG" || log "WARN fetch failed: $svc"
  def="$(git -C "$repo" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@')"
  [ -z "$def" ] && def="$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
  git -C "$repo" reset --hard "origin/$def" >>"$LOG" 2>&1 || log "WARN reset failed: $svc"

  out="$ROOT/derived/services/$svc.skeleton.json"
  prev="$STATE_DIR/$svc.skeleton.prev.json"
  if [ -f "$out" ]; then cp "$out" "$prev"; else rm -f "$prev"; fi

  # --out is REQUIRED: the extractor's default is CWD-relative derived/services/,
  # which lands outside the repo whenever the caller's cwd isn't work-context/.
  if ! "$PY" "$SCRIPT_DIR/go_extractor.py" --repo "$repo" --svc "$svc" --out "$out" >>"$LOG" 2>&1; then
    log "ERROR extractor failed: $svc"
    continue
  fi

  head="$(git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo '?')"
  # Content diff ignoring the volatile `commit` field.
  if "$PY" - "$prev" "$out" <<'PY'
import json, sys
def load(p):
    try:
        d = json.load(open(p))
    except Exception:
        return None
    if isinstance(d, dict):
        d.pop("commit", None)
    return d
sys.exit(0 if load(sys.argv[1]) == load(sys.argv[2]) else 1)
PY
  then
    log "$svc: no content change @ $head"
  else
    log "$svc: CHANGED @ $head"
    changed+=("$svc")
  fi
done

"$PY" - "$STATE_DIR/service_brief_changed.json" ${changed[@]+"${changed[@]}"} <<'PY'
import json, sys, datetime
path = sys.argv[1]
changed = sys.argv[2:]
json.dump(
    {"date": datetime.date.today().isoformat(), "changed": changed},
    open(path, "w"), indent=2,
)
PY

log "=== refresh done — changed=[${changed[*]-}] ==="
echo "CHANGED: ${changed[*]-}"

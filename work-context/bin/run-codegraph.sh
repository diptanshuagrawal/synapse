#!/usr/bin/env bash
# run-codegraph.sh — daily code-knowledge-graph refresh for /ask code-logic queries.
#
# DETERMINISTIC: builds the graph from dedicated MIRROR clones under
# ~/.code-review-graph/repos, each hard-reset to its remote default branch
# (origin/HEAD) every run. These mirrors hold NO human work, so reset --hard is
# safe — the dev repos under $HOME/git are never touched. Result: the graph always
# reflects remote main/master regardless of local dev state. Full rebuild each
# run (~90s combined; no incremental bookkeeping). No LLM calls;
# embeddings local/optional. Needs git SSH access headless (keychain key).
set -uo pipefail

CRG="$HOME/.local/bin/code-review-graph"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="$ROOT/state"
LOG="$STATE_DIR/codegraph_$(date +%Y%m%d).log"
MIRROR="$HOME/.code-review-graph/repos"
REPOS=($("$ROOT/.venv/bin/python" -c "import sys; sys.path.insert(0,'$ROOT'); from derive.sources_config import codegraph_repos; print(' '.join(codegraph_repos()))"))

mkdir -p "$STATE_DIR"
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

log "=== codegraph refresh start ==="
ok=0; fail=0
for r in "${REPOS[@]}"; do
  repo="$MIRROR/$r"
  if [ ! -d "$repo/.git" ]; then log "ERROR missing mirror: $repo"; fail=$((fail + 1)); continue; fi

  if ! git -C "$repo" fetch --quiet origin 2>>"$LOG"; then
    log "WARN fetch failed: $repo (building last-known state)"
  fi

  # Remote default branch (origin/HEAD), e.g. main for service-a, master for service-c.
  def="$(git -C "$repo" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's@^origin/@@')"
  [ -z "$def" ] && def="$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"

  # Mirror holds no human work — hard-reset to remote default is safe + deterministic.
  if git -C "$repo" reset --hard "origin/$def" >>"$LOG" 2>&1; then
    log "$repo: pinned to origin/$def @ $(git -C "$repo" rev-parse --short HEAD)"
  else
    log "$repo: reset to origin/$def failed — building current state"
  fi

  if "$CRG" build --repo "$repo" >>"$LOG" 2>&1; then
    ok=$((ok + 1)); log "$repo: graph rebuilt"
  else
    fail=$((fail + 1)); log "ERROR build failed: $repo"
  fi
done

if [ "$fail" -eq 0 ]; then date +%F > "$STATE_DIR/last_codegraph_success.date"; fi
log "=== codegraph refresh done — ok=$ok fail=$fail ==="

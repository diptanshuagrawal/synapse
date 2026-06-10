#!/usr/bin/env bash
# Bootstrap Claude Code routines (scheduled agents) for THIS machine.
#
# Sibling of install-agents.sh — but routines differ from launchd crons in one
# key way: they are REGISTERED through the scheduled-tasks MCP, not loaded by a
# shell command. A plain script cannot call MCP. So this script does the half it
# can (materialise the templated SKILL.md files into ~/.claude/scheduled-tasks/)
# and then PRINTS the create_scheduled_task registration payloads from
# scheduled-tasks/routines.yaml for Claude to register.
#
# A routine whose `needs` env var is unset is SKIPPED (not written) so an
# unattended re-run can never blank out a live routine's channel/MCP id.
#
# Usage:
#   SLACK_MCP_SERVER=<server-id> STANDUP_CHANNEL=<channel-id> bin/install-routines.sh
#
# Env vars (only needed by the routines that reference them — see routines.yaml `needs`):
#   SLACK_MCP_SERVER  — the Slack MCP server id (the hash in mcp__<id>__slack_*)
#   STANDUP_CHANNEL   — Slack channel id the daily standup posts to
#   ROUTINES_DST      — override the install dir (default ~/.claude/scheduled-tasks); for dry-runs
set -euo pipefail

WORKCTX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # .../work-context
REPO_ROOT="$(cd "$WORKCTX/.." && pwd)"                        # repo root (replaces __REPO__)
SRC="$WORKCTX/scheduled-tasks"
DST="${ROUTINES_DST:-$HOME/.claude/scheduled-tasks}"
MANIFEST="$SRC/routines.yaml"
PY="$WORKCTX/.venv/bin/python"

[[ -f "$MANIFEST" ]] || { echo "ERROR: manifest not found: $MANIFEST" >&2; exit 1; }

REPO_ROOT="$REPO_ROOT" HOME_DIR="$HOME" SRC="$SRC" DST="$DST" \
SLACK_MCP="${SLACK_MCP_SERVER:-}" STANDUP_CHANNEL="${STANDUP_CHANNEL:-}" \
"$PY" - "$MANIFEST" <<'PYEOF'
import json, os, sys
from pathlib import Path
import yaml

manifest = sys.argv[1]
repo_root = os.environ["REPO_ROOT"]
home_dir  = os.environ["HOME_DIR"]
src = Path(os.environ["SRC"])
dst = Path(os.environ["DST"])
subs = {
    "__REPO__": repo_root,
    "__HOME__": home_dir,
    "__SLACK_MCP__": os.environ.get("SLACK_MCP", ""),
    "__STANDUP_CHANNEL__": os.environ.get("STANDUP_CHANNEL", ""),
}
# Which env vars back each `needs` token.
NEED_OK = {
    "slack_mcp": bool(subs["__SLACK_MCP__"]),
    "standup_channel": bool(subs["__STANDUP_CHANNEL__"]),
}

m = yaml.safe_load(open(manifest))
routines = m.get("routines", [])

print(f"Materialising routine SKILL.md files → {dst}\n")
ready = []
for r in routines:
    rid = r["id"]
    missing = [n for n in (r.get("needs") or []) if not NEED_OK.get(n, True)]
    if missing:
        print(f"SKIP {rid}  — missing env: {', '.join(missing)} (left untouched)")
        continue
    tmpl = src / rid / "SKILL.md"
    if not tmpl.exists():
        print(f"SKIP {rid}  — template not found: {tmpl}")
        continue
    text = tmpl.read_text()
    for k, v in subs.items():
        text = text.replace(k, v)
    out_dir = dst / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "SKILL.md"
    out.write_text(text)
    print(f"OK   {rid}  → {out}")
    ready.append(r)

print("\n" + "═" * 72)
print(" REGISTRATION — files are in place, but routines must be REGISTERED via")
print(" the scheduled-tasks MCP. Ask Claude to run, for each routine below,")
print(f" the create_scheduled_task tool with the given fields. (cwd = {repo_root})")
print("═" * 72 + "\n")

for r in ready:
    payload = {
        "taskId": r["id"],
        "cronExpression": r["cron"],
        "enabled": bool(r.get("enabled")),
        "filePath": str(dst / r["id"] / "SKILL.md"),
        "cwd": repo_root,
    }
    if r.get("permissions"):
        payload["approvedPermissions"] = [{"toolName": p} for p in r["permissions"]]
    print(f"# {r['id']}")
    print(json.dumps(payload, indent=2))
    print()

skipped = [r["id"] for r in routines if r not in ready]
if skipped:
    print(f"Skipped (set the missing env var, then re-run): {', '.join(skipped)}")
print("Note: enabled=false routines are registered but dormant — flip them on")
print("with the scheduled-tasks MCP update tool once you're ready.")
PYEOF

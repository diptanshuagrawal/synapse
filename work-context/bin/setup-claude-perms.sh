#!/usr/bin/env bash
#
# setup-claude-perms.sh — one-time per-machine setup so Claude Code scheduled
# fires don't pause for "Allow once / Allow always" prompts.
#
# Problem: work-context/ has its own .claude/commands/ dir, so Claude Code
# treats it as a separate project root. It does NOT inherit settings from
# the parent context/.claude/settings.local.json (which holds
# `defaultMode: bypassPermissions` + the Slack MCP allow entry). Result:
# every new MCP tool name prompts on first call, breaking unattended fires.
#
# Fix: symlink work-context/.claude/settings.local.json → parent's, then
# both Claude "projects" share the same permission state. If the parent
# file doesn't exist yet, scaffold a minimal one.
#
# Idempotent — safe to re-run.

set -euo pipefail

# Resolve repo root (parent of work-context/) regardless of where this is run.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_CONTEXT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$WORK_CONTEXT_DIR/.." && pwd)"

PARENT_SETTINGS="$REPO_ROOT/.claude/settings.local.json"
WC_SETTINGS="$WORK_CONTEXT_DIR/.claude/settings.local.json"

mkdir -p "$REPO_ROOT/.claude" "$WORK_CONTEXT_DIR/.claude"

# 1. Scaffold parent settings.local.json if missing.
if [[ ! -e "$PARENT_SETTINGS" ]]; then
  echo "→ scaffolding $PARENT_SETTINGS"
  cat > "$PARENT_SETTINGS" <<'JSON'
{
  "permissions": {
    "defaultMode": "bypassPermissions",
    "allow": [
      "mcp__4a0f7cfe-8802-4a6a-9e98-3b69a9229e4a__*",
      "Read(//private/tmp/slack_mcp_cache/**)"
    ]
  }
}
JSON
else
  echo "✓ parent settings exist: $PARENT_SETTINGS"
fi

# 2. Symlink work-context settings → parent.
if [[ -L "$WC_SETTINGS" ]]; then
  current_target="$(readlink "$WC_SETTINGS")"
  if [[ "$current_target" == "../../.claude/settings.local.json" ]]; then
    echo "✓ symlink already in place: $WC_SETTINGS"
    exit 0
  fi
  echo "→ replacing stale symlink ($current_target)"
  rm "$WC_SETTINGS"
elif [[ -e "$WC_SETTINGS" ]]; then
  echo "ERROR: $WC_SETTINGS exists and is NOT a symlink." >&2
  echo "       Move it aside, then re-run this script." >&2
  exit 1
fi

ln -s "../../.claude/settings.local.json" "$WC_SETTINGS"
echo "✓ created symlink: $WC_SETTINGS → ../../.claude/settings.local.json"

echo
echo "Done. Restart Claude Code so the new settings load."

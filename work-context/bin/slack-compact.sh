#!/usr/bin/env bash
# Slack thread compaction wrapper.
#
# Usage:
#   bin/slack-compact.sh dump [--days 365] [--limit 200]
#   bin/slack-compact.sh apply
#
# Workflow (chat-driven; no LLM in scripts):
#   1. bin/slack-compact.sh dump           — writes state/slack_compact_pending.json
#   2. /slack-compact in chat              — produces state/slack_compact_verdicts.json
#   3. bin/slack-compact.sh apply          — applies verdicts, deletes raw events
#
# Compaction never runs unattended. Owner triggers when DB / 1-year window
# warrants it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="$ROOT/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
    echo "error: $PY not found — run from work-context root with .venv installed" >&2
    exit 1
fi

cd "$ROOT"
exec "$PY" ingest/slack-compact.py "$@"

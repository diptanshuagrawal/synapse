#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"        # work-context/
REPO="$(cd "$ROOT/.." && pwd)"              # context/

# IC rota bot: T-2d stint reminder + #on-call topic sync.
# Idempotent per fire: reminders dedupe via state/ic_rota_state.json, topic
# sync no-ops when the topic already matches (live) / was already echoed (test).
# Mode (test|live) lives in config/ic_rota.yaml — this wrapper never overrides it.

mkdir -p "$ROOT/logs"
"$ROOT/.venv/bin/python" "$REPO/bin/ic_rota_bot.py" --remind --sync-topic \
  >> "$ROOT/logs/ic_rota.log" 2>&1

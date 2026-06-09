#!/usr/bin/env bash
# Standalone narrative-dump for one engineer.
# Usage: narrate-one.sh <github_handle> [days]
#   e.g. narrate-one.sh example-dev 7

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <github_handle> [days=28]" >&2
  exit 2
fi

ACTOR="$1"
DAYS="${2:-28}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ACTOR="$ACTOR" DAYS="$DAYS" "$SCRIPT_DIR/manual-rollup.sh" narrate-dump

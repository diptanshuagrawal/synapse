#!/usr/bin/env bash
# Run the ingest / derive unit + integration test suite.
# Installs pytest into the project venv on first use, then runs it.
# Usage:
#   bin/run-tests.sh                 # full suite, quiet
#   bin/run-tests.sh -v              # verbose
#   bin/run-tests.sh tests/test_common_enrich_refs.py   # one file
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="$ROOT/.venv/bin/python"

if ! "$PY" -c "import pytest" 2>/dev/null; then
  echo "pytest not found in venv — installing…" >&2
  "$PY" -m pip install -q pytest
fi

cd "$ROOT"
exec "$PY" -m pytest "$@"

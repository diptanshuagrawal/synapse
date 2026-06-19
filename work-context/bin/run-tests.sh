#!/usr/bin/env bash
# Run the ingest / derive unit + integration test suite.
# Installs pytest(+cov) into the project venv on first use, then runs it.
# Usage:
#   bin/run-tests.sh                 # FULL suite + coverage floor (--cov-fail-under)
#   bin/run-tests.sh -v              # full suite, verbose, + floor
#   bin/run-tests.sh tests/test_common_enrich_refs.py   # one file, NO floor
#
# The coverage floor (COV_FAIL_UNDER) is enforced only on a full-suite run — an
# ad-hoc single-file run would trivially be under the floor, so we skip it there.
# A "full run" = no positional test path given (flags like -v are fine).
set -euo pipefail

COV_FAIL_UNDER="${COV_FAIL_UNDER:-33}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="$ROOT/.venv/bin/python"

if ! "$PY" -c "import pytest" 2>/dev/null; then
  echo "pytest not found in venv — installing…" >&2
  "$PY" -m pip install -q pytest pytest-cov
fi

# Detect whether a positional test path was passed (vs only flags).
has_path=0
for arg in "$@"; do
  case "$arg" in
    -*) ;;                 # a flag — ignore
    *) has_path=1 ;;       # a path/expr — single/partial run
  esac
done

cd "$ROOT"
if [ "$has_path" -eq 0 ] && "$PY" -c "import pytest_cov" 2>/dev/null; then
  exec "$PY" -m pytest --cov --cov-report=term-missing:skip-covered \
       --cov-fail-under="$COV_FAIL_UNDER" "$@"
fi
exec "$PY" -m pytest "$@"

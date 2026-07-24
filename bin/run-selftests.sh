#!/usr/bin/env bash
# run-selftests.sh — discover & run every module selftest in this repo.
# A "selftest" is a module defining `_selftest()` and calling it under
# `if __name__ == "__main__"`. Hermetic (in-memory fixtures, no live DB).
# Exit 0 iff every selftest passes. Invoked standalone or from preflight.sh.
set -uo pipefail

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO/work-context"
PYBIN="$REPO/work-context/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN=python3

# Modules that expose a selftest, as `python3 -m` dotted paths (package context
# so intra-repo `from derive.x import ...` resolves).
FILES="$(grep -rl 'def _selftest' derive --include='*.py' 2>/dev/null | grep -v __pycache__ | sort)"
[ -z "$FILES" ] && { echo "run-selftests: no selftests found"; exit 0; }

fail=0
for f in $FILES; do
  mod="$(echo "$f" | sed 's#/#.#g; s#\.py$##')"   # derive/jira_metrics.py -> derive.jira_metrics
  echo "==> $mod"
  if ! "$PYBIN" -m "$mod"; then echo "   FAIL: $mod"; fail=1; fi
done

# Shell-level regression tests (bin/test_*.sh). These guard the transcription
# sweep, which is bash, not Python — self-skip when their toolchain is absent.
for t in "$REPO"/bin/test_*.sh; do
  [ -f "$t" ] || continue
  echo "==> $(basename "$t")"
  if ! bash "$t"; then echo "   FAIL: $(basename "$t")"; fail=1; fi
done

if [ "$fail" -ne 0 ]; then echo ""; echo "run-selftests: FAILED"; exit 1; fi
echo ""; echo "run-selftests: PASS"

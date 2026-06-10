#!/usr/bin/env bash
# preflight.sh — pre-push leak gate for THIS repo. Run from anywhere inside it.
#
# Scans the git-visible tree (tracked + untracked-not-ignored) IN PLACE — no
# staging, no copy — against the leak denylists, checks filenames, and
# py_compiles all Python. With --push, pushes to origin/main only if clean.
#
# Denylist sources (both used if present):
#   .githooks/leak-patterns.txt   generic, tracked, ships
#   .publish-denylist.txt         real org tokens, gitignored, local-only
set -uo pipefail

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"
DO_PUSH=0; [ "${1:-}" = "--push" ] && DO_PUSH=1

PATS="$(grep -hvE '^[[:space:]]*(#|$)' .githooks/leak-patterns.txt .publish-denylist.txt 2>/dev/null || true)"
[ -z "$PATS" ] && { echo "preflight: no denylist patterns found (.githooks/leak-patterns.txt)"; exit 1; }

# Lines carrying an intentional-placeholder marker are allowed.
ALLOW='EXAMPLE|PLACEHOLDER|yourorg|yourcompany|sample|noreply|OWNER|ALICE|BOB|CAROL|DAN|EVE|FRANK|GRACE|HENRY|IVAN'

FILES="$(git ls-files --cached --others --exclude-standard)"
fail=0

echo "==> content scan"
HITS="$(echo "$FILES" | while IFS= read -r f; do
          [ -f "$f" ] && grep -HnIE -f <(echo "$PATS") "$f" 2>/dev/null
        done | grep -vE "$ALLOW" || true)"
if [ -n "$HITS" ]; then echo "$HITS" | head -40; fail=1; else echo "   clean"; fi

echo "==> filename scan"
FHITS="$(echo "$FILES" | grep -iE -f <(echo "$PATS") 2>/dev/null || true)"
if [ -n "$FHITS" ]; then echo "$FHITS" | head -20; fail=1; else echo "   clean"; fi

echo "==> py_compile"
PYF="$(echo "$FILES" | grep -E '\.py$' || true)"
if [ -n "$PYF" ]; then
  PYBIN="$REPO/work-context/.venv/bin/python"; [ -x "$PYBIN" ] || PYBIN=python3
  if ! echo "$PYF" | xargs "$PYBIN" -m py_compile 2>/tmp/preflight_py.txt; then
    cat /tmp/preflight_py.txt; fail=1
  else echo "   ok"; fi
fi

echo "==> selftests"
if ! "$REPO/bin/run-selftests.sh" >/tmp/preflight_st.txt 2>&1; then
  cat /tmp/preflight_st.txt; fail=1
else echo "   ok"; fi

if [ "$fail" -ne 0 ]; then echo ""; echo "preflight: FAILED — fix above; not pushing."; exit 2; fi
echo ""; echo "preflight: PASS"

if [ "$DO_PUSH" = "1" ]; then
  echo "==> pushing origin main"
  env -u GITHUB_TOKEN -u GH_TOKEN git push origin main
fi

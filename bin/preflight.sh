#!/usr/bin/env bash
# preflight.sh — pre-push leak gate for THIS repo. Run from anywhere inside it.
#
# Scans the git-visible tree (tracked + untracked-not-ignored) IN PLACE — no
# staging, no copy — against the leak denylists, checks filenames, and
# py_compiles all Python. With --push, pushes to origin/main only if clean.
#
# Denylist sources (all used if present):
#   .githooks/leak-patterns.txt      generic + structural, tracked, ships
#   .publish-denylist.txt            hand-curated real org tokens, gitignored
#   .publish-denylist.generated.txt  AUTO-derived from config/* (regenerated below)
set -uo pipefail

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"
DO_PUSH=0; [ "${1:-}" = "--push" ] && DO_PUSH=1

# Refresh the config-derived denylist so every real identifier in config/* (emails,
# repos, host, full names, channel/MCP ids) is blocked — no hand-maintenance, no chat
# session in the loop.
#
# NOT best-effort. This was `|| true` on the grounds that a failure here should not block
# the scan, which had it exactly backwards: this generator is most of the denylist, so a
# silent failure means the scan runs and passes while blocking almost nothing.
if [ ! -x "$REPO/bin/gen-denylist.sh" ]; then
  echo "preflight: bin/gen-denylist.sh is missing or not executable, so the scan would"
  echo "   run on the curated list alone and miss every config-derived identifier."
  exit 1
fi
if true; then
  if ! "$REPO/bin/gen-denylist.sh" >/tmp/gen_denylist.txt 2>&1; then
    echo "preflight: the denylist generator failed, so the scan would check almost nothing:"
    sed 's/^/   /' /tmp/gen_denylist.txt
    exit 1
  fi
fi

PATS="$(grep -hvE '^[[:space:]]*(#|$)' .githooks/leak-patterns.txt .publish-denylist.txt .publish-denylist.generated.txt 2>/dev/null || true)"
[ -z "$PATS" ] && { echo "preflight: no denylist patterns found (.githooks/leak-patterns.txt)"; exit 1; }

# Lines carrying an intentional-placeholder marker are allowed.
#
# Matched against the matched LINE only, never the path. This was `grep -vE` over the
# whole `file:line:content` string, so every hit in any file whose PATH contained one of
# these words was silently dropped — a real Slack id in `sample_dir/leak.py` passed the
# gate. These words are common enough in directory names that the gate was fail-open for
# a large part of the tree.
ALLOW='EXAMPLE|PLACEHOLDER|yourorg|yourcompany|sample|noreply|OWNER|ALICE|BOB|CAROL|DAN|EVE|FRANK|GRACE|HENRY|IVAN'

FILES="$(git ls-files --cached --others --exclude-standard)"
fail=0

echo "==> content scan"
# The grep loop is wrapped so its exit status is its own business: under `pipefail` a
# grep that simply found nothing in a file returns 1 and would fail the whole pipeline.
# Only the FILTER's status is worth acting on, because that is the step whose failure
# would make an empty result look like a clean scan.
HITS="$({ echo "$FILES" | while IFS= read -r f; do
          [ -f "$f" ] && grep -HnIE -f <(echo "$PATS") "$f" 2>/dev/null
        done; true; } | ALLOW="$ALLOW" python3 -c '
import os, re, sys

# Bytes, not text. A single non-UTF-8 byte on a matching line used to raise on decode,
# and the error was swallowed, which dropped that hit AND every hit after it while the
# scan still reported clean.
allow = re.compile(br"\b(?:" + os.environ["ALLOW"].encode() + br")\b")
for line in sys.stdin.buffer:
    # file:line:content — only the content may carry a placeholder marker, never the
    # path, or a directory called sample/ hides every leak beneath it.
    parts = line.split(b":", 2)
    body = parts[2] if len(parts) == 3 else line
    # Word-anchored: unanchored, DANGER matched DAN, EVENT matched EVE and OWNERSHIP
    # matched OWNER, so three real ids walked straight through.
    if not allow.search(body):
        sys.stdout.buffer.write(line)
')" || {
  # No `|| true`: a filter that fails means the scan did not run, and an empty result
  # would otherwise read as clean.
  echo "preflight: the placeholder filter failed, so the content scan did not complete"
  exit 1
}
if [ -n "$HITS" ]; then echo "$HITS" | head -40; fail=1; else echo "   clean"; fi

echo "==> filename scan"
FHITS="$(echo "$FILES" | grep -iE -f <(echo "$PATS") 2>/dev/null || true)"
if [ -n "$FHITS" ]; then echo "$FHITS" | head -20; fail=1; else echo "   clean"; fi

# Template placeholder lint: in PUBLISHED templates (scheduled-tasks/** + *.example.*)
# a Slack channel mention must be a placeholder, never a real channel name. This
# catches NEW/unregistered channels (the #standup-updates class) that the denylist
# can't know about. Scoped to template files only → no CSS/markdown false positives.
echo "==> template placeholders"
TFILES="$(echo "$FILES" | grep -E '(^|/)scheduled-tasks/|\.example\.' || true)"
THITS="$(echo "$TFILES" | while IFS= read -r f; do
           [ -f "$f" ] && grep -HnoE '#[a-z][a-z0-9_-]{2,}' "$f" 2>/dev/null
         done | grep -vE '#example|#!' || true)"
if [ -n "$THITS" ]; then
  echo "   real-looking #channel in a template — use a __PLACEHOLDER__ or #example*:"
  echo "$THITS" | head -20; fail=1
else echo "   clean"; fi

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

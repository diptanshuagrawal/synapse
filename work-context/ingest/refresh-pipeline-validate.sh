#!/usr/bin/env bash
# Refresh the cross-cutting pipeline-integrity validate cache
# (state/last_pipeline_validate.json), consumed by bin/cron-status.sh.
#
# This is the cross-source sibling of the per-source validate caches each
# run-<src>.sh writes. It is cheap (a handful of aggregate queries) and
# idempotent, so every ingest fire refreshes it regardless of which source ran
# — whichever wrapper fires last leaves the freshest snapshot.
#
# Fail-soft by contract: a validator error must NEVER affect the caller's exit
# code. Call as:  ingest/refresh-pipeline-validate.sh "$ROOT"
set +e
ROOT="${1:?usage: refresh-pipeline-validate.sh <repo-root>}"
# Per-process tmp ($$): every ingest wrapper fires this concurrently, so a
# shared tmp name lets the first mv win and the rest fail "No such file".
TMP="$ROOT/state/last_pipeline_validate.json.$$.tmp"
"$ROOT/.venv/bin/python" "$ROOT/derive/pipeline_validate.py" --json \
  > "$TMP" 2>/dev/null \
  && mv "$TMP" "$ROOT/state/last_pipeline_validate.json" \
  || rm -f "$TMP"
exit 0

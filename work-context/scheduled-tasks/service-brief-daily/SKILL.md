---
name: service-brief-daily
description: Daily from 18:00 IST (retries every 30 min until it succeeds once) — refresh Go service skeletons and re-brief only those whose skeleton content changed.
---

Daily diff-gated service-brief routine for the engineering-management copilot.

Working dir: __REPO__/work-context

GOAL: keep the service briefs current, but spend LLM tokens ONLY on services whose code skeleton actually changed today. (Which services are in scope is config-driven — see derive/service_derive/refresh-skeletons.sh.)

## RUN-ONCE GATE (idempotent — this routine retries every 30 min until it succeeds once today)
Before doing ANY work, run this and obey it:

    MARK=__REPO__/work-context/state/last_routine_service_brief_success.date
    TODAY=$(TZ=Asia/Kolkata date +%F)
    if [ -f "$MARK" ] && [ "$(cat "$MARK")" = "$TODAY" ]; then echo "GATE: service-brief already succeeded today ($TODAY) — idle"; else echo "GATE: service-brief not done today — proceed"; fi

If it prints "already succeeded today" → STOP NOW: do not refresh skeletons, brief, or ingest anything; end the run. Only proceed to the steps below if it prints "not done today".

## Step 1 — deterministic refresh + diff gate (no LLM)
Run from the working dir:

    bash derive/service_derive/refresh-skeletons.sh

This pins both mirror clones to origin default, rebuilds each `derived/services/<svc>.skeleton.json` via the Go extractor, and diffs each against its previous version IGNORING the volatile `commit` field. Its LAST stdout line is:

    CHANGED: <svc> <svc>     (space-separated; may be empty)

The same list is in `state/service_brief_changed.json`.

## Step 2 — gate
- If the CHANGED list is EMPTY: log one line ("service-brief: no skeleton diffs today, nothing to brief") and STOP. Do not call any LLM, do not write any brief.
- Otherwise continue to Step 3 for EACH changed service.

## Step 3 — fill + ingest the brief for each changed service
The skeletons are already (re)built by Step 1 — do NOT re-run the extractor. For each changed `<svc>`, follow the service-brief skill body at `__REPO__/.claude/commands/service-brief.md`, STEPS 3 through 6 ONLY (skip steps 1–2; the mirror + skeleton are done):
  - Read `derived/services/<svc>.skeleton.json`.
  - Fill the semantic ("why") fields under that skill's HARD RULES (never invent names/paths/tables; copy facts verbatim from the skeleton; `(unknown)` when unsure; counts must equal the skeleton).
  - Write the brief to `derived/services/<svc>.md` in the skill's output format.
  - Persist as DB rows:  python3 derive/service_derive/ingest_briefs.py --svc <svc>

## Step 4 — summary
Print one block per service: svc + commit, counts (endpoints/tables/consumers/producers) confirming they match the skeleton, count of `(unknown)` fields left, and the .md path written. Then list any services that were skipped because unchanged.

## Permission posture (CRITICAL — unattended fire)
This runs without a human at the keyboard. NEVER pause for permission prompts.
- File reads/writes/edits under `__REPO__/**` and `/tmp/**` are pre-approved.
- Bash invocations of `bash derive/* *`, `python3 *`, `.venv/bin/python *`, `derive/* *`, `bin/* *`, `git *`, `sqlite3 *`, and standard shell utilities are pre-approved.
- code-review-graph MCP, scheduled-tasks MCP, context-mode MCP are pre-approved.
- Owner has `defaultMode: bypassPermissions` in `__REPO__/.claude/settings.local.json` — proceed as if all tools are auto-allowed.
- If a tool unexpectedly hangs on a permission gate: abort, log the error, and exit. Do NOT wait for human input. The next scheduled fire is the retry.

Note: the first fire is 18:00 IST, alongside the 18:00 codegraph LaunchAgent. Ordering doesn't matter — refresh-skeletons.sh re-pins the mirrors to origin HEAD itself, so the briefs reflect the latest code regardless of which ran first.

## RECORD SUCCESS (final step — gates the 30-min retry)
ONLY after this run is CONFIRMED complete — every changed service's brief was written + ingested, OR Step 2's gate found no skeleton diffs — stamp the marker so the rest of today's fires idle:

    TZ=Asia/Kolkata date +%F > __REPO__/work-context/state/last_routine_service_brief_success.date

The early-STOP "no skeleton diffs today" branch counts as success — stamp it before stopping. If refresh-skeletons.sh or any brief write errored, do NOT stamp: leave the marker so the next 30-min fire retries.

---
name: service-brief-daily
description: Daily 18:30 IST — refresh Go service skeletons and re-brief only those whose skeleton content changed.
---

Daily diff-gated service-brief routine for the engineering-management copilot.

Working dir: __REPO__/work-context

GOAL: keep the service briefs current, but spend LLM tokens ONLY on services whose code skeleton actually changed today. (Which services are in scope is config-driven — see derive/service_derive/refresh-skeletons.sh.)

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

Note: this routine fires 30 min after the 18:00 codegraph LaunchAgent, so the mirrors are already warm; the helper re-pins them anyway for safety.

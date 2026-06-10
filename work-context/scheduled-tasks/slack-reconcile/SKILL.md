---
name: slack-reconcile
description: Slack nightly reconcile — fires /slack-reconcile at 02:00 IST. Trailing 7-day window for edits + deletions.
---

Run /slack-reconcile

Nightly Slack reconcile from the personal engineering-management copilot at __REPO__/work-context. The skill body is at `__REPO__/.claude/commands/slack-reconcile.md` (one level above the working dir — NOT inside work-context/) — read it and follow Phases 1-5.

Working dir: __REPO__/work-context

Default window: 7 days. Hard cap 200 tombstones/channel/fire (aborts on overrun, surfaces warning).

After fire completes, record summary to state/slack_routine_status.json via:

  .venv/bin/python derive/slack_ingest_runner.py record-fire --summary-json '<your-summary>'

Hard rules (per prd/slack-ingest.md §12):
- DM hard-skip enforced — skill body covers this
- No in-skill retry — next nightly fire IS the retry
- Tombstone is UPDATE deleted_ts = now() (preserve body), NOT DELETE
- Build thread_summary incrementally after reconcile write

**Permission posture (CRITICAL — unattended fire):**
This routine runs without a human at keyboard. NEVER pause for permission prompts.
- File reads/writes/edits under `__REPO__/**` and `/tmp/**` are pre-approved by owner.
- Bash invocations of `.venv/bin/python *`, `derive/* *`, `bin/* *`, `ingest/* *`, `git *`, `sqlite3 *`, `rtk *`, and standard shell utilities are pre-approved.
- Slack MCP (`mcp____SLACK_MCP__slack_*`), scheduled-tasks MCP, context-mode MCP, code-review-graph MCP are pre-approved.
- Owner has `defaultMode: bypassPermissions` set in `__REPO__/.claude/settings.local.json` — proceed as if all tools are auto-allowed.
- If a tool unexpectedly hangs on a permission gate: abort the fire, record error in summary (`record-fire --summary-json '{"errors":["permission_blocked:<tool>"]}'`), and exit. Do NOT wait for human input. Next scheduled fire is the retry.

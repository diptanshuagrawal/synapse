---
name: slack-ingest
description: Slack steady-state ingest — fires /slack-ingest every 30 min during work hours (12:00–22:30 IST).
---

Run /slack-ingest

Steady-state Slack ingest from the personal engineering-management copilot at __REPO__/work-context. The skill body is at `__REPO__/.claude/commands/slack-ingest.md` (one level above the working dir — NOT inside work-context/) — read it and follow Phases 1-5.

Working dir: __REPO__/work-context

If skill body isn't reachable: abort + report. Do NOT improvise.

After fire completes, record summary to state/slack_routine_status.json via:

  .venv/bin/python derive/slack_ingest_runner.py record-fire --summary-json '<your-summary>'

so the cron-status SLACK block reflects this fire.

Hard rules (per prd/slack-ingest.md §12):
- DM hard-skip enforced — skill body covers this
- No in-skill retry on errors — next routine fire IS the retry
- Cursor advance only after page-set commits successfully
- Build thread_summary incrementally after ingest write
- Pagination cap 10 pages/channel/fire — owner runs /slack-backfill if more needed

**Permission posture (CRITICAL — unattended fire):**
This routine runs without a human at keyboard. NEVER pause for permission prompts.
- File reads/writes/edits under `__REPO__/**` and `/tmp/**` are pre-approved by owner.
- Bash invocations of `.venv/bin/python *`, `derive/* *`, `bin/* *`, `ingest/* *`, `git *`, `sqlite3 *`, `rtk *`, and standard shell utilities are pre-approved.
- Slack MCP (`mcp____SLACK_MCP__slack_*`), scheduled-tasks MCP, context-mode MCP, code-review-graph MCP are pre-approved.
- Owner has `defaultMode: bypassPermissions` set in `__REPO__/.claude/settings.local.json` — proceed as if all tools are auto-allowed.
- If a tool unexpectedly hangs on a permission gate: abort the fire, record error in summary (`record-fire --summary-json '{"errors":["permission_blocked:<tool>"]}'`), and exit. Do NOT wait for human input. Next scheduled fire is the retry.

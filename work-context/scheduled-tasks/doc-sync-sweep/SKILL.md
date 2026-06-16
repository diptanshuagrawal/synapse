---
name: doc-sync-sweep
description: Monthly (1st, 10:00 IST) — runs the doc-drift sweep over the team-doc inventory in SAFE PREVIEW mode (--dry-run --target dm). Posts NOTHING to live docs/channel; DMs the owner what it would raise. Flip off --dry-run to go live.
---

Run the monthly doc-drift sweep. SAFE PREVIEW: this fire must NOT post inline comments
to Confluence or to any channel — it runs dry and reports to the owner's DM only.

Working dir: __REPO__

STEP 0 — Resolve a yaml-capable python:
  PY=$(for p in /opt/homebrew/bin/python3 python3 /usr/local/bin/python3; do "$p" -c 'import yaml' 2>/dev/null && { echo "$p"; break; }; done)

STEP 1 — Run the sweep skill EXACTLY as defined in `.claude/commands/doc-sync-sweep.md`,
with options:  `--dry-run --target dm --run-id <YYYY-MM>`  (current year-month, IST).
- Loads `config/doc_sync_inventory.yaml` → `monitor` list ONLY.
- Per doc: fetch page, gather code truth (graph + migrations + source), run the five
  drift checks + DIRECTION GATE, keep BACKWARD-drift findings only.
- Diagrams: a scheduled run has NO interactive work browser, so ZenUML/image diagrams
  are NOT checkable here — emit the one-line "verify manually" note, never fabricate.
- Build candidates → `$PY derive/doc_sync_state.py filter-new --file …` (dedup gate).
- Because --dry-run: DO NOT call createConfluenceInlineComment and DO NOT post the Slack
  summary to a channel. Instead DM the owner (config `doc_sync.yaml` slack.dm_user_id)
  the rendered sweep summary of what it WOULD post, prefixed "[doc-sync sweep PREVIEW]".

STEP 2 — Output: docs scanned · would-post findings · deduped (already-open) · clean ·
skipped (not-checkable/empty). No state writes in dry-run.

HARD RULES: dry-run preview only — ZERO Confluence writes, ZERO channel posts. Monitor
list only (never needs_confirm/excluded). To go LIVE later: drop `--dry-run`, set
`config/doc_sync.yaml` slack.channel_id, and switch `--target channel`.

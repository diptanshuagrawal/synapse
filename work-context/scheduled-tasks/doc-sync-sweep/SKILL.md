---
name: doc-sync-sweep
description: Monthly, days 1–3 from 10:00 IST (retries every 30 min until it succeeds once that month) — doc-drift sweep over the team-doc inventory in CHANNEL-PREVIEW mode (dry-run → doc-sweep channel). Includes a Chrome diagram pass (attempt-with-fallback) + Relay Approve/Reject cards for newly-discovered docs. Posts ZERO Confluence comments.
---

Run the monthly doc-drift sweep. CHANNEL-PREVIEW: this fire must NOT post inline comments
to Confluence. It runs dry and posts the rendered PREVIEW (+ discovery Approve/Reject cards)
to the doc-sweep channel (config slack.channel_id).

Working dir: __REPO__

## RUN-ONCE GATE (idempotent — this MONTHLY routine retries every 30 min on days 1–3 until it succeeds once this month)
Before doing ANY work, run this and obey it:

    MARK=__REPO__/work-context/state/last_routine_docsync_sweep_success.date
    MONTH=$(TZ=Asia/Kolkata date +%Y-%m)
    if [ -f "$MARK" ] && [ "$(cat "$MARK")" = "$MONTH" ]; then echo "GATE: doc-sync-sweep already succeeded this month ($MONTH) — idle"; else echo "GATE: doc-sync-sweep not done this month — proceed"; fi

If it prints "already succeeded this month" → STOP NOW: discover nothing, sweep nothing, post nothing; end the run. Only proceed to the steps below if it prints "not done this month".

STEP 0 — Resolve a yaml-capable python:
  PY=$(for p in /opt/homebrew/bin/python3 python3 /usr/local/bin/python3; do "$p" -c 'import yaml' 2>/dev/null && { echo "$p"; break; }; done)

STEP 1 — Run the sweep skill EXACTLY as defined in `.claude/commands/doc-sync-sweep.md`,
with options:  `--dry-run --target channel --run-id <YYYY-MM>`  (current year-month, IST).
Target id comes from `work-context/config/doc_sync.yaml` slack.channel_id.
- Phase 0.5 DISCOVERY (runs even in dry-run): CQL-discover team docs across the spaces +
  keywords in inventory `meta` (spaces_scanned / discovery_terms / owned_services),
  apply the 3-part filter (owned service in `meta.owned_services` + author in config/people.yaml
  scope:team + NOT ops/RCA/oncall/perf/setup/tracking), write survivors to
  state/doc_sync_discovered.json, then `$PY work-context/derive/doc_sync_state.py discover-merge
  --inventory work-context/config/doc_sync_inventory.yaml --candidates state/doc_sync_discovered.json --write`
  to append only NEW ids to `needs_confirm`.
  New docs are NOT swept this run (promotion to `monitor` happens via Relay — STEP 1.5).
- Loads `work-context/config/doc_sync_inventory.yaml` → `monitor` list ONLY for the drift checks.
- Per doc: fetch page, gather code truth (graph + migrations + source), run the five
  drift checks + DIRECTION GATE, keep BACKWARD-drift findings only. Text-only here;
  ZenUML/image diagrams → mark `diagram_pending` for Phase 1.5.
- Phase 1.5 CHROME DIAGRAM PASS (attempt EVERY run, degrade gracefully): `list_connected_browsers`.
  If a Confluence-signed-in work browser is connected, read each `diagram_pending` doc's
  ZenUML/image diagram visually (navigate → expand → screenshot/zoom → transcribe top-to-bottom)
  and diff steps + table/field names against code; append high-confidence BACKWARD sequence-drift
  to candidates. **If NO browser is connected (typical headless cron fire), SKIP the visual read** —
  emit "diagram source not machine-readable — Chrome pass skipped (no work browser this run);
  verify manually" per pending doc and record `diagram_pass: skipped_no_browser`. NEVER fabricate steps.
- Build candidates → `$PY work-context/derive/doc_sync_state.py filter-new --file …` (dedup gate).
- Because --dry-run: DO NOT call createConfluenceInlineComment. STILL post the rendered preview
  (prefixed "[doc-sync sweep PREVIEW]") to the doc-sweep channel via slack_send_message,
  including the `diagram_pass` status line.

STEP 1.5 — DISCOVERY APPROVE/REJECT (Phase 4.5): if discovery found NEW docs this run, post Relay cards:
    $PY bin/relay_bot.py --post-docsync <YYYY-MM>
  One Approve/Reject card per newly-discovered doc → doc-sweep channel. Approve promotes
  needs_confirm→monitor; Reject moves it to excluded (Relay LaunchAgent applies on click,
  owner-gated). If the bot errors (not in channel / token), report stderr — do NOT silently fail.
  Skip if discovery found nothing new.

STEP 2 — Output: docs scanned · would-post findings · deduped (already-open) · newly-discovered ·
clean · diagram/empty · diagram_pass status. No drift state writes in dry-run (discovery append IS written).

HARD RULES: dry-run preview only — ZERO Confluence writes. Preview + discovery cards post to the
doc-sweep channel (config slack.channel_id), never DM. Drift checks on monitor list only (never
needs_confirm/excluded); discovery appends new docs to needs_confirm only — promotion to monitor
happens ONLY via the Relay Approve button. Chrome pass is attempt-with-fallback; never fabricate
diagram steps. To go FULLY LIVE later: drop `--dry-run` (starts posting real Confluence inline
comments) — but FIRST fix the title-based dedup gate so reworded re-finds don't double-post.

## RECORD SUCCESS (final step — gates the days-1–3 retry)
ONLY after the PREVIEW post is CONFIRMED delivered to the doc-sweep channel (the would-post summary + discovery cards, or a clean "no drift" line) — stamp the marker with the year-month so the rest of this month's fires idle:

    TZ=Asia/Kolkata date +%Y-%m > __REPO__/work-context/state/last_routine_docsync_sweep_success.date

A clean "no drift / nothing to preview" run counts as success — stamp it. If discovery/sweep errored or the channel post could not be delivered, do NOT stamp: leave the marker so the next 30-min fire (today or day 2/3) retries.

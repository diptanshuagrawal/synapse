---
name: doc-sync-sweep
description: Monthly, days 1–3 from 10:00 IST (retries every 30 min until it succeeds once that month) — doc-drift sweep over the team-doc inventory in CHANNEL-PREVIEW mode (dry-run → doc-sweep channel). Includes a Chrome diagram pass (attempt-with-fallback) + Relay Approve/Reject cards for newly-discovered docs. Posts ZERO Confluence comments.
---

Run the monthly doc-drift sweep. CHANNEL-PREVIEW: this fire must NOT post inline comments
to Confluence. It runs dry and posts the rendered PREVIEW (+ discovery Approve/Reject cards)
to the doc-sweep channel (config slack.channel_id).

Working dir: __REPO__

## RUN-ONCE GATE (idempotent — this MONTHLY routine retries every 30 min on days 1–3)
TWO markers gate this routine so the Chrome diagram pass is REQUIRED-by-default without spamming the channel:
- `last_routine_docsync_sweep_posted.date`  — the text-sweep PREVIEW + cards were delivered this month (posted once).
- `last_routine_docsync_sweep_success.date` — FULLY done: the diagram pass also actually RAN (browser present), or there were zero `diagram_pending` docs.

Before doing ANY work, run this and obey the printed MODE:

    SUCCESS=__REPO__/work-context/state/last_routine_docsync_sweep_success.date
    POSTED=__REPO__/work-context/state/last_routine_docsync_sweep_posted.date
    MONTH=$(TZ=Asia/Kolkata date +%Y-%m)
    if [ -f "$SUCCESS" ] && [ "$(cat "$SUCCESS")" = "$MONTH" ]; then echo "GATE: fully done this month ($MONTH) — idle"; \
    elif [ -f "$POSTED" ] && [ "$(cat "$POSTED")" = "$MONTH" ]; then echo "GATE: text sweep already posted ($MONTH) — DIAGRAM-ONLY mode (only the Chrome pass is outstanding)"; \
    else echo "GATE: not posted this month — FULL run"; fi

- **"fully done this month"** → STOP NOW: discover nothing, sweep nothing, post nothing; end the run.
- **"DIAGRAM-ONLY mode"** → the text sweep already posted this month; the ONLY outstanding work is the mandatory diagram pass. Do NOT re-run discovery or the text sweep and do NOT re-post the preview. Go straight to Phase 1.5 (STEP 1, Chrome pass) on the docs recorded in `state/doc_sync_diagram_pending_<MONTH>.json`:
    - **Browser connected** → read the diagrams, diff vs code, post a "[doc-sync sweep — diagram pass]" follow-up to the doc-sweep channel + Relay finding cards for any new sequence drift, then stamp SUCCESS (see RECORD SUCCESS).
    - **No browser** → IDLE this fire (post nothing), so a later fire with a browser completes it. STOP.
- **"FULL run"** → proceed to the steps below (discovery + text sweep + attempt diagrams + post preview + cards).

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
- Phase 1.5 CHROME DIAGRAM PASS (REQUIRED every run — this is the DEFAULT, not optional):
  `list_connected_browsers`. ALWAYS attempt it. Persist the `diagram_pending` page list to
  `state/doc_sync_diagram_pending_<MONTH>.json` (so a later DIAGRAM-ONLY fire can complete it).
  If a Confluence-signed-in work browser is connected, read each `diagram_pending` doc's
  ZenUML/image diagram visually (navigate → expand → screenshot/zoom → transcribe top-to-bottom)
  and diff steps + table/field names against code; append high-confidence BACKWARD sequence-drift
  to candidates; record `diagram_pass: done`. **If NO browser is connected, the diagram pass is
  INCOMPLETE — not a clean skip.** Emit the loud "⚠️ DIAGRAMS NOT READ this run — Chrome pass needs
  a Confluence-signed-in browser; will retry until one is connected" note per pending doc, record
  `diagram_pass: pending_no_browser`, and DO NOT stamp the SUCCESS marker (only POSTED) — the run
  retries in DIAGRAM-ONLY mode on later fires until a browser is available. NEVER fabricate diagram
  steps and NEVER pre-judge a diagram as drift-incapable to skip it.
- Build candidates → `$PY work-context/derive/doc_sync_state.py filter-new --file …` (dedup gate).
- Because --dry-run: DO NOT call createConfluenceInlineComment. STILL post the rendered preview
  (prefixed "[doc-sync sweep PREVIEW]") to the doc-sweep channel via slack_send_message,
  including the `diagram_pass` status line.

STEP 1.5 — RELAY APPROVE/REJECT CARDS (Phase 4.5): post buttoned cards to the doc-sweep channel
  (Relay bot must be a channel member). These ARE the path to Confluence — the sweep never
  auto-comments; a human Approve click does. So post cards even under --dry-run.
  (a) Drift findings: write this run's findings to state/doc_sync_findings_<YYYY-MM>.json (one per
      candidate incl. finding_key + already_open flag for any filter-new SKIPPED dupe), then
      `$PY bin/relay_bot.py --post-findings <YYYY-MM>`. Approve → bin/doc_sync_apply.py posts the
      inline Confluence comment (footer fallback) + records open; Reject → records rejected.
      Already-open dupes are EXCLUDED from buttons.
  (b) Discovered docs: `$PY bin/relay_bot.py --post-docsync <YYYY-MM>`. Approve promotes
      needs_confirm→monitor; Reject → excluded (derive/doc_sync_state.py move).
  Owner-gated, RELAY_APPLY_MODE=live. If the bot errors (not in channel / token), report stderr —
  do NOT silently fail. Skip a set if it's empty.

STEP 2 — Output: docs scanned · would-post findings · deduped (already-open) · newly-discovered ·
clean · diagram/empty · diagram_pass status. No drift state writes in dry-run (discovery append IS written).

HARD RULES: dry-run preview only — ZERO Confluence writes. Preview + discovery cards post to the
doc-sweep channel (config slack.channel_id), never DM. Drift checks on monitor list only (never
needs_confirm/excluded); discovery appends new docs to needs_confirm only — promotion to monitor
happens ONLY via the Relay Approve button. Chrome pass is attempt-with-fallback; never fabricate
diagram steps. Comments reach Confluence ONLY via a human Approve on a Relay finding card
(STEP 1.5) — the sweep never auto-posts, so there's no need to drop `--dry-run`. The dedup gate
(filter-new) catches reworded re-finds (exact key + fuzzy same-page identifier match; soft
matches flagged on the card), so approving won't double-post.

## RECORD SUCCESS (final step — gates the days-1–3 retry; TWO markers)
1. **POSTED** — the moment the PREVIEW post is CONFIRMED delivered to the doc-sweep channel (the would-post summary + discovery cards, or a clean "no drift" line), stamp:

    TZ=Asia/Kolkata date +%Y-%m > __REPO__/work-context/state/last_routine_docsync_sweep_posted.date

   This prevents the text sweep from re-posting on later fires. Stamp it for a clean "no drift / nothing to preview" run too.

2. **SUCCESS** — stamp ONLY when the mandatory diagram pass is genuinely satisfied this month, i.e. `diagram_pass: done` (a browser was connected and the diagrams were read) OR there were zero `diagram_pending` docs:

    TZ=Asia/Kolkata date +%Y-%m > __REPO__/work-context/state/last_routine_docsync_sweep_success.date

   **Do NOT stamp SUCCESS if `diagram_pass: pending_no_browser`** — leave it unstamped so subsequent fires retry in DIAGRAM-ONLY mode until a Confluence browser is connected and the diagrams are actually read. (In DIAGRAM-ONLY mode, stamp SUCCESS once the diagram follow-up is delivered.)

If discovery/sweep errored or the channel post could not be delivered, stamp NEITHER marker so the next 30-min fire retries the whole thing.

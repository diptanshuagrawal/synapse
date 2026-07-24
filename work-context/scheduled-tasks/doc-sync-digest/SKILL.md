---
name: doc-sync-digest
description: Mon/Wed/Fri from 13:00 IST (retries every 30 min until it succeeds once) — doc-sync pending-review digest; posts the per-dev list of open review threads to the team channel (`__DEV_UPDATES_CHANNEL__`). Silent when nothing is open. Read-only on Confluence.
---

Run the doc-sync pending-review digest. READ-ONLY on Confluence — never post, edit, or
resolve a comment. When there are open review threads, posts the digest to the team channel
`__DEV_UPDATES_CHANNEL__`. When there is NO drift, posts nothing.

Working dir: __REPO__

## RUN-ONCE GATE (idempotent — this routine retries every 30 min until it succeeds once today)
Before doing ANY work, run this and obey it:

    MARK=__REPO__/work-context/state/last_routine_docsync_digest_success.date
    LOCK=__REPO__/work-context/state/docsync_digest_inprogress.lock
    TODAY=$(TZ=Asia/Kolkata date +%F)
    NOW=$(date +%s)
    LOCKTS=$(cat "$LOCK" 2>/dev/null); LOCKTS=${LOCKTS:-0}
    if [ -f "$MARK" ] && [ "$(cat "$MARK")" = "$TODAY" ]; then echo "GATE: doc-sync-digest already succeeded today ($TODAY) — idle"
    elif [ -f "$LOCK" ] && [ $((NOW - LOCKTS)) -lt 2700 ]; then echo "GATE: another doc-sync-digest run is in progress (lock age <45min) — idle"
    else echo "$NOW" > "$LOCK"; echo "GATE: doc-sync-digest not done today — proceed"; fi

If it prints "already succeeded today" OR "another doc-sync-digest run is in progress" → STOP NOW: do not list, poll, or DM anything; end the run. Only proceed to the steps below if it prints "not done today — proceed".

(The lock closes the same >30-min-run race validated on daily-standup 2026-07-13 — the marker is checked at start but stamped at end, so a run longer than 30 min overlaps the next cron fire and the deliverable posts twice. The lock is stamped at start; a crashed run's stale lock self-expires after 45 min so retries still happen.)

STEP 0 — Resolve a yaml-capable python:
  PY=$(for p in /opt/homebrew/bin/python3 python3 /usr/local/bin/python3; do "$p" -c 'import yaml' 2>/dev/null && { echo "$p"; break; }; done)

STEP 1 — Determine open doc-drift (per `.claude/commands/doc-sync-digest.md`, `--target channel`):
- `$PY work-context/derive/doc_sync_state.py list --open` → tracked comments still open.
- For each page with open tracked comments, poll `getConfluencePageInlineComments`
  (resolutionStatus open + resolved) and update only OUR tracked comment_ids via
  `$PY work-context/derive/doc_sync_state.py set-status --file …`. NEVER count untracked page comments.
- `$PY work-context/derive/doc_sync_state.py render-digest --date "<Ddd DD Mon YYYY>" --cc <cc id>`
  to get the CURRENT pending-review set.

STEP 2 — Deliver ONLY IF there is drift:
- If render-digest shows ZERO open threads (no doc drift): **do NOT post anything to Slack** —
  skip straight to RECORD SUCCESS. Silence = clean; we no longer post the celebratory line.
- If there is ≥1 open thread: post the rendered digest to `__DEV_UPDATES_CHANNEL__` via
  `slack_send_message`. This needs the Slack MCP connector, which is often ABSENT in headless
  cron — probe first:
  - SLACK-PATH PROBE: try to load the Slack send tool (ToolSearch query `slack_send_message`).
    If NO `slack_*` send tool is registered (headless, no connector):
        MISS=__REPO__/work-context/state/docsync_digest_slack_missing.date
    · If `$MISS` does NOT exist OR `$(cat "$MISS" 2>/dev/null)` != `$TODAY`: fire ONE loud
      signal — a PushNotification reading "doc-sync-digest: open doc-review threads but Slack
      connector not attached in cron. Run /doc-sync-digest --target channel from an interactive
      session to deliver." — then stamp the marker: `echo "$TODAY" > "$MISS"`.
    · `rm -f __REPO__/work-context/state/docsync_digest_inprogress.lock` and STOP. Do NOT stamp
      the success marker — an interactive run later today must still post (the success gate is
      date-shared; stamping here would suppress the real delivery). The rest of today's headless
      fires re-detect, find `$(cat "$MISS")` == `$TODAY`, and idle silently (one notification/day).
  - If the Slack send tool IS registered: `rm -f __REPO__/work-context/state/docsync_digest_slack_missing.date`
    (clear any stale miss), post the digest, then RECORD SUCCESS.

STEP 3 — Output: open threads remaining · newly-resolved-since-last-run · per-dev counts.

HARD RULES: read-only on Confluence; track only our own comment_ids; NEVER post when there is
no drift; when posting, post to the team channel `__DEV_UPDATES_CHANNEL__` — never hardcode the id.

## RECORD SUCCESS (final step — gates the 30-min retry)
Stamp the marker AND release the in-progress lock when EITHER (a) there was no open drift so
nothing was posted, OR (b) the pending-review digest was CONFIRMED posted to the team channel:

    TZ=Asia/Kolkata date +%F > __REPO__/work-context/state/last_routine_docsync_digest_success.date
    rm -f __REPO__/work-context/state/docsync_digest_inprogress.lock

Zero-drift (no post) counts as success — stamp it. If Confluence polling errored, or there was
drift but the message could not be posted, do NOT stamp — but DO `rm -f` the lock so the next
30-min fire retries (a crashed session that never reaches this step is covered by the lock's
45-min self-expiry). The headless "Slack connector missing" path in STEP 2 intentionally does
NOT stamp, so an interactive run can still deliver today's digest.

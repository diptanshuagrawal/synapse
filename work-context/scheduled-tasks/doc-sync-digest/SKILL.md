---
name: doc-sync-digest
description: Mon/Wed/Fri from 13:00 IST (retries every 30 min until it succeeds once) — doc-sync pending-review digest; posts the per-dev list of open review threads to the team channel (`__DEV_UPDATES_CHANNEL__`). Read-only on Confluence.
---

Run the doc-sync pending-review digest. READ-ONLY on Confluence — never post, edit, or
resolve a comment. Posts the digest to the team channel `__DEV_UPDATES_CHANNEL__`.

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

STEP 1 — Run the digest skill EXACTLY as defined in `.claude/commands/doc-sync-digest.md`,
with option:  `--target channel`.
- `$PY work-context/derive/doc_sync_state.py list --open` → tracked comments still open.
- For each page with open tracked comments, poll `getConfluencePageInlineComments`
  (resolutionStatus open + resolved) and update only OUR tracked comment_ids via
  `$PY work-context/derive/doc_sync_state.py set-status --file …`. NEVER count untracked page comments.
- `$PY work-context/derive/doc_sync_state.py render-digest --date "<Ddd DD Mon YYYY>" --cc <cc id>` and
  post the result to the team channel `__DEV_UPDATES_CHANNEL__`
  via slack_send_message. If nothing is open, send the clean "no pending reviews" line.

STEP 2 — Output: open threads remaining · newly-resolved-since-last-run · per-dev counts.

HARD RULES: read-only on Confluence; track only our own comment_ids; post to the team
channel `__DEV_UPDATES_CHANNEL__` — never hardcode the id.

## RECORD SUCCESS (final step — gates the 30-min retry)
ONLY after the digest message is CONFIRMED posted to the team channel (the per-dev list, or the clean "no pending reviews" line) — stamp the marker AND release the in-progress lock so the rest of today's fires idle:

    TZ=Asia/Kolkata date +%F > __REPO__/work-context/state/last_routine_docsync_digest_success.date
    rm -f __REPO__/work-context/state/docsync_digest_inprogress.lock

A clean "no pending reviews" post counts as success — stamp it. If the poll errored or the message could not be posted, do NOT stamp — but DO `rm -f` the lock so the next 30-min fire retries immediately (a crashed session that never reaches this step is covered by the lock's 45-min self-expiry).

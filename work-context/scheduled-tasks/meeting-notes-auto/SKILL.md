---
name: meeting-notes-auto
description: Weekdays every 5 min 09:00–21:55 — renders /meeting-notes for any newly recorded meeting that has no note yet + generates requested MoMs; idles cheaply otherwise.
---

Auto-render notes for newly recorded meetings (meeting-intelligence pipeline).

RENDER-ONLY: this routine NEVER transcribes. Transcription (whisper) is owned
solely by the always-on launchd watcher `com.example.transcripts-watch`
(WatchPaths on the inbox + 5-min backstop) and the on-demand Steno "Transcribe"
button. A routine session is short-lived; a long meeting's whisper run outlives
it, so a sweep launched here gets SIGTERM'd on session end and can NEVER finish
(CBS-audit 92-min / Jayanth 1-1 72-min, 2026-07-20). So this routine only reads
already-transcribed+ingested meetings (`meeting_recorded` events the watcher
created) and renders their notes.

## STEP 0 — SINGLE-FLIGHT LOCK + CHEAP GATE (run this FIRST; obey it)

At the 5-min cadence a slow render can still be running when the next fire
starts. Take a single-flight lock FIRST so two runs never render the same
meeting at once (double-written note / double signals / double MoM). `mkdir` is
atomic; a stale lock (>15 min = a prior run died) is taken over.

    LOCK=__REPO__/work-context/transcripts/.notes_render.lock
    if ! mkdir "$LOCK" 2>/dev/null; then
      age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ))
      if [ "$age" -lt 900 ]; then echo "LOCKED: another notes render already in progress — idle"; else rm -rf "$LOCK" && mkdir "$LOCK" && echo "took over stale lock (>15m)"; fi
    fi

If it printed "LOCKED … — idle" → STOP NOW: do NO other work and do NOT touch
the lock (it belongs to the live run). Otherwise you hold the lock — continue.

Cheap gate (only when you hold the lock):

    cd __REPO__/work-context
    PENDING=$(sqlite3 index/events.db "SELECT subject, title FROM events WHERE source='meeting' AND event_type='meeting_recorded' ORDER BY ts DESC LIMIT 10" | while IFS='|' read -r sub title; do
      d=$(echo "$sub" | cut -d: -f2); slug=$(echo "$sub" | cut -d: -f3)
      [ -f "__REPO__/management/meetings/$d-$slug.md" ] || echo "$sub"
    done)
    MOMS=$(ls __REPO__/management/meetings/*.mom.request 2>/dev/null)
    if [ -z "$PENDING" ] && [ -z "$MOMS" ]; then rm -rf "$LOCK"; echo "GATE: nothing pending — idle"; else echo "GATE: pending notes:"; echo "$PENDING"; echo "GATE: pending MoMs:"; echo "$MOMS"; fi

If it prints "nothing pending — idle" → STOP NOW (the lock was just released).

## If pending work exists

Run the /meeting-notes skill (the SAME skill at `.claude/commands/meeting-notes.md`) for the
pending subjects and/or MoM requests only. Follow it exactly: template by classified
category, scratchpad merge if present, attached-links resolution (STEP 2.4), attribution
honesty ((unattributed) over guessing), STEP 5 signal persistence via signals.py for EVERY
meeting (not standups only — the Steno To-do view + standup gather both read this store), STEP 5.5
MoM generation for each `.mom.request` marker (formal shareable minutes; delete the marker
after writing), note file to `management/meetings/<date>-<slug>.md`.

Rules:
- RELEASE THE LOCK when finished: after the LAST note/MoM is written — or if you stop
  early for any reason after acquiring it — run
  `rm -rf __REPO__/work-context/transcripts/.notes_render.lock`. (If the run crashes without
  releasing, the 15-min TTL in STEP 0 lets the next fire take over — so overlap degrades to a
  brief skip, never a double render.)
- NO Slack posts, no external writes — note/MoM files + signals state are the only outputs.
- NEVER run `bin/transcripts_process.sh` here (see RENDER-ONLY above) — the launchd watcher owns
  transcription. If a recorded meeting has no note AND no ingested transcript yet, it's simply not
  transcribed yet (watcher deferred: on battery, paused, or still running); leave it — a later
  fire renders it once ingested.
- If a file is stuck untranscribed in the inbox, that's a watcher concern; check
  `/tmp/transcripts-watch.log`. Report it in the run output and move on.
- Working dir: __REPO__/work-context.

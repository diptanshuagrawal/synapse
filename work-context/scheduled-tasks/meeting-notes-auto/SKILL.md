---
name: meeting-notes-auto
description: Weekdays every 5 min 09:00–21:55 — renders /meeting-notes for any newly recorded meeting that has no note yet + generates requested MoMs; idles cheaply otherwise.
---

Auto-render notes for newly recorded meetings (meeting-intelligence pipeline).

## STEP 0 — CHEAP GATE (run this FIRST; obey it)

    cd __REPO__/work-context
    bash __REPO__/bin/transcripts_process.sh > /tmp/mn_sweep.log 2>&1
    PENDING=$(sqlite3 index/events.db "SELECT subject, title FROM events WHERE source='meeting' AND event_type='meeting_recorded' ORDER BY ts DESC LIMIT 10" | while IFS='|' read -r sub title; do
      d=$(echo "$sub" | cut -d: -f2); slug=$(echo "$sub" | cut -d: -f3)
      [ -f "__REPO__/management/meetings/$d-$slug.md" ] || echo "$sub"
    done)
    MOMS=$(ls __REPO__/management/meetings/*.mom.request 2>/dev/null)
    if [ -z "$PENDING" ] && [ -z "$MOMS" ]; then echo "GATE: nothing pending — idle"; else echo "GATE: pending notes:"; echo "$PENDING"; echo "GATE: pending MoMs:"; echo "$MOMS"; fi

If it prints "idle" → STOP NOW, end the run with no further work.

## If pending work exists

Run the /meeting-notes skill (the SAME skill at `.claude/commands/meeting-notes.md`) for the
pending subjects and/or MoM requests only. Follow it exactly: template by classified
category, scratchpad merge if present, attached-links resolution (STEP 2.4), attribution
honesty ((unattributed) over guessing), STEP 5 signal persistence via signals.py for EVERY
meeting (not standups only — the Steno To-do view + standup gather both read this store), STEP 5.5
MoM generation for each `.mom.request` marker (formal shareable minutes; delete the marker
after writing), note file to `management/meetings/<date>-<slug>.md`.

Rules:
- NO Slack posts, no external writes — note/MoM files + signals state are the only outputs.
- If transcription failed for a file (still in inbox per the sweep log), report the error in
  the run output and move on.
- Working dir: __REPO__/work-context.

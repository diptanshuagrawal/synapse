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
atomic; a stale lock (past the TTL = a prior run died) is taken over. TTL is
8 min — comfortably longer than any legit render (so a live run is never
preempted) yet short enough that a dead lock recovers within ~2 fires instead
of ~3. Every fire appends to `/tmp/meeting-notes-auto.log`; a REPEATED
"TOOK OVER stale lock" line there is the smoking gun for a render that keeps
dying mid-run.

    LOCK=__REPO__/work-context/transcripts/.notes_render.lock
    LOG=/tmp/meeting-notes-auto.log
    TTL=480   # stale-lock takeover after 8 min (was 900/15 min)
    if mkdir "$LOCK" 2>/dev/null; then
      echo "$(date '+%F %T') acquired lock (pid $$)" >> "$LOG"
    else
      age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ))
      if [ "$age" -lt "$TTL" ]; then
        echo "$(date '+%F %T') LOCKED age=${age}s<${TTL}s — idle" >> "$LOG"
        echo "LOCKED: another notes render already in progress — idle"
      else
        rm -rf "$LOCK" && mkdir "$LOCK"
        echo "$(date '+%F %T') TOOK OVER stale lock age=${age}s>=${TTL}s (prior render died without releasing)" >> "$LOG"
        echo "took over stale lock"
      fi
    fi

If it printed "LOCKED … — idle" → STOP NOW: do NO other work and do NOT touch
the lock (it belongs to the live run). Otherwise you hold the lock — continue.

Cheap gate (only when you hold the lock):

    LOCK=__REPO__/work-context/transcripts/.notes_render.lock
    LOG=/tmp/meeting-notes-auto.log
    cd __REPO__/work-context
    PENDING=$(sqlite3 index/events.db "SELECT subject, title FROM events WHERE source='meeting' AND event_type='meeting_recorded' ORDER BY ts DESC LIMIT 10" | while IFS='|' read -r sub title; do
      d=$(echo "$sub" | cut -d: -f2); slug=$(echo "$sub" | cut -d: -f3)
      [ -f "__REPO__/management/meetings/$d-$slug.md" ] || echo "$sub"
    done)
    MOMS=$(ls __REPO__/management/meetings/*.mom.request 2>/dev/null)
    REGENS=$(ls __REPO__/management/meetings/*.regen.request 2>/dev/null)
    if [ -z "$PENDING" ] && [ -z "$MOMS" ] && [ -z "$REGENS" ]; then
      rm -rf "$LOCK"; echo "$(date '+%F %T') gate: nothing pending — released, idle" >> "$LOG"; echo "GATE: nothing pending — idle"
    else
      echo "$(date '+%F %T') gate: notes=[$(echo $PENDING | tr '\n' ' ')] moms=[$(ls __REPO__/management/meetings/*.mom.request 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')] regens=[$(ls __REPO__/management/meetings/*.regen.request 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')]" >> "$LOG"
      echo "GATE: pending notes:"; echo "$PENDING"; echo "GATE: pending MoMs:"; echo "$MOMS"; echo "GATE: pending regens:"; echo "$REGENS"
    fi

If it prints "nothing pending — idle" → STOP NOW (the lock was just released).

## If pending work exists

Run the /meeting-notes skill (the SAME skill at `.claude/commands/meeting-notes.md`) for the
pending subjects, MoM requests, AND regen requests only. Follow it exactly: template by classified
category, scratchpad merge if present, attached-links resolution (STEP 2.4), attribution
honesty ((unattributed) over guessing), STEP 5 signal persistence via signals.py for EVERY
meeting (not standups only — the Steno To-do view + standup gather both read this store), STEP 5.5
MoM generation for each `.mom.request` marker (formal shareable minutes; delete the marker
after writing), note file to `management/meetings/<date>-<slug>.md`.

REGEN requests: each `<date>-<slug>.regen.request` marker means the owner hit "regenerate" in
the Steno UI — re-render THAT specific note (the previous version is at `<mid>.md.prev`),
honoring its `.cat`/scratchpad/links sidecars, then DELETE the marker. Age-independent: the
marker names the exact mid, so an OLD meeting regenerates even though it's absent from the
recent-recordings scan above (the bug that stranded regens older than the last ~10 recordings).

FAST-PATH — MoM / regen-only fires SKIP the inbox sweep. If there are NO pending NOTES
(PENDING empty) and the only work is `.mom.request` / `.regen.request` markers, do NOT run the
/meeting-notes STEP 0 inbox sweep (whisper/ingest) — the note + archived transcript already
exist on disk. Go straight to: read `management/meetings/<mid>.md` +
`transcripts/archive/<month>/<mid>.txt`, synthesize the MoM (STEP 5.5) or regenerate the note,
write it, delete the marker. WHY: a MoM fire that runs the full sweep + synthesis has been
overrunning the run window and getting SIGKILLed mid-render (note fires finish in ~3 min; MoM
fires hung ~12 min and were killed → MoM never landed, "queued forever"). Skipping the sweep
keeps MoM/regen fires as short as note fires.

Rules:
- LOG each phase to `/tmp/meeting-notes-auto.log` (same log STEP 0 writes) so a repeatedly-dying
  render is diagnosable AND you can see WHERE it dies. The moment you decide to render, append
  `echo "$(date '+%F %T') render START notes=<n> moms=<m>" >> /tmp/meeting-notes-auto.log`;
  right before synthesizing each MoM/note append `synth <basename>`; after each file is written
  append `wrote <basename>`; after the LAST one append `render DONE`; on ANY early stop append
  `render ABORTED: <reason>`. (START-but-no-`synth` = died in the sweep; `synth`-but-no-`wrote`
  = died in synthesis.)
- RELEASE THE LOCK when finished: after the LAST note/MoM is written — or if you stop
  early for any reason after acquiring it — run
  `rm -rf __REPO__/work-context/transcripts/.notes_render.lock` and append
  `echo "$(date '+%F %T') released lock" >> /tmp/meeting-notes-auto.log`. (If the run crashes
  without releasing, the 8-min TTL in STEP 0 lets the next fire take over — so overlap degrades
  to a brief skip, never a double render.)
- NO Slack posts, no external writes — note/MoM files + signals state are the only outputs.
- NEVER run `bin/transcripts_process.sh` here (see RENDER-ONLY above) — the launchd watcher owns
  transcription. If a recorded meeting has no note AND no ingested transcript yet, it's simply not
  transcribed yet (watcher deferred: on battery, paused, or still running); leave it — a later
  fire renders it once ingested.
- If a file is stuck untranscribed in the inbox, that's a watcher concern; check
  `/tmp/transcripts-watch.log`. Report it in the run output and move on.
- Working dir: __REPO__/work-context.

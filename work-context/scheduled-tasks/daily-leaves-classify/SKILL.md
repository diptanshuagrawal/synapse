---
name: daily-leaves-classify
description: Daily from 13:00 IST (retries every 30 min until it succeeds once) — waits for today's Slack ingest to land, then runs the full /leaves pipeline: dump, chat-classify, apply, render.
---

Run the team-leave tracking pipeline end-to-end, including the LLM chat-classify step. This replaces the old leaves LaunchAgent (which only did the dump+render half and could not classify).

Working directory: __REPO__/work-context
Always use the venv python: __REPO__/work-context/.venv/bin/python

The canonical procedure lives in the `/leaves` skill — invoke it via the Skill tool and follow its phases. If for any reason the skill is unavailable, execute these steps manually:

## RUN-ONCE + SLACK-FRESHNESS GATE (idempotent — this routine retries every 30 min until it succeeds once today)
The dump reads Slack messages out of events.db, so classifying before today's first
Slack ingest success silently misses same-morning leave announcements (a "nothing to
classify" run then stamps success and the day is lost). Idle until BOTH markers allow
a run. Before doing ANY work, run this and obey it:

    MARK=__REPO__/work-context/state/last_routine_leaves_success.date
    SLACK=__REPO__/work-context/state/last_slack_success.date
    LOCK=__REPO__/work-context/state/leaves_inprogress.lock
    TODAY=$(TZ=Asia/Kolkata date +%F)
    NOW=$(date +%s)
    LOCKTS=$(cat "$LOCK" 2>/dev/null); LOCKTS=${LOCKTS:-0}
    if [ -f "$MARK" ] && [ "$(cat "$MARK")" = "$TODAY" ]; then echo "GATE: leaves already succeeded today ($TODAY) — idle"
    elif [ ! -f "$SLACK" ] || [ "$(cat "$SLACK")" != "$TODAY" ]; then echo "GATE: slack ingest has not succeeded today ($TODAY) — idle, retry next fire"
    elif [ -f "$LOCK" ] && [ $((NOW - LOCKTS)) -lt 2700 ]; then echo "GATE: another leaves run is in progress (lock age <45min) — idle"
    else echo "$NOW" > "$LOCK"; echo "GATE: leaves not done today, slack fresh — proceed"; fi

If it prints anything other than "proceed" → STOP NOW: do not dump, classify, apply, or
render anything, and do NOT stamp the success marker or touch the lock (the slack-not-fresh
and lock-held idles must leave both untouched so a later fire retries after ingest lands /
the running fire finishes); end the run.

(The lock closes the same >30-min-run race validated on daily-standup 2026-07-13 — the marker
is checked at start but stamped at end, so a run longer than 30 min overlaps the next cron
fire and the deliverable posts twice. The lock is stamped at start; a crashed run's stale
lock self-expires after 45 min so retries still happen.)

PHASE 1 — Refresh pending (idempotent):
  cd __REPO__/work-context && .venv/bin/python derive/leaves_dump.py
  Output ends with "[summary] N events awaiting /leaves chat classify" (or "nothing to classify").
  If nothing is pending, STOP — Phase 1 already re-rendered the markdown.

PHASE 2 — Classify:
  Read the rules first: state/pending_leaves.rules.md
  Then read all pending events: state/pending_leaves.json
  Emit one verdict per event into state/verdicts.leaves.json (shape {"verdicts": [...]}).
  Verdict schema per event: {"event_id": "<echo unchanged>", "is_leave": true|false, "confidence": 0.0-1.0, "leaves": [{"actor": "<canonical handle from team_canonical>", "date_start": "YYYY-MM-DD"|null, "date_end": "YYYY-MM-DD"|null, "reason": "wfh|vacation|sick|holiday|ooo|travel|other"}]}.

  CRITICAL classification heuristic (the dominant failure mode):
  - The vast majority of regex matches are FALSE POSITIVES. The pattern catches "(OOO ...)", "(WFH)", "(OOO, today)", "Name(OOO)" tags embedded in the Slack DISPLAY NAME of a *mentioned* person (e.g. "<@U...|Mahiman (OOO 15th-19th June)>"). That is NOT the message author announcing leave — it is just someone they @-mentioned whose display name carries a leave tag. Mark these is_leave=false, leaves=[].
  - Only mark is_leave=true when a TEAM canonical member (see team_canonical in the pending JSON) announces THEIR OWN unavailability — e.g. "taking sick leave today", "on leave from 22 June to 3 July", "I am travelling and AFK", "wfh today", "day off".
  - The owner is excluded — reports only. "<owner> is ooo" / "<owner> is on leave" → is_leave=false.
  - Mentions of non-team people as the leave subject → is_leave=false (or drop those entries).
  - Resolve relative dates ("tomorrow", "till Friday", "next Monday") against the event's mentioned_at timestamp.
  - confidence < 0.7 → row is rejected by apply and stays pending; don't fabricate certainty. Ambiguous/no-date → date_start=null, date_end=null, reason="other", confidence ≤ 0.7.

PHASE 3 — Apply + render:
  cd __REPO__/work-context && \
    .venv/bin/python derive/apply_leaves.py && \
    .venv/bin/python derive/render_leaves.py

Success criteria: apply reports "[validate] N accepted, 0 rejected" (or only low-confidence rejects), render writes derived/team-leaves.md and prints active/upcoming/recent/ambiguous counts. Report a one-line summary: how many events classified, true vs false, and any notable upcoming leaves.

## RECORD SUCCESS (final step — gates the 30-min retry)
ONLY after the pipeline is CONFIRMED complete — apply + render finished, OR Phase 1 reported nothing pending — stamp the marker AND release the in-progress lock so the rest of today's fires idle:

    TZ=Asia/Kolkata date +%F > __REPO__/work-context/state/last_routine_leaves_success.date
    rm -f __REPO__/work-context/state/leaves_inprogress.lock

The early-STOP "nothing to classify" branch in Phase 1 counts as success — stamp it before stopping. If any phase errored, do NOT stamp — but DO `rm -f` the lock so the next 30-min fire retries immediately (a crashed session that never reaches this step is covered by the lock's 45-min self-expiry).

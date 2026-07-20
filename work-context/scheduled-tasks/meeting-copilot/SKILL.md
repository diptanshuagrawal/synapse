---
name: meeting-copilot
description: EXPERIMENTAL — daily every 10 min 09:05–20:55; instantly idles unless the owner ARMED copilot on the Steno /copilot page; when armed, latches onto the meeting recording and streams grounded speaking suggestions.
---

EXPERIMENTAL meeting copilot (P6, prd/meeting-intelligence.md).

## STEP 0 — CHEAP GATE (run FIRST; obey)

    CAP=__REPO__/work-context/transcripts/.capture
    if [ ! -f "$CAP/copilot.on" ]; then echo "GATE: copilot not armed — idle"
    elif [ -f "$CAP/copilot.session" ] && [ $(( $(date +%s) - $(stat -f %m "$CAP/copilot.session") )) -lt 90 ]; then echo "GATE: another analyst is live — idle"
    else echo "GATE: armed, no analyst — proceed"; fi

If it prints "idle" → STOP NOW. (This is the normal case; the owner arms
copilot on http://127.0.0.1:8788/copilot before a meeting he wants help in.)

## If armed

Run the /meeting-copilot skill (the SAME skill at
`.claude/commands/meeting-copilot.md`) and follow it exactly: wait up to 20 min
for a recording to start (pre-warm), start `bin/live_transcribe.sh`, preload
meeting context from events.db, then loop — read the live transcript tail every
~20s and append grounded suggestion blocks (receipts mandatory) to
`$CAP/copilot_suggestions.md` until the recording ends. Then append a session summary and finish — LEAVE `$CAP/copilot.on` in place
(the arm is STICKY; only the owner disarms via the /copilot page toggle).

Hard rules: read-only on all sources; writes ONLY to `$CAP` working files;
never Slack/Jira/notes; live transcript = gist, never verbatim-quote it.
Working dir: `__REPO__/work-context`.

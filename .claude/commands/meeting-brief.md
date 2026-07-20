---
description: >
  Pre-meeting briefs from the published Outlook calendar feed + events.db
  work context. Granola preps "what you discussed last time"; this preps that
  PLUS each topic's live Jira/Slack/PR state. Read-only everywhere; output is
  chat-only (no files, no posts).
argument-hint: "[today | day YYYY-MM-DD | next | <title substring>]"
---

# /meeting-brief — know the room before you enter it

Part of the meeting-intelligence pipeline (prd/meeting-intelligence.md, P3).

## STEP 1 — Resolve target meetings

    cd __REPO__/work-context && .venv/bin/python3 derive/meetings/calendar_feed.py <today|day <date>|next>

- Default (no args) = `today`. A title-substring arg → run `today` + `upcoming 3`
  and pick matching meetings.
- Feed facts: title, time, teams-link, uid. NO attendee list (published feeds
  strip it) — attendee context below is inference, label it as such.
- Skip all-day rows and obvious non-meetings (`Lunch`, `Busy`, focus blocks)
  unless the owner explicitly asked for one.

## STEP 2a — Run-wide lookups (ONCE per run, NOT per meeting — they're day-scoped)

Fetch these once and reuse across every meeting in the brief:

1. **Open signals** — `python3 derive/meetings/signals.py list` returns ALL rows;
   match rows to meetings in-context afterwards.
2. **Leaves for the date** — one query covers every meeting that day:
       sqlite3 work-context/index/events.db "SELECT actor, date_start, date_end
         FROM team_leaves WHERE date_start <= '<date>'
         AND COALESCE(date_end, date_start) >= '<date>'"
   (actor = canonical handle → people.yaml name.)
3. **people.yaml** — one Read; resolves every title-name below.

## STEP 2b — Context per meeting (~2-3 lookups each, cap it)

1. **Last time** — prior instances: note files `management/meetings/*<title-slug>*.md`
   + prior transcripts (`SELECT subject,title,ts FROM events WHERE source='meeting'
   AND title LIKE '%<title-fragment>%' ORDER BY ts DESC LIMIT 3`). Pull the
   TL;DR + open action items from the most recent note if present.
2. **Live topic state** — events_fts on 2-3 title keywords (skip generic words),
   recent-first, top ~5 hits: the Jira tickets / Slack threads / PRs the meeting
   is likely ABOUT, with their current status from the board. Issue the
   per-meeting queries for INDEPENDENT meetings as parallel tool blocks.
3. **People (inference)** — names embedded in the title (`A<>B`, "with X",
   possessives) resolved via the STEP-2a people.yaml read; for team members add
   their current in-progress state in one line. Never present inferred
   attendance as fact — "likely: …".
4. **Leave / on-call flags** — cross-ref the STEP-2a leave rows against each
   meeting's inferred attendees ("⚠️ <name> is on leave — expect a thin room /
   reschedule?"). If the current on-call (per the standup gather's oncall
   config) is an inferred attendee of a long meeting, note that too ("on-call,
   may drop for pages").

## STEP 3 — Render (chat only; per meeting ≤10 lines — this is a pre-read, not a report)

    ### <HH:MM> <title>
    - Purpose: <one line — inferred from title + last-time + topic hits>
    - Last time: <TL;DR of prior instance + still-open action items> (omit if first)
    - Open on you: <owner action items / asks tied to this meeting or its people>
    - Topic state: <2-3 bullets: ticket/thread + current status, linkified>
    - Worth raising: <1-2 concrete talking points the data suggests>

- `today` mode: brief every real meeting, one screen total; lead with a 2-line
  day shape ("back-to-back 14:00–19:00, heaviest: X").
- HARD RULES: linkify every ticket (jira base URL); no invented attendees or
  facts; a meeting with zero context gets one honest line ("no prior context —
  walk in fresh"), not padding.

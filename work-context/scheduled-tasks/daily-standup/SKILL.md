---
name: daily-standup
description: Weekdays from 06:00 IST (retries every 30 min until it succeeds once) — runs /standup for the previous working day and posts the team digest to the team-standup channel.
---

Run the daily team standup and post it to Slack.

## RUN-ONCE GATE (idempotent — this routine retries every 30 min until it succeeds once today)
Before doing ANY work, run this and obey it:

    MARK=__REPO__/work-context/state/last_routine_standup_success.date
    TODAY=$(TZ=Asia/Kolkata date +%F)
    if [ -f "$MARK" ] && [ "$(cat "$MARK")" = "$TODAY" ]; then echo "GATE: standup already succeeded today ($TODAY) — idle"; else echo "GATE: standup not done today — proceed"; fi

If it prints "already succeeded today" → STOP NOW: do not gather, render, or post anything; end the run. Only proceed to the steps below if it prints "not done today".

STEP 1 — Determine the target date (previous WORKING day, IST):
- Today is the run day. Compute the previous working day:
  - If today is Monday → target = last Friday (3 days ago).
  - If today is Tue/Wed/Thu/Fri → target = yesterday (1 day ago).
- (Sat/Sun never fire because the cron is Mon–Fri.)
- If the target date lands on a known holiday with ~no activity, fall back to the most recent working day before it and note that in the post.

STEP 1.5 — Pre-standup slack thread sweep (freshness; best-effort):
Slack's own ingest only fires 12:00–23:00, and the 24h thread-drain cooldown can hold a
previous-evening late reply (on a thread whose root scrolled below the cursor) past this
06:00 digest — so on-call/queue items get silently missed (validated 2026-06-23: a direct
@owner reply on an old thread wasn't ingested until ~12:30 next day). Refresh recently-active threads
on team_involved + oncall channels with the cooldown BYPASSED, so they're in events.db
before the gather runs:

    cd __REPO__/work-context && PYTHONPATH=__REPO__/work-context \
      __REPO__/work-context/.venv/bin/python -m ingest.slack_ingest_app --threads-sweep || true

Best-effort: if it errors, log it and proceed to STEP 2 anyway (don't block the digest);
note in the run output that the sweep failed so freshness may lag.

STEP 2 — Run the standup skill for that date, team scope:
- Invoke the /standup skill with: team <target-date-YYYY-MM-DD>
- This is the SAME skill at .claude/commands/standup.md. Follow it exactly: roster = config/people.yaml scope:team (EXCLUDE the manager), credit by assignee/author not transitioner, in-progress/up-next/blockers = current board state, leave + on-call from the gather's `# LEAVES` / `# ONCALL` blocks (explicit On-call line), sprint-ahead leave + on-call-rota collisions/coverage gaps from `# ONCALL FORECAST` / `# RISKS` (surface in §7a Day update), CMRs as ops, describe+enrich+link every ticket.
- Do NOT write md files (changed 2026-06-12, owner decision) — the Slack post is the only deliverable. Render the team digest directly.

STEP 3 — Post the team standup to Slack as FOUR separate root messages, across TWO channels:
- The /standup `team` output is FOUR distinct top-level posts (§7), in this order:
  1. `📅 Day update — <target-date>` (§7a) → channel ID __STANDUP_CHANNEL__ (owner-facing).
  2. `⚠️ Your queue — <target-date>` (§7b — owner's personal action items; if empty, `Nothing pending your action.`) → channel ID __STANDUP_CHANNEL__ (owner-facing).
  3. `👥 Standup updates — <target-date>` (§7c — per-person standup in nested bullets, NO team summary) → channel ID __DEV_UPDATES_CHANNEL__ (team-facing — config dev_updates_channel).
  4. `📋 Team summary — <target-date>` (§7d — team-level synthesis: blocked / ships / unowned / out-on-call, PLAIN names no @-pings) → channel ID __STANDUP_CHANNEL__ (owner-facing).
- Owner-facing (1, 2, 4) → __STANDUP_CHANNEL__. Team-facing (3) → __DEV_UPDATES_CHANNEL__. (When the two ids are the same, all four land in one channel — fine.) Team summary is its OWN message to __STANDUP_CHANNEL__, NOT appended to the team-facing Standup updates.
- Send each as its OWN root message (NOT threaded under one another) via the slack send-message tool with the channel ID above. Post in order 1 → 2 → 3 → 4.
- If any post cannot be delivered (you are not a member, channel archived), DO NOT silently fail — report the error clearly in this run's output so it can be fixed. (Posts go out as the OWNER via the Slack MCP, not a bot — the owner must be a member of both channels.)

- FORMATTING (the send tool renders STANDARD markdown — write `[text](url)` and `**bold**` directly; do NOT draft in Slack mrkdwn `<url|text>` / `*bold*`, that just forces a conversion pass):
  - HYPERLINK EVERY Jira ticket and PR inline — `[KEY-NNNN](<jira-base-url>/browse/KEY-NNNN)`, `[PR #N](github-url)`, `[thread](slack-permalink)`. A bare `KEY-NNNN` in the Slack post is a regression — links are the whole point of the digest.
  - REAL @-MENTIONS in Message 3 (team-facing — every dev must be notified): the per-person header is the dev's Slack handle as a real mention via `<@SLACK_USER_ID>` (id from config/people.yaml). The mention ONLY renders if it is NOT inside a `###` markdown header — a `###` heading escapes it to literal `<@U…>` text. So the person header is a BOLD line, not a heading: `**<@U…> · <domain>**  📟/🌴 …`. Use the same `<@U…>` mention for every dev cross-reference (reviewer of, "<dev>'s ticket") too. Verify by reading one posted message back — a real mention round-trips as `<@U…|Name>`, an escaped one as `&lt;@U…&gt;`.
  - AUDIENCE of Message 3 (Standup updates) = the WHOLE TEAM, a general broadcast — NEVER addressed to the owner. No "your"/"you"/"for your review"/"needs your call". Re-frame any owner-directed ask as a neutral team statement ("needs your lookback call" → "pending a decision on the lookback window"). Owner-directed action items live ONLY in Messages 2 (Your queue) + 4 (Team summary), which post to __STANDUP_CHANNEL__.
  - NESTED bullets per §7c: bold status header as a `- ` parent bullet, items as 4-space-indented `    - ` sub-bullets. Don't flatten back to single-level `•`.
  - The send tool caps a message at **5000 chars per text element**. Messages 1 & 2 fit easily; Message 3 (Standup updates) usually won't. When a single message exceeds 5000 with links intact, split THAT message at PERSON boundaries into threaded replies under it (thread_ts = that message's own ts) — first few people in the root, the rest + Team summary in replies. Never drop links to fit; trim PROSE (shorten descriptions, cut In-progress/Up-next to top 2-3 per person) instead.

Read-only on all data sources (events.db, Jira, Confluence, on-call). The ONLY write is the Slack post — no md files.

Working dir: __REPO__/work-context

## RECORD SUCCESS (final step — gates the 30-min retry)
ONLY after this run's deliverable is CONFIRMED — not merely attempted (the team digest actually landed in Slack) — stamp the marker so the rest of today's fires idle:

    TZ=Asia/Kolkata date +%F > __REPO__/work-context/state/last_routine_standup_success.date

If the run errored or the post could not be delivered, do NOT stamp: leave the marker so the next 30-min fire retries.

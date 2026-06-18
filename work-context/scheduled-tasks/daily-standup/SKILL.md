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

STEP 2 — Run the standup skill for that date, team scope:
- Invoke the /standup skill with: team <target-date-YYYY-MM-DD>
- This is the SAME skill at .claude/commands/standup.md. Follow it exactly: roster = config/people.yaml scope:team (EXCLUDE the manager), credit by assignee/author not transitioner, in-progress/up-next/blockers = current board state, leave + on-call from the gather's `# LEAVES` / `# ONCALL` blocks (explicit On-call line), sprint-ahead leave + on-call-rota collisions/coverage gaps from `# ONCALL FORECAST` / `# RISKS` (surface in §7a Day update), CMRs as ops, describe+enrich+link every ticket.
- Do NOT write md files (changed 2026-06-12, owner decision) — the Slack post is the only deliverable. Render the team digest directly.

STEP 3 — Post the team standup to Slack as THREE separate root messages:
- The /standup `team` output is THREE distinct top-level posts (§7), in this order, all to channel ID __STANDUP_CHANNEL__ (the private team-standup channel):
  1. `📅 Day update — <target-date>` (§7a — decisions, announcements, timelines, ships, prod/ops watch, team status & sprint-ahead risk).
  2. `⚠️ Your queue — <target-date>` (§7b — the owner's personal action items; if empty, `Nothing pending your action.`).
  3. `👥 Dev updates — <target-date>` (§7c — per-person standup in nested bullets + Team summary).
- Send each as its OWN root message (NOT threaded under one another) via the slack send-message tool with that channel ID. Post in order 1 → 2 → 3.
- If any post cannot be delivered (bot not a member, channel archived), DO NOT silently fail — report the error clearly in this run's output so it can be fixed.

- FORMATTING (the send tool renders STANDARD markdown — write `[text](url)` and `**bold**` directly; do NOT draft in Slack mrkdwn `<url|text>` / `*bold*`, that just forces a conversion pass):
  - HYPERLINK EVERY Jira ticket and PR inline — `[KEY-NNNN](<jira-base-url>/browse/KEY-NNNN)`, `[PR #N](github-url)`, `[thread](slack-permalink)`. A bare `KEY-NNNN` in the Slack post is a regression — links are the whole point of the digest.
  - NESTED bullets per §7c: bold status header as a `- ` parent bullet, items as 4-space-indented `    - ` sub-bullets. Don't flatten back to single-level `•`.
  - The send tool caps a message at **5000 chars per text element**. Messages 1 & 2 fit easily; Message 3 (Dev updates) usually won't. When a single message exceeds 5000 with links intact, split THAT message at PERSON boundaries into threaded replies under it (thread_ts = that message's own ts) — first few people in the root, the rest + Team summary in replies. Never drop links to fit; trim PROSE (shorten descriptions, cut In-progress/Up-next to top 2-3 per person) instead.

Read-only on all data sources (events.db, Jira, Confluence, on-call). The ONLY write is the Slack post — no md files.

Working dir: __REPO__/work-context

## RECORD SUCCESS (final step — gates the 30-min retry)
ONLY after this run's deliverable is CONFIRMED — not merely attempted (the team digest actually landed in Slack) — stamp the marker so the rest of today's fires idle:

    TZ=Asia/Kolkata date +%F > __REPO__/work-context/state/last_routine_standup_success.date

If the run errored or the post could not be delivered, do NOT stamp: leave the marker so the next 30-min fire retries.

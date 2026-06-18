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
- This is the SAME skill at .claude/commands/standup.md. Follow it exactly: roster = config/people.yaml scope:team (EXCLUDE the manager), credit by assignee/author not transitioner, in-progress/up-next/blockers = current board state, leave + on-call from the gather's `# LEAVES` / `# ONCALL` blocks (explicit On-call line), CMRs as ops, describe+enrich+link every ticket.
- Do NOT write md files (changed 2026-06-12, owner decision) — the Slack post is the only deliverable. Render the team digest directly.

STEP 3 — Post the team digest to Slack:
- Send the rendered team digest to channel ID __STANDUP_CHANNEL__ (the private team-standup channel).
- Use the slack send-message tool with that channel ID. If the post cannot be delivered (bot not a member, channel archived), DO NOT silently fail — report the error clearly in this run's output so it can be fixed.
- Lead the Slack message with a one-line header: "Team standup — <target-date> (for previous working day)".

- FORMATTING (the send tool renders STANDARD markdown — write `[text](url)` and `**bold**` directly; do NOT draft in Slack mrkdwn `<url|text>` / `*bold*`, that just forces a conversion pass):
  - HYPERLINK EVERY Jira ticket and PR inline — `[KEY-NNNN](<jira-base-url>/browse/KEY-NNNN)`, `[PR #N](github-url)`, `[thread](slack-permalink)`. A bare `KEY-NNNN` in the Slack post is a regression — links are the whole point of the digest.
  - The send tool caps a message at **5000 chars per text element**; the full digest with links is usually ~7K. To fit WITHOUT dropping ticket links: trim PROSE (shorten descriptions, cut In-progress/Up-next to the top 2-3 items per person), never the hyperlinks.
  - If it still won't fit under 5000 with links intact, split at PERSON boundaries: parent message = owner sections (§7b/§7c) + first few people; threaded replies (thread_ts = parent ts) = remaining people, then the Team summary. Never drop links to fit.
  - Use `•` bullets.

Read-only on all data sources (events.db, Jira, Confluence, on-call). The ONLY write is the Slack post — no md files.

Working dir: __REPO__/work-context

## RECORD SUCCESS (final step — gates the 30-min retry)
ONLY after this run's deliverable is CONFIRMED — not merely attempted (the team digest actually landed in Slack) — stamp the marker so the rest of today's fires idle:

    TZ=Asia/Kolkata date +%F > __REPO__/work-context/state/last_routine_standup_success.date

If the run errored or the post could not be delivered, do NOT stamp: leave the marker so the next 30-min fire retries.

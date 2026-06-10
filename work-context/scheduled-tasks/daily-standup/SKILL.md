---
name: daily-standup
description: Weekday 11:45 IST — runs /standup for the previous working day and posts the team digest to the team-standup channel.
---

Run the daily team standup and post it to Slack.

STEP 1 — Determine the target date (previous WORKING day, IST):
- Today is the run day. Compute the previous working day:
  - If today is Monday → target = last Friday (3 days ago).
  - If today is Tue/Wed/Thu/Fri → target = yesterday (1 day ago).
- (Sat/Sun never fire because the cron is Mon–Fri.)
- If the target date lands on a known holiday with ~no activity, fall back to the most recent working day before it and note that in the post.

STEP 2 — Run the standup skill for that date, team scope:
- Invoke the /standup skill with: team <target-date-YYYY-MM-DD>
- This is the SAME skill at .claude/commands/standup.md. Follow it exactly: roster = config/people.yaml scope:team (EXCLUDE the manager), credit by assignee/author not transitioner, in-progress/up-next/blockers = current board state, live leave scan + team_leaves, on-call from config/oncall.yaml (explicit On-call line), CMRs as ops, describe+enrich+link every ticket, write the md files to management/standup/<target-date>/ (team.md + per-person files).

STEP 3 — Post the team digest to Slack:
- Send the team digest (the contents of management/standup/<target-date>/team.md, or the rendered team digest) to channel ID __STANDUP_CHANNEL__ (the private team-standup channel).
- Use the slack send-message tool with that channel ID. If the post cannot be delivered (bot not a member, channel archived), DO NOT silently fail — report the error clearly in this run's output so it can be fixed.
- Lead the Slack message with a one-line header: "Team standup — <target-date> (for previous working day)".

- FORMATTING (Slack mrkdwn — the send tool renders standard markdown):
  - HYPERLINK EVERY Jira ticket and PR inline — `[KEY-NNNN](<jira-base-url>/browse/KEY-NNNN)`, `[PR #N](github-url)`, `[thread](slack-permalink)`. A bare `KEY-NNNN` in the Slack post is a regression — links are the whole point of the digest. (team.md already links them; do NOT strip the links when rendering to Slack.)
  - The send tool caps a message at **5000 chars per text element**; the full team.md is usually ~9K. To fit WITHOUT dropping ticket links: trim PROSE (shorten descriptions, cut In-progress/Up-next to the top 2-3 items per person), never the hyperlinks.
  - If it still won't fit under 5000 with links intact, split: post the header + per-person sections as the main message and the Team summary as a threaded reply (thread_ts = parent ts), rather than dropping links.
  - Use Slack-friendly bullets (`•`) and `*bold*` for names/headers.

Read-only on all data sources (events.db, Jira, Confluence, on-call). The only writes are the standup md files and the Slack post.

Working dir: __REPO__/work-context

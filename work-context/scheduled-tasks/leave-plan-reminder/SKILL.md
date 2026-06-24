---
name: leave-plan-reminder
description: 1st of every even month (Feb/Apr/Jun/Aug/Oct/Dec) at 10:00 IST — posts a team leave-plan reminder to the leave-plan channel asking the subteam to update their leave plan for the NEXT two months.
---

Post the recurring team leave-plan reminder to the leave-plan Slack channel
(channel_id: `__LEAVE_PLAN_CHANNEL__`) using the Slack MCP `slack_send_message`.

Working dir: __REPO__

STEP 1 — Compute the two target months.
- Take the CURRENT date in Asia/Kolkata (IST).
- M1 = the month AFTER the current month. M2 = the month after M1.
- Use full English month names. Handle year rollover (e.g. a December run → "January and February").
- Examples: run in June → "July and August". Run in April → "May and June". Run in December → "January and February".

STEP 2 — Post EXACTLY this single line to channel `__LEAVE_PLAN_CHANNEL__` (no preamble, no extra
text, keep the literal subteam token verbatim so Slack renders the group ping):

    <!subteam^__LEAVE_PLAN_SUBTEAM__> Please update leave plan for {M1} and {M2}

STEP 3 — Confirm it posted and output the returned message link.

HARD RULES:
- Target is the real channel `__LEAVE_PLAN_CHANNEL__` — this is LIVE and
  pings the subteam. Post exactly once.
- Do NOT add anything beyond the single line above. No greetings, no signature.
- Keep the raw `<!subteam^__LEAVE_PLAN_SUBTEAM__>` mention verbatim so Slack renders the group ping.

Cadence note: this fires once per occurrence (1st of even months at 10:00 IST), so there is no
retry/run-once gate — a single fire posts a single message. If the app was closed at fire time it
runs on next launch; the month math is still correct because it is computed at run time.

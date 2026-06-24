---
name: track-work-ticketize
description: Weekdays from 06:15 IST (retries every 30 min until it succeeds once) — runs /ticketize DETECT for the previous working day and posts the ticket-candidate list to the #track-work channel. DETECT only; never applies (apply stays gated on owner reply).
---

Run the daily /ticketize DETECT and post the candidate list to Slack. This is READ-ONLY on Jira — it ONLY proposes; it must NEVER create/transition Jira issues. Apply happens separately, gated on the owner's reply.

Working dir: __REPO__

## RUN-ONCE GATE (idempotent — this routine retries every 30 min until it succeeds once today)
Before doing ANY work, run this and obey it:

    MARK=__REPO__/work-context/state/last_routine_ticketize_success.date
    TODAY=$(TZ=Asia/Kolkata date +%F)
    if [ -f "$MARK" ] && [ "$(cat "$MARK")" = "$TODAY" ]; then echo "GATE: ticketize already succeeded today ($TODAY) — idle"; else echo "GATE: ticketize not done today — proceed"; fi

If it prints "already succeeded today" → STOP NOW: do not gather, detect, write the candidate md, or invoke the bot; end the run. Only proceed to the steps below if it prints "not done today".

STEP 0 — Resolve a yaml-capable python (the interactive/cron shell may pick a bare python3 without pyyaml):
  PY=$(for p in /opt/homebrew/bin/python3 python3 /usr/local/bin/python3; do "$p" -c 'import yaml' 2>/dev/null && { echo "$p"; break; }; done)
Use $PY for every python call.

STEP 0.5 — Shared pre-gather slack thread sweep (freshness; best-effort, once/day):
Ticketize gathers from events.db, so yesterday's late thread-replies must be ingested
first (same need as /standup — both fire ~06:00–06:15). The sweep is gated by a dated
marker: whichever of standup / ticketize runs FIRST sweeps + stamps; the other skips
(events.db is the shared cache). So ticketize is NOT silently dependent on standup having
run. Just call the shared script before the gather:

    bash __REPO__/bin/threads_sweep_once.sh

Best-effort: it always exits 0 and never blocks DETECT; stamps the marker only on a clean
sweep, so a failed sweep is retried next fire.

STEP 1 — Target window (previous WORKING day, IST):
- Mon → last Friday; Tue–Fri → yesterday. (Sat/Sun never fire — cron is Mon–Fri.)
- Call the resolved date <date> (YYYY-MM-DD).

STEP 2 — Run the /ticketize DETECT phase for <date>, team scope. This is the SAME skill at .claude/commands/ticketize.md — follow it EXACTLY:
- Gather via `$PY bin/standup_gather.py <date> team` (read-only).
- Resolve current on-call from work-context/config/oncall.yaml (Opsgenie) and SUPPRESS candidates assigned to the on-call.
- Detect the 4 signal classes (A adhoc-no-ticket, B future-ask-no-ticket, C CMR-with-no-board-ticket, D release-with-no-CMR). Be CONSERVATIVE — under-propose.
- Pre-create dedupe: search Jira (project=__JIRA_PROJECT__) by PR link / summary keywords; drop anything already tracked. Reporter≠assignee: never assign a reported defect to its reporter.
- Fingerprint + dedupe across runs: `echo '<seeds-json>' | $PY bin/ticketize_state.py annotate --date <date>`. DROP any with prior_status created/rejected.
- Write the proposal file management/standup/<date>/ticket-candidates.md (full per-candidate blocks, decision: pending; epic fallback __JIRA_PROJECT__-2882; placement active-sprint; Environment PROD default). Do NOT commit state in DETECT.

STEP 3 — Post to #track-work via the Relay bot (v1.5c — buttons):
- The Relay bot posts the candidates with **Approve / Reject buttons** and handles the click
  (owner-only) → live apply. So this routine does NOT slack-send the candidate list itself.
- The bot renders each candidate's `summary` + `why` into the Slack message, so the `why:` field
  in the candidate md MUST be HUMAN-READABLE — one plain sentence, prod terms, NO Jira-internal
  jargon ("link Associated", "placement: active-sprint", "source:" etc. stay out of `why`).
  Gloss any 🔴/🟡/🟢 in `why` if used ("🔴 = touches money").
- After writing the candidate md, post the buttons:
  ```
  $PY bin/relay_bot.py --post <date>
  ```
  It posts one buttoned block per OPEN candidate (or "No new ticketable gaps … ✅" if none),
  reading the channel from config. If it errors (bot not in channel / token), DO NOT silently
  fail — report the stderr in this run's output.
- OPTIONAL thoroughness note: after the bot post, you MAY `slack_send_message` ONE plain line to
  the same channel so the reader trusts the scan (e.g. "Checked N other CMRs — all already tracked
  or one-off data fixes."). No buttons, no jira jargon. Skip if nothing notable was dropped.
- Approval is now in Slack: the owner taps Approve/Reject on the bot message; the Relay
  LaunchAgent applies live. (Manual `/ticketize apply <date>` and typed-reply remain as fallback.)

HARD RULES: DETECT only — this routine performs ZERO Jira writes (the bot does the gated apply on
click). Roster = scope:team reports, on-call suppressed. Read-only on events.db / Jira / Slack-read;
the only writes are the candidate md file + invoking the bot post.

## RECORD SUCCESS (final step — gates the 30-min retry)
ONLY after the DETECT run is CONFIRMED complete — the candidate md was written and the Relay bot post landed (or the bot correctly posted "no new ticketable gaps") — stamp the marker so the rest of today's fires idle:

    TZ=Asia/Kolkata date +%F > __REPO__/work-context/state/last_routine_ticketize_success.date

A clean "no new gaps" run counts as success — stamp it. If the gather/detect errored or the bot post could not be delivered, do NOT stamp: leave the marker so the next 30-min fire retries.

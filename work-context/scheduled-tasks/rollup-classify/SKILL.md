---
name: rollup-classify
description: Weekday 15:00 IST — runs the full /rollup (240-day window): keyword pass → chat-classify pending subjects → apply verdicts → derived rebuild, then posts a run-summary to the #rollup channel.
---

Run the full activity rollup and post a run-summary to Slack.

This is the SAME skill at __REPO__/.claude/commands/rollup.md. Follow it EXACTLY — do not reimplement. Working dir: __REPO__/work-context.

CADENCE: weekday 15:00 IST. Lookback window = FULL 240 days (the rollup default). The dump only surfaces PENDING (unclassified) subjects, so most runs will be small even with the 240-day window.

STEP 1 — Phase 1 (keyword pass + dump):
  cd __REPO__/work-context && DAYS=240 derive/manual-rollup.sh dump
- If output contains "✓ no pending classifications": there is nothing to classify. SKIP straight to STEP 4 and post a "nothing pending" summary. Do NOT run apply.
- If output mentions "epic(s) need slug creation": run STEP 2 first.

STEP 2 — Phase 1.5 (epic-slug creation, ONLY if dump flagged it):
- Invoke /slug-epics (it reads state/pending_slug_creation.json, writes state/verdicts.epic_slugs.json, applies via derive/manual-rollup.sh apply-slugs).
- Then re-run STEP 1 (dump) and continue.

STEP 3 — Phase 2 (classify) + Phase 3 (apply):
- Read the classification rules FIRST: __REPO__/work-context/state/pending_classification.json.rules.md
- Then read all pending subjects: __REPO__/work-context/state/pending_classification.json
- Classify each subject per rules.md (domains + summary + risk_flags + confidence + ownership fields). For thin GitHub subjects with empty body + no matterai_summary, fetch the PR diff inline with `gh pr diff <num> --repo <owner/repo>` before classifying. Sync/merge PRs → domains [], confidence 0.80, no diff fetch. Honor ALL hard constraints + ownership constraints in rules.md (slug enum, team-id enum, confidence thresholds, epic_domain∈domains).
- Write the complete verdict JSON array to __REPO__/work-context/state/verdicts.json
- Apply: cd __REPO__/work-context && derive/manual-rollup.sh apply
  (runs apply_verdicts.py → ownership_corrections.py → cluster_ownership_rollup.py → run_rollup). Capture its final output.

CRITICAL — this run IS a fresh chat session, so the LLM classification happens HERE in this session. NEVER call the Anthropic API from a script and NEVER let a script do the classification. Scripts strip auth; chat does all LLM work. If you ever see an auth/credit error from a script, surface it and STOP — do not fall back to OpenAI chat.

STEP 4 — Post a run-summary to Slack channel #rollup (channel ID __ROLLUP_CHANNEL__):
- Use the slack send-message tool with that channel ID.
- The send tool renders STANDARD markdown — write `[text](url)` and `**bold**` directly (not Slack mrkdwn).
- Lead with a one-line header: "Rollup run — <today's date YYYY-MM-DD>".
- Body (concise, bullets with `•`):
  • subjects classified / stored / deferred (low-confidence) — from Phase 2.
  • any epics that needed slug creation (from STEP 2), or "none".
  • the final apply output highlights (verdicts applied, ownership corrections, derived rebuild status).
  • if nothing was pending: a single line "No pending classifications — nothing to roll up today."
- If the post cannot be delivered (bot not a member of #rollup, channel archived): DO NOT silently fail — report the error clearly in this run's output so it can be fixed. The bot must be invited to #rollup (__ROLLUP_CHANNEL__) for the post to land.

Read-only on all data SOURCES (events.db reads, Jira, Confluence, gh). The ONLY writes are: the rollup apply (subject_summary + ownership cols + derived rebuild, which is the skill's job) and the Slack post.

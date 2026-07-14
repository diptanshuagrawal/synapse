---
name: housekeeping-review
description: Weekly (Mondays, retries every 30 min until it succeeds once that week) — runs the deterministic project prune (old backups/verdicts/handoffs/logs/.DS_Store + stale-MPIM) AND a classification layer that scans for FURTHER cleanup candidates, judges each, and posts Approve/Reject cards to #rollup. Suggest-only: nothing extra is deleted without an explicit Approve click.
---

Run the housekeeping job — prune the known-safe stuff, then surface + classify further cleanup candidates and post them to #rollup for Approve/Reject. This OWNS the prune now (the old weekly `com.example.housekeeping` launchd job is disabled). The only deletes that happen unattended are the deterministic prune (steps 1–7 of housekeeping.sh, same as always); every FURTHER suggestion is gated on an owner click.

Working dir: __REPO__/work-context. (STEP 0 resolves the python for the render + relay-post; housekeeping.sh uses the project venv internally for the prune + scan.)

## RUN-ONCE GATE (idempotent — this routine retries every 30 min until it succeeds once THIS WEEK)
Before ANY work, run this and obey it:

    MARK=__REPO__/work-context/state/last_routine_housekeeping_review_success.week
    LOCK=__REPO__/work-context/state/housekeeping_review_inprogress.lock
    WEEK=$(TZ=Asia/Kolkata date +%G-%V)
    NOW=$(date +%s)
    LOCKTS=$(cat "$LOCK" 2>/dev/null); LOCKTS=${LOCKTS:-0}
    if [ -f "$MARK" ] && [ "$(cat "$MARK")" = "$WEEK" ]; then echo "GATE: housekeeping-review already succeeded this week ($WEEK) — idle"
    elif [ -f "$LOCK" ] && [ $((NOW - LOCKTS)) -lt 2700 ]; then echo "GATE: another housekeeping-review run is in progress (lock age <45min) — idle"
    else echo "$NOW" > "$LOCK"; echo "GATE: not done this week — proceed"; fi

If it prints "already succeeded this week" OR "another housekeeping-review run is in progress" → STOP NOW: do nothing, end the run. Only proceed if it prints "not done this week — proceed".

(The lock closes the same >30-min-run race validated on daily-standup 2026-07-13 — the marker is checked at start but stamped at end, so a run longer than 30 min overlaps the next cron fire and the deliverable posts twice. The lock is stamped at start; a crashed run's stale lock self-expires after 45 min so retries still happen.)

## STEP 0 — resolve a python that can drive the Relay post
The render + relay-post need `slack_sdk` + `yaml`, which live in the SYSTEM python3, NOT the
project venv. Pick the first that has both:

    PY=$(for p in /opt/homebrew/bin/python3 python3 /usr/local/bin/python3; do "$p" -c 'import yaml, slack_sdk' 2>/dev/null && { echo "$p"; break; }; done)

Use $PY for the STEP-3 render + relay-post calls. (housekeeping.sh runs its OWN venv python
internally for the prune + scan — those need the project deps and are independent of $PY.)
cd into __REPO__/work-context first.

## STEP 1 — deterministic prune (the existing job, unchanged)
    cd __REPO__/work-context && bash bin/housekeeping.sh --apply 2>&1 | tee -a logs/housekeeping.log
Steps 1–7 delete/truncate the known-safe patterns + prune stale MPIMs. The `tee` keeps the
HOUSEKEEPING lane in cron-status fed. If this errors, do NOT stamp success — let the 30-min
retry handle it.

## STEP 2 — scan for FURTHER candidates (facts only, no deletion)
    cd __REPO__/work-context && bash bin/housekeeping.sh --scan
Writes `state/housekeeping_candidates.json`.

## STEP 3 — classify + render + post
Follow `.claude/shared/housekeeping-classify.md` EXACTLY (STEP A–D):
- Read `state/housekeeping_candidates.json`. If `summary.n == 0` → write `{"verdicts":{}}`, render,
  and the relay post will say "nothing further to clean ✅".
- Judge each candidate (delete / truncate / worktree_remove / keep / investigate) per the
  per-category rules. Be conservative — when unsure, `investigate`, never `delete`.
  NEVER recommend deleting a git-tracked path (the renderer + apply both refuse it anyway).
- Write `state/housekeeping_verdicts.json`, then:
      cd __REPO__/work-context && $PY -m derive.housekeeping_render --verdicts state/housekeeping_verdicts.json
  Note the `run_id` it prints.
- Post the cards to #rollup:
      cd __REPO__ && $PY bin/relay_bot.py --post-housekeeping <run-id>
  The Relay LaunchAgent (already live) handles Approve → git-safe delete, Reject → skip.
  If the bot errors (not in #rollup / token), report the stderr — do NOT stamp success.

HARD RULES: the ONLY unattended deletes are the STEP-1 prune. Every STEP-3 suggestion is
suggest-only — applied only on the owner's Approve click in Slack. Read-only on Jira/Slack-read.

## RECORD SUCCESS (final step — gates the 30-min retry)
ONLY after the prune ran, the scan + render completed, and the Relay post landed (or correctly
posted "nothing further to clean"), stamp the weekly marker AND release the in-progress lock:

    TZ=Asia/Kolkata date +%G-%V > __REPO__/work-context/state/last_routine_housekeeping_review_success.week
    rm -f __REPO__/work-context/state/housekeeping_review_inprogress.lock

A clean "nothing further" run counts as success — stamp it. If any leg failed, do NOT stamp —
but DO `rm -f` the lock so the next 30-min fire retries immediately (a crashed session that
never reaches this step is covered by the lock's 45-min self-expiry).

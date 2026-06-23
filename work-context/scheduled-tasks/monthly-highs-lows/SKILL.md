---
name: monthly-highs-lows
description: Monthly on the 28th from 12:00 IST (retries every 30 min on days 28–31 until it succeeds once that month) — complete /refresh-embeddings, then /ask "highs and lows" for the month (1st→28th) which writes a /retro artifact, then push the highs/lows to the standup-updates channel.
---

Monthly job: first run the COMPLETE embeddings refresh (same pipeline as `refresh-embeddings-weekly`), then produce the month's highs & lows via the `/ask` skill (which delegates to `/retro`), then push the result to the standup-updates channel.

Working dir: __REPO__/work-context

## RUN-ONCE GATE (idempotent — this MONTHLY routine retries every 30 min on days 28–31 until it succeeds once this month)
Before doing ANY work, run this and obey it:

    MARK=__REPO__/work-context/state/last_routine_monthly_highs_lows_success.month
    MONTH=$(TZ=Asia/Kolkata date +%Y-%m)
    if [ -f "$MARK" ] && [ "$(cat "$MARK")" = "$MONTH" ]; then echo "GATE: monthly-highs-lows already succeeded this month ($MONTH) — idle"; else echo "GATE: monthly-highs-lows not done this month — proceed"; fi

If it prints "already succeeded this month" → STOP NOW: refresh nothing, query nothing, post nothing; end the run. Only proceed to the steps below if it prints "not done this month".

## WINDOW (compute once, in IST — the current calendar month, 1st → 28th)

    MONTH=$(TZ=Asia/Kolkata date +%Y-%m)     # e.g. 2026-06
    SINCE=${MONTH}-01                          # inclusive start (e.g. 2026-06-01)
    UNTIL=${MONTH}-28                          # inclusive end   (e.g. 2026-06-28)

Remember SINCE / UNTIL / MONTH for the steps below. The retro covers the month-to-date through the 28th.

---

## STEP 1 — COMPLETE EMBEDDINGS REFRESH (same pipeline as `refresh-embeddings-weekly`)

This is the SAME skill at `__REPO__/.claude/commands/refresh-embeddings.md` — follow it exactly. All commands run from `__REPO__/work-context`. Embeddings use the OpenAI key from `~/.secrets` (NOT a routine token). NO Anthropic API calls anywhere — OpenAI is used ONLY for `text-embedding-3-*`; if a script throws an auth/credit error, surface it and STOP (failure, no stamp).

1a — Pre-flight status:
```bash
.venv/bin/python derive/refresh_embeddings.py status
```
- If `embed_required == 0`: corpus already in sync. The refresh is DONE for this run — print "✓ nothing to embed; corpus in sync" and skip straight to STEP 2.
- Else note the one-line delta (n_new / n_drifted).

1b — Noise filter + refresh WITH APPLY:
```bash
.venv/bin/python derive/cluster_noise_filter.py refresh
.venv/bin/python derive/refresh_embeddings.py refresh --min-cluster-size 5 --jaccard-threshold 0.8 --apply
```
- Capture the JSON. If `embed.errors` is non-empty, the orchestrator stops because the OpenAI key is missing, or apply fails: this is a FAILURE — DO NOT stamp. Report the error and STOP. The next 30-min fire retries.
- Note `diff_plan.summary` → preserve / relabel / new / dropped_old.

1c — In-session finalize labeling — ONLY if `summary.new + summary.relabel > 0`:
```bash
.venv/bin/python derive/finalize_refresh.py dump
```
- Read `state/pending_cluster_finalize.json.rules.md` FIRST, then `state/pending_cluster_finalize.json`.
- Write `state/verdicts.cluster_finalize.json` — one combined entry per cluster with ALL fields (label + status + decisions + blockers + outcomes + followups + risk_areas + root_cause + stakeholders + artifacts + participant_roles), per rules.md.
- THIS run IS a fresh chat session — the labeling LLM work happens HERE, in-session. NEVER call the Anthropic API from a script; NEVER fall back to OpenAI chat (OpenAI = embeddings only).
```bash
.venv/bin/python derive/finalize_refresh.py apply
```
- (apply also auto-stubs Recurring clusters, links clusters→projects.yaml slugs, re-derives ownership. `clusters_unmapped > 0` is expected — note it, no action.)
- If `summary.new + summary.relabel == 0`: no labeling needed; skip to 1d.

1d — Integrity gate:
```bash
.venv/bin/python derive/topic_brief_validate.py --json > state/last_topic_brief_validate.json
.venv/bin/python -c "import json;d=json.load(open('state/last_topic_brief_validate.json'));print('null_label',d.get('n_null_label'),'null_status',d.get('n_null_status'),'FAIL',d.get('n_fail'))"
```
- Refresh SUCCESS requires: apply completed AND no null labels / null status left AND 0 FAIL. (If new+relabel was 0, integrity is trivially clean.)
- If null labels/status remain or FAIL > 0: FAILURE — DO NOT stamp. Report what's unresolved and STOP; the next fire retries.

---

## STEP 2 — HIGHS & LOWS via `/ask` (delegates to `/retro`)

Invoke the `/ask` skill — the SAME skill at `__REPO__/.claude/commands/ask.md` — with this question (substitute the computed dates):

```
/ask highs and lows from <SINCE> to <UNTIL>
```

- `/ask` classifies this as the **highs_lows** intent and delegates to `/retro since=<SINCE>T00:00:00Z until=<UNTIL>T23:59:59Z`.
- `/retro` writes the durable artifact to `management/retros/<SINCE>-to-<UNTIL>.md` (e.g. `management/retros/2026-06-01-to-2026-06-28.md`). Follow `/ask` + `/retro` exactly — stakeholder-facing team-level voice, Highs = production deliveries only, Lows = delays/incidents/not-shipped, real impact numbers from slack threads, no dev names / no PR-ticket-cluster jargon.
- Read-only on all data sources + in-chat synthesis: NO LLM API calls.
- Confirm the retro file exists and is non-empty after the run. If `/ask` / `/retro` errors or writes nothing → FAILURE: DO NOT stamp, report, STOP. The next fire retries.

---

## STEP 3 — PUSH the highs/lows to the standup-updates channel (channel ID `__DEV_UPDATES_CHANNEL__`)

Read the retro file written in STEP 2, then post it as the OWNER (via the slack send-message tool — posts go out as the owner, not a bot; the owner must be a member of the channel).

- The send tool renders STANDARD markdown — write `[text](url)` and `**bold**` directly (NOT Slack mrkdwn). The retro body is already stakeholder markdown; post it as-is.
- Lead with a one-line header: `📈 Monthly highs & lows — <Month YYYY>` (e.g. "Monthly highs & lows — June 2026"; derive the month name from MONTH), then the `## Highs` and `## Lows` content.
- **5000-char cap per text element.** If header + Highs + Lows exceeds 5000 chars with links intact: post the header + `## Highs` as the ROOT message, then post `## Lows` as a threaded reply (thread_ts = the root message's own ts). Never drop content to fit; split at the Highs/Lows boundary.
- If the post cannot be delivered (not a member, channel archived) → DO NOT silently fail and DO NOT stamp: report the error in this run's output. The post landing is REQUIRED for success — the next 30-min fire retries.

---

## STEP 4 — STAMP SUCCESS (final step — ONLY on confirmed success)

Stamp ONLY after ALL of: refresh clean (apply done + integrity 0 FAIL, or "nothing to embed"), the retro artifact written, AND the highs/lows post landed in the standup-updates channel:
```bash
TZ=Asia/Kolkata date +%Y-%m > __REPO__/work-context/state/last_routine_monthly_highs_lows_success.month
```
- This stops further retries this month.
- Print a final one-line verdict: refresh (embedded N / nothing-to-embed), retro written to `<path>`, posted to standup-updates, stamped `<MONTH>`.

Hard rules: `--apply` is the only mutator in the refresh. NO Anthropic API calls anywhere; OpenAI ONLY for embeddings; `/ask` + `/retro` synthesise in-chat with no API. On ANY failure (refresh error, retro empty, or the Slack post failing to land), DO NOT stamp — let the 30-min retry handle it through day 31 of the month.

---
name: refresh-embeddings-weekly
description: Wednesday from 17:00 IST (retries every 30 min through Thursday 23:00 until it succeeds once that cycle) — incremental /refresh-embeddings: embed delta → re-cluster → in-session finalize-label → apply.
---

Incrementally refresh the embedding + clustering pipeline, then label new/relabeled clusters in-session and apply. This is the SAME skill at __REPO__/.claude/commands/refresh-embeddings.md — follow it exactly. Working dir: __REPO__/work-context.

This routine intentionally overrides the skill's "owner-invoked, do not cron" note (owner decision 2026-06-18): run weekly on WEDNESDAY 17:00 IST, with a retry-until-success window through THURSDAY 23:00 IST (next-day EOD). The cron fires every 30 min on Wed+Thu; STEP 0 gates each fire so only the real window runs and it stops once successful.

STEP 0 — RETRY-UNTIL-SUCCESS GATE (run FIRST, every fire):
Run this to decide whether to proceed. Compute everything in Asia/Kolkata (IST).
```bash
cd __REPO__/work-context
.venv/bin/python - <<'PY'
from datetime import datetime, time
from zoneinfo import ZoneInfo
import pathlib
now = datetime.now(ZoneInfo("Asia/Kolkata"))
wd = now.weekday()  # Mon=0 .. Wed=2, Thu=3
# anchor = this cycle's Wednesday date (YYYY-MM-DD)
if wd == 2:        anchor = now.date()
elif wd == 3:      anchor = (now.date().fromordinal(now.date().toordinal()-1))
else:              print("IDLE: not Wed/Thu"); raise SystemExit
# window: Wed >= 17:00, Thu <= 23:00
if wd == 2 and now.time() < time(17,0): print("IDLE: before Wed 17:00 IST"); raise SystemExit
if wd == 3 and now.time() > time(23,0): print("IDLE: past Thu 23:00 IST (missed cycle)"); raise SystemExit
stamp = pathlib.Path("state/last_routine_refresh_embeddings_success.date")
if stamp.exists() and stamp.read_text().strip() == str(anchor):
    print(f"IDLE: already succeeded this cycle ({anchor})"); raise SystemExit
print(f"PROCEED anchor={anchor}")
PY
```
- If the output starts with "IDLE": print that one line and STOP the run. Do nothing else.
- If it prints "PROCEED anchor=YYYY-MM-DD": remember that anchor date and continue. The anchor is the success-stamp value for the final step.

STEP 1 — Pre-flight status (Phase 2):
```bash
.venv/bin/python derive/refresh_embeddings.py status
```
- If `embed_required == 0`: corpus is already in sync. This counts as SUCCESS for the cycle. Stamp the success date (STEP 5) and STOP — print "✓ nothing to embed; cycle complete".
- Else print the one-line delta (n_new / n_drifted) + new_head/drifted_head.

STEP 2 — Noise filter + refresh WITH APPLY (Phase 4):
```bash
.venv/bin/python derive/cluster_noise_filter.py refresh
.venv/bin/python derive/refresh_embeddings.py refresh --min-cluster-size 5 --jaccard-threshold 0.8 --apply
```
- Capture the JSON. If `embed.errors` is non-empty, or the orchestrator stops because the OpenAI key is missing, or apply fails: this is a FAILURE — DO NOT stamp success. Report the error clearly in the run output and STOP. The next 30-min fire will retry.
- Note `diff_plan.summary` → preserve / relabel / new / dropped_old.

STEP 3 — In-session finalize labeling (Phase 6) — ONLY if `summary.new + summary.relabel > 0`:
```bash
.venv/bin/python derive/finalize_refresh.py dump
```
- Read state/pending_cluster_finalize.json.rules.md FIRST, then state/pending_cluster_finalize.json.
- Write state/verdicts.cluster_finalize.json — one combined entry per cluster with ALL fields (label + status + decisions + blockers + outcomes + followups + risk_areas + root_cause + stakeholders + artifacts + participant_roles), per rules.md.
- THIS run IS a fresh chat session — the labeling LLM work happens HERE, in-session. NEVER call the Anthropic API from a script; NEVER fall back to OpenAI chat. OpenAI is used ONLY for embeddings. If a script throws an auth/credit error, surface it and STOP (failure, no stamp).
```bash
.venv/bin/python derive/finalize_refresh.py apply
```
- (apply also auto-stubs Recurring clusters, links clusters→projects.yaml slugs, and re-derives per-cluster ownership. Capture the cluster_project_link block; clusters_unmapped > 0 is expected — note it, no action needed in the routine.)
- If `summary.new + summary.relabel == 0`: no labeling needed; skip to STEP 4.

STEP 4 — Integrity gate (Phase 7):
```bash
.venv/bin/python derive/topic_brief_validate.py --json > state/last_topic_brief_validate.json
.venv/bin/python -c "import json;d=json.load(open('state/last_topic_brief_validate.json'));print('null_label',d.get('n_null_label'),'null_status',d.get('n_null_status'),'FAIL',d.get('n_fail'))"
```
- SUCCESS requires: apply completed AND no null labels / null status left (the label loop closed; integrity shows 0 FAIL). If new+relabel was 0, integrity is trivially clean → success.
- If null labels/status remain or FAIL > 0: FAILURE — DO NOT stamp. Report what's unresolved and STOP; the next fire retries.

STEP 5 — Stamp success (ONLY on confirmed success):
```bash
printf '%s\n' "<ANCHOR-from-STEP-0>" > state/last_routine_refresh_embeddings_success.date
```
- Substitute the anchor date STEP 0 printed. This stops further retries this cycle.
- Print a final one-line verdict: embedded N, clusters preserved/relabel/new/dropped, integrity clean, stamped <anchor>.

Hard rules: `--apply` is the only mutator. NO Anthropic API calls anywhere. OpenAI only for embeddings. On ANY failure, do not stamp — let the 30-min retry handle it until Thu 23:00 IST. No Slack post for this routine (owner did not request reporting); the session completion notification is the report.

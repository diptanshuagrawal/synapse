---
description: Monthly EM-review pack builder — assembles the three agenda sections (CX top issues, top-20 business metrics MoM, initiative impact scorecard) into one presentable markdown pack. Reads the metrics log (events.db), Slack dashboard images, the monthly plan (plan-A.json / OINT epics), and Jira impact fields. Owner-invoked, read-only on every source; posts nothing.
---

# /em-review — monthly EM review pack

Builds the pack for the EM monthly review agenda:
1. CX issues raised by customers
2. Business metrics for the savings+transactions domain vs last month (top 20)
3. Monthly-planning initiatives: stated Impact → actual progression

Output: `work-context/metrics-report/em-review-<YYYY-MM>.md` + a chat summary.
Read-only everywhere. Never posts to Slack; the owner presents the pack.

## Step 0 — Review window

Default: the last FULL calendar month (invoked early Sep → reviews Aug).
`$ARGUMENTS` may override with `YYYY-MM` and/or a path to the CX dump file.

## Section 1 — CX top issues

Three inputs, use whichever exist:

**A. CX dump from Karil** (the analytical core). Look for the newest file in
`work-context/metrics-report/cx/` (any csv/xlsx) or the path given in `$ARGUMENTS`.
If found:
- Top issue categories by ticket volume (exact counts, no estimates).
- MoM comparison if a prior month's dump exists in the same folder.
- Top 5 customer-facing issues, each with count + one representative ticket/example.
If missing: say so in the pack — "CX dump not provided; ask Karil" — and build
from input B alone. Never fabricate CX numbers.

Channel IDs are workspace-specific — read them from
`work-context/config/em_review.local.yaml` (keys under `cx_channels`; template:
`em_review.example.yaml`). If that file is missing, say so in the pack and ask the
owner — never guess a channel ID.

**B. Daily CX ticket images** in `#dsa-live-metrics` (ID = `cx_channels.daily_tickets`): the bot
posts a "CX DSA Ticket Status Report — Touched Unresolved" image daily (~07:00 IST).
These are OUT of the daily metrics-routine scope, so read them here on demand:
sample the month's first, last, and each Monday's image; transcribe
touched/unresolved counts exactly; render the month trend line.

**C. CX Product-Level breakdown** in `#cx_metrics` (ID = `cx_channels.product_breakdown`): daily image
"CX Product-Level breakdown | <date>" (~10:30 IST) splits CX interaction volume by
product — Digital Savings Account, Payments, Credit Card, borrow, Merchant — with
Past-30-Days, MTD, and Previous-Month-MTD columns. Read the image posted on the
1st of the following month: its Past-30-Days column ≈ the review month. Report the
domain share (DSA + Payments) and its MoM direction. Row format is
`Users (Interactions)`; the Total row is reversed: `Interactions (Users)` — read
the headers, don't assume. Same channel also has "CX Ticket Ops | Daily Metrics"
(org-wide volume by pod + untouched %/resolution SLAs) — cite only when an ops
regression is part of the month's story. `#freeze_cx_metrics` (ID = `cx_channels.freeze_cx`) exists
for freeze-specific CX; check it when the freeze-construct initiative is under review.

## Section 2 — Top-20 business metrics MoM

Canonical metric names come ONLY from the metrics routine's table in
`work-context/metrics-report/ROUTINE.md` — same names, same units, no ad-hoc inventions.

**Primary source:** `metrics_readings` in
`~/context/work-context/events.db`
(open with `PRAGMA busy_timeout=30000;`). Per metric: end-of-month reading and
month average vs prior month's.

**Bootstrap fallback** (log younger than two full months): the dashboards print
month comparisons natively — Liabilities Overall has MTD vs LMTD columns; UPI MTD
images print M-o-M change %. Read the review-month's LAST posted image per
dashboard from the source channels and transcribe the MTD/LMTD (or MoM) columns
exactly. Mark rows sourced this way with `†` (image-derived).

**Selection:** ~20 rows, weighted to the domain story — savings funnel (signups,
onboarded, approval, activations), balances (total liabilities, DSA, FD, atom),
deposits (created, GTV, SR), DSA payments (txns, SR, GTV, users), add money
(GTV, SR), UPI legs (txns + SR per leg, MTD txns/GTV where they matter).

**Format:** fixed-width table — `metric | this month | last month | Δ%` — plus a
3-bullet "what moved and why it matters" reading below it. Unreadable/missing
values render as `N/A`, never invented.

## Section 3 — Initiative impact scorecard

**Inputs:**
- Monthly plan: `work-context/derived/plan-A.json` (and `plan-B.json` if present) —
  `allocations: [{key: OINT-*, summary, months: {Mon: SP}}]` for the review month.
  If the plan artifacts predate the review month, JQL for the owner's OINT
  initiatives active in the month instead.
- Jira, per initiative key: status, resolution, story-point progress of children,
  and the **Impact field = `customfield_11858`** (mapping lives in
  `work-context/config/sprint_planning.yaml`). Creds: `~/.secrets/atlassian_*`,
  JQL POST to `/rest/api/3/search/jql` — same access pattern as
  `derive/resolve_prefetch.py`; reuse that script's plumbing where it fits.

**Per initiative, produce:**
- Stated Impact (verbatim from customfield_11858; if empty → "no impact recorded
  in Jira — pull from the monthly planning doc", don't invent one).
- Delivery state: planned SP for the month (plan-A) vs done/remaining (Jira children).
- Impact progression: if the stated impact names a metric that exists in the
  canonical table, pull that metric's actual curve from `metrics_readings`
  (or the image MTD/LMTD fallback) and state the movement with numbers.
  Otherwise: "impact not yet measurable from tracked metrics" + what would need
  tracking.
- Classification: `shipped, impact visible` / `shipped, impact pending` /
  `in-flight` / `slipped` — derived from delivery state + impact movement,
  one line of justification each.

## Assembly

Write `work-context/metrics-report/em-review-<YYYY-MM>.md`:

```
# EM Review — <Month YYYY> (Savings + Transactions)
## 1. CX — top customer issues
## 2. Business metrics — MoM (top 20)
## 3. Initiatives — impact scorecard
## Appendix: sources & confidence
```

The appendix lists every source used (dump filename, image file IDs, Jira keys,
log row counts) and any `partial`/`failed`/`N/A` readings — caveats live here,
not in the sections, so sections 1–3 stay copy-pasteable into slides.

Then give a 5-line chat summary: the one-line story per section + the pack path.

## Constraints

- Read-only: no Jira writes, no Slack posts, no channel additions.
- Numbers are transcribed, never recomputed or rounded beyond the source.
- Canonical metric names only; new metrics get added to ROUTINE.md first.
- Missing input ≠ blocker: build what's buildable, name what's missing.

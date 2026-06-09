Synthesize a STAKEHOLDER-FACING engineering retrospective (highs + lows) over a flexible date range, grounded in `events.db` + slack thread content. Output goes to `management/retros/<START_DATE>-to-<END_DATE>.md` (durable artifact — referenced for trend comparison + 1:1 prep, NOT ephemeral scratch).

## Usage — `/retro <date-range>`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run anything.

**What it does:** Synthesizes a stakeholder-facing engineering retrospective (highs + lows) over a date range, grounded in `events.db` + Slack threads; writes a durable artifact under `management/retros/`.

**Sections it produces:**
- Highs — production deliveries ONLY (user-facing rollout with measurable impact: RPS, latency, success rate, accounts).
- Lows — dev-complete-but-not-rolled-out, slipped dates, incidents.
- Team-level voice; no dev names, no PR/ticket/cluster jargon.


**Usage:** `/retro <date-range>`
- `date-range` (required) — e.g. `2026-04-01 to 2026-05-01`, or a month/quarter.

**Audience: cross-team stakeholders / leadership.** Not engineering peers. Hard rules:

- **Team-level voice only.** "The team delivered X" / "instant-pay rollout reached 70% coverage". NEVER dev names ("Frank delivered X" is wrong — leadership reads at team level).
- **Highs = DELIVERIES only.** "Code merged" or "ticket Done" ≠ delivery. Production rollout to users, CMR Implementation-Reviewed, feature live with end-user impact = delivery. Dev-only progress (code done, awaiting rollout) goes in §Lows as "X dev complete, rollout slipping to May".
- **Every high needs measurable business / flow / platform impact.** Numbers from slack threads (RPS, latency, success rate, accounts, cost, downtime saved). Pull from the team's own rollout-update posts — don't invent.
- **No tech-internal jargon.** No PR/ticket counts. No cluster references. No window_state / lookahead. Stakeholder doesn't know these.
- **No IC-level metrics.** Velocity, sp_completion, after_hours_share — all internal. Drop.
- **Lows = delays + incidents + work that didn't ship.** Explicit attribution of cause + next-step rollout date when known.
- **Match the owner's prior precedent.** Read past stakeholder updates (e.g. Feb/March highs-lows messages) to copy voice, structure, level of detail.

Format (matches the owner's Feb + March stakeholder messages in slack):
- Numbered `## Highs` and `## Lows` (no other sections).
- Each item: **one-line headline** with the delivery, then sub-bullets with concrete numbers + Impact line.
- Each impact line: "Impact: <business / flow / platform outcome with numbers if measurable>".

**Phase 1 below covers signal-gathering. Phase 2 narrative shape is the LOCKED stakeholder format described above — NOT the older engineering-internal shape.**

## Input parsing

User input: `$ARGUMENTS`.

Accepted forms:

| Input | Meaning |
|-------|---------|
| `2026-04-15 2026-05-14` | Explicit start (inclusive) + end (inclusive), space-separated |
| `2026-04-15..2026-05-14` | Same, range syntax |
| `30` (any integer) | Trailing N days ending today (UTC) |
| _(empty)_ | Ask the user: "Provide a date range — `YYYY-MM-DD YYYY-MM-DD`, `YYYY-MM-DD..YYYY-MM-DD`, or `N` for trailing N days." Then stop. |

Compute `START_TS` and `END_TS` as ISO8601 UTC strings:
- For explicit dates: `START_TS = "<start>T00:00:00Z"`, `END_TS = "<end>T23:59:59Z"`.
- For trailing N days: `END_TS = now UTC`, `START_TS = END_TS - N days` at 00:00:00Z.

Reject:
- End before start → error message, stop.
- Window > 365 days → confirm with user before proceeding (large windows produce noisy retros).
- Window < 7 days → confirm — retros under a week rarely have signal.

Print computed window before proceeding:
```
Retro window: <START_TS> → <END_TS> (<N> days)
```

## Phase 0 — load shared Jira-metrics module (REQUIRED)

**Single source of truth: `derive/jira_metrics.py`.** Do NOT re-implement attribution / dedup / ops-detection / ownership inline. Skills consume the module; if a metric needs to change, change the module.

```python
import sys; sys.path.insert(0, '$HOME/work-context')
from derive.jira_metrics import (
    load_people_lookup, get_aliases_for, all_team_canonicals,
    compute_done_credits, filter_credits_for,
    aggregate_velocity_by_actor, aggregate_velocity_by_sprint,
    attribution_source_summary, team_velocity_baseline,
    compute_pr_author_ownership,
    detect_ops_tickets, OPS_PATTERNS,
    strip_epic_prefix,
)
import sqlite3
conn = sqlite3.connect('$HOME/work-context/index/events.db')
people = load_people_lookup()
```

Module contracts in §narrative.md Phase 0 (same module). Key for retro:
- `team_velocity_baseline(conn, start, end)` → for retro's "Metrics" + "Lows" sections (top deliverers, sprint pacing).
- `compute_done_credits(...)` → use for shipped epic / per-domain SP totals.
- `detect_ops_tickets(...)` per top actor → ops-incident-response items per-person.
- `compute_pr_author_ownership(...)` per top actor → for "Owned" framing in retro Per-person notes.

**Never inline SQL for these computations.** If retro needs a new metric, add it to the module.

## Phase 1 — CENSUS (REQUIRED, primary discovery — run FIRST)

**Recall is guaranteed by census, not by sampling feeds.** Synthesising from
clusters + MoM alone silently misses anything not in those feeds (proven: a
100% go-live buried in a 73-reply thread; on-call incidents auto-stubbed as
noise; a zero-downtime year-end close mis-attributed to a sister team via its
broadcast-channel author). The census enumerates EVERY window subject and
partitions it exhaustively, so discovery is complete + auditable.

```bash
cd $HOME/work-context
.venv/bin/python derive/retro_census.py \
    --since "<START_TS>" --until "<END_TS>" > /tmp/retro_census.json
.venv/bin/python derive/retro_census.py \
    --since "<START_TS>" --until "<END_TS>" --format summary   # eyeball
```

**Coverage gate — HARD STOP.** Read `coverage_ok` + `totals.unclassified`.
If `coverage_ok != true` OR `unclassified > 0`, STOP and surface the gap — the
census is the denominator; an unaccounted subject means a silent-miss risk.

The census JSON carries:
- `totals` — subjects / represented / noise / **unclassified (must be 0)**
- `by_ownership` — team / sister / external counts
- `by_signal` — incident / alert_auto / rollout / delivery / fix / cmr_ops / work / design / pr_work / discussion / noise
- `incidents[]`, `rollouts[]` — full sub-censuses with evidence URLs + (rollout) `confirmed` ∈ {true, false=announced-unconfirmed, null}
- `buckets` — `<ownership>/<signal>` → subject list (the candidate pools)
- `ownership_audit` — `identity_fallback_subjects_total` (thin-content residual) + `review_domains_in_window` (ambiguous slugs to confirm)

**The team candidate set = all `team/*` buckets (+ `sister/*` where the team
co-responds).** This is what Highs/Lows draw from. Ownership is content-first
(domains→team via `domain_team_map.yaml`); a sister-primary item the team
co-owns still surfaces via co_owners — do NOT silently drop cross-team wins.

Detectors only ROUTE (structural: channel role, jira issue_type, source;
keywords as fallback). They are HINTS — the irreducible signal-type judgement
is yours, applied over the COMPLETE candidate set, not over curated feeds.

## Phase 1-enrich — gather supporting signals (cluster/MoM/per-person framing)

These ENRICH the census candidates with framing + measured impact. They are NOT
the discovery mechanism (the census is). Run these queries from
`$HOME/work-context/`. DB path is `index/events.db`.

### 1a. Event volume + cycle time per source

```bash
sqlite3 -header -column index/events.db <<SQL
SELECT source, event_type, COUNT(*) AS n
FROM events
WHERE ts BETWEEN '<START_TS>' AND '<END_TS>'
GROUP BY source, event_type
ORDER BY source, n DESC;
SQL
```

### 1b. PR cycle time (opened → merged, hours)

```bash
sqlite3 -header -column index/events.db <<SQL
WITH opens AS (
  SELECT subject, MIN(ts) AS opened_ts FROM events
  WHERE event_type = 'pr_opened' AND ts BETWEEN '<START_TS>' AND '<END_TS>'
  GROUP BY subject
),
merges AS (
  SELECT subject, MIN(ts) AS merged_ts FROM events
  WHERE event_type = 'pr_merged' AND ts BETWEEN '<START_TS>' AND '<END_TS>'
  GROUP BY subject
)
SELECT
  COUNT(*) AS n_merged,
  ROUND(AVG((julianday(merged_ts) - julianday(opened_ts)) * 24), 1) AS avg_hours,
  ROUND(MIN((julianday(merged_ts) - julianday(opened_ts)) * 24), 1) AS min_hours,
  ROUND(MAX((julianday(merged_ts) - julianday(opened_ts)) * 24), 1) AS max_hours
FROM merges m JOIN opens o USING (subject)
WHERE m.merged_ts >= o.opened_ts;
SQL
```

### 1c. Per-person activity counts

```bash
sqlite3 -header -column index/events.db <<SQL
SELECT actor, event_type, COUNT(*) AS n
FROM events
WHERE ts BETWEEN '<START_TS>' AND '<END_TS>'
  AND actor IS NOT NULL
  AND actor NOT LIKE '%[bot]%'
  AND actor != 'matterai'
GROUP BY actor, event_type
ORDER BY actor, n DESC;
SQL
```

### 1d. Shipped epics (status_change → Done within window)

```bash
sqlite3 -header -column index/events.db <<SQL
SELECT subject, actor, title, ts
FROM events
WHERE source = 'jira'
  AND event_type = 'status_change'
  AND ts BETWEEN '<START_TS>' AND '<END_TS>'
  AND title LIKE '%→ Done%'
ORDER BY ts;
SQL
```

### 1e. Sprint composition (active sprints overlapping window)

```bash
sqlite3 -header -column index/events.db <<SQL
SELECT sprint_name, sprint_state,
       COUNT(DISTINCT subject) AS tickets,
       ROUND(SUM(story_points), 1) AS total_points
FROM events
WHERE source = 'jira'
  AND event_type = 'issue_created'
  AND sprint_name IS NOT NULL
  AND sprint_name != ''
GROUP BY sprint_name, sprint_state
ORDER BY tickets DESC
LIMIT 10;
SQL
```

(Note: sprint membership is point-in-time-of-ingest, not point-in-time-of-window. For an exact in-window roster, cross-reference `sprint_change` events.)

### 1f. Risk-flagged subjects (security/data-loss/panic/race/migration/breaking-api)

```bash
sqlite3 -header -column index/events.db <<SQL
SELECT s.subject, s.summary, s.risk_flags, s.confidence
FROM subject_summary s
JOIN events e ON e.subject = s.subject
WHERE e.ts BETWEEN '<START_TS>' AND '<END_TS>'
  AND s.risk_flags != '[]'
  AND s.risk_flags != ''
GROUP BY s.subject
ORDER BY s.confidence DESC
LIMIT 30;
SQL
```

### 1g. Stale + drive-by anti-patterns in window

Read `derived/alerts.md` (always whole-file — small). Note: the file reflects the latest rollup, not the retro window. Use it as a *current state* snapshot, not for in-window-only attribution.

### 1h. Domain volume (top 10 by subject count in window)

```bash
sqlite3 -header -column index/events.db <<SQL
WITH win_subjects AS (
  SELECT DISTINCT subject FROM events
  WHERE ts BETWEEN '<START_TS>' AND '<END_TS>' AND subject IS NOT NULL
)
SELECT domain.value AS domain, COUNT(*) AS subjects
FROM subject_summary,
     json_each(subject_summary.domains) AS domain
WHERE subject IN (SELECT subject FROM win_subjects)
GROUP BY domain.value
ORDER BY subjects DESC
LIMIT 10;
SQL
```

### 1i. Read per-domain rollups for top 5 domains

For each of the top 5 domain slugs from 1h: read `$HOME/work-context/derived/projects/<slug>.md` and extract:
- "Recent items" section (gives MatterAI summaries of top PRs in the domain)
- Contributor leaderboard

### 1j. Read per-person profiles for active actors

From 1c: identify top 5-8 contributors by total activity in window. For each, read `$HOME/work-context/derived/people/<handle>.md` — extract `## Activity summary` + `## Narrative` sections.

### 1k. Topic clusters active in window (Phase D — cluster-grained framing)

The `topic_brief` table provides cluster-grained workstream framing on top of raw events. Query it for both highs (ACTIVE clusters with new decisions in window) and lows (clusters whose `root_cause` is non-null + recent activity).

```bash
cd $HOME/work-context
.venv/bin/python derive/ask_engine.py window \
    --since "<START_TS>" --until "<END_TS>" > /tmp/retro_active_clusters.json
.venv/bin/python derive/ask_engine.py rootcauses \
    --since "<START_TS>" --until "<END_TS>" > /tmp/retro_root_causes.json
```

Each cluster carries `label`, `status`, `decisions_json`, `blockers_json`, `root_cause`, `participants_json`, `member_count`, `source_breakdown_json`, `first_ts`, `last_activity_ts`.

Use these for:
- the **Workstreams** Highs section (replaces ad-hoc domain narrative from 1h/1i for richer framing)
- the **Incident themes** Lows section (cluster-level root_cause distillation)
- enriching Per-person notes (per-cluster roles from `participants_json`)

### 1k-bis. Project-level rollup (prefer this over cluster lists)

HDBSCAN splits big initiatives across multiple clusters (e.g. Revamp lives in clusters 352 + 297 + 281). Aggregate via `cluster_project_map` to surface project-level deliveries:

```bash
.venv/bin/python derive/ask_engine.py projects-window \
    --since "<START_TS>" --until "<END_TS>" > /tmp/retro_projects.json
```

Each entry: `project_slug`, `cluster_count`, `member_count_total`, `top_cluster_labels[]`, `linked_cluster_ids[]`, `status_distribution`. **Use this as the primary scoping for the Highs section.** For each top project_slug:

1. Pull `projects.yaml::name` for the human-readable label (e.g. `accounting-revamp` → "Accounting Platform Revamp").
2. For each cluster in the project's linked set, pull `outcomes_json` from `topic_brief` and union them.
3. Render as ONE stakeholder bullet per project with measured-impact numbers.

This replaces per-cluster bullets (which would scatter one initiative across 3-5 entries). Cluster IDs stay invisible to the reader — only project names appear in the rendered retro.

**Important**: cluster_ids are not stable across re-clusters. Always reference clusters by `label`, never by `cluster_id`, in the rendered output.

**Cluster status vs window_state.** Each cluster JSON carries TWO state fields:

- `status` — current state TODAY (`ACTIVE` / `STALE` / `RESOLVED` / `RECURRING`). Reflects NOW, not the asked window.
- `window_state` — derived per-query, describes cluster lifetime vs window. Values: `fully_in` / `started_in` / `ended_in` / `spans` / `pre_window` / `post_window`.

For retros >30d ago (Feb asked in May), `status` reads STALE for almost everything — uninformative. **Use `window_state` for narrative framing** on historical retros:
- `spans` or `fully_in` → "active workstream during <window>"
- `started_in` → "workstream kicked off in <window>"
- `ended_in` → "workstream wrapped up in <window>"

Never frame a historical retro using `status='STALE'` — mislabels what the team was focused on at the time. The lifetime-overlap filter (`first_ts < until AND last_activity_ts >= since`) catches all in-window-active clusters; the old `last_activity_ts in [since, until)` filter missed everything that carried through the window.

### 1l. Per-actor cluster roles (joins 1c × 1k)

For each top contributor from 1c, scan `/tmp/retro_active_clusters.json` + `/tmp/retro_root_causes.json` for entries where the person appears in `participants_json` with role IN (AUTHOR, RESOLVER, DECIDER) — use those for Per-person notes "led" framing.

### 1m. Weekly-sync MoM extraction (REQUIRED for Highs grounding)

Cluster-grained framing favours sustained workstreams over point-in-time announcements. Concrete go-live dates + measured impact (% rollout, ₹ revenue, branch counts) live in weekly-sync MoM threads, which often DON'T form their own cluster — they're status callouts inside larger threads. Without this step, the retro misses real team deliveries (e.g. "instant-pay ATM live <date> ~₹X day-one charges").

```bash
.venv/bin/python derive/mom_extractor.py \
    --since "<START_TS>" --until "<END_TS>" \
    > /tmp/retro_moms.json
```

Default scrape channel: `C0EXAMPLE` (service-c-internal / service-c Weekly Sync). Override via `--channels` if the team uses a different MoM venue.

Each MoM entry contains: `ts`, `title`, `root_actor`, `root_body` (full text), `replies[]` (top 8 chronological replies with actor + body), and `subject_url` (slack permalink for citation).

**Use this as a co-primary signal alongside `/tmp/retro_active_clusters.json` + `/tmp/retro_projects.json`.** For each MoM in window:

1. Scan `root_body` + first 3-5 replies for bulletted items matching go-live / rollout / completion keywords: `live`, `rollout`, `completed`, `deployed`, `migrated`, `100%`, `<N>%`, `production`, `cohort N`.
2. Extract date + scope + numeric impact (₹, % users, # branches, # accounts, # FDs).
3. Match each extracted item back to one of the project_slugs from 1k-bis (e.g. "instant-pay ATM live 19 May" → `instant-pay-atm` project bullet).
4. If a project_slug has both cluster signal AND MoM signal — use MoM date + impact in the headline, cluster framing for context.
5. If a MoM-only item has no matching cluster (e.g. one-off fix announcement) — still surface as a high if it has measured impact.

**Citation:** every MoM-derived high MUST link the MoM `subject_url` so the reader can verify the claim. Cluster-derived highs cite cluster `evidence_subject` URLs per existing rules.

**Hybrid scoping:**
- MoM-grounded highs get the SPECIFIC date and impact number in the headline.
- Cluster-grounded highs get the WORKSTREAM framing + scope.
- A delivery with both signals (typical) merges them: "Cash Scale Rollout — 1 branch (14 May) → 8 branches (18 May). 11 active artefacts across rollout + ops CMRs."

## Phase 2 — synthesise retro markdown (CENSUS-DRIVEN + reconciliation)

Write the output to `$HOME/context/management/retros/<START_DATE>-to-<END_DATE>.md` (use ISO dates from the window, no time component, e.g. `2026-04-15-to-2026-05-14.md`).

### Synthesis contract (REQUIRED — this is how recall is guaranteed)

Judge signal-type over the COMPLETE team candidate set from Phase 1 census
(`team/*` buckets + `sister/*` where the team co-responds). Enrich with cluster
framing + MoM dates/impact. Then:

1. **Highs = DELIVERIES ONLY** — shipped to users / live in production / platform
   change live. Pull from `rollout` (confirmed), `delivery`, `pr_work`, and
   confirmed go-lives in MoM. A design/TRD/epic/Done-task is NOT a delivery →
   it goes to Lows ("scoped/dev-complete, rollout slipped"). Every High needs
   measured impact (numbers from MoM / rollout threads).
   **The `delivery` bucket includes executed CMRs** (jira status reached
   `Change Released` / `Implementation Reviewed`) — these are PLATFORM wins
   (DB/index/infra optimisations shipped to prod), not toil. Scan `delivery`
   for platform deliveries, not only user-facing ones — an executed
   enhancement CMR (e.g. index-defrag SP parallelisation) is a legitimate High.
2. **Lows** — from `incident` (the team incident sub-census — reconcile every
   one), `fix`/`cmr_ops` (manual-correction toil), `alert_auto` if a real
   pattern, plus deliveries that slipped + designs-not-shipped.
3. **Rollout `confirmed=false` (announced-unconfirmed)** — do NOT claim as a
   High and do NOT silently drop. Put in the reconciliation as a flagged
   verify-item (the "IMPS going live, no confirmation" case).
2a. **Window-edge — exclude pre-window deliveries.** The census `window_edge[]`
   block lists delivery candidates whose terminal status was reached BEFORE the
   window (work delivered an earlier month, only closed/touched in-window — the
   example-db / IMPS-pods leak). Do NOT claim these as in-window Highs. If one
   is genuinely a this-window delivery (rare), confirm with a real in-window
   ship event before promoting. Otherwise drop them with reason in the
   reconciliation. Symmetrically, a PR `pr_opened` in-window but not yet merged
   is in-flight, not a delivery.
3a. **Impact must be READ from the source, not summarised from the title.**
   For every High AND Low, open the source event body (the rollout/MoM thread,
   the incident thread, the ticket) and pull the MEASURED numbers it contains —
   RPS, p99/p95 latency, account counts, % rollout, ₹ revenue, downtime
   minutes, branch counts, success-rate. A generic impact line ("improves
   reliability") is INSUFFICIENT when the thread carries numbers. Example: the
   instant-pay 100% go-live thread states "instant-pay RPS dropped to ~0" + "N lakh
   accounts, NN RPS peak, p99 NN ms" — all of that belongs in the impact line.
   If the source has no numbers, say so; do not invent.
4. **Reconciliation appendix (REQUIRED)** — after Highs/Lows, append a
   `## Coverage & reconciliation (audit — not for stakeholder copy)` section:
   - census coverage line (subjects / represented / noise / unclassified=0 / coverage_ok)
   - by_ownership + a candidate-accounting table: every team delivery/incident/
     rollout signal → *in-retro (High/Low N)* or *dropped-with-reason*
   - `ownership_audit`: identity-fallback count + any review-domains
   - **Open verification items** — every `confirmed=false` rollout + any
     ownership the resolver was unsure about. Flag, don't decide silently.

This appendix is the auditable proof that no candidate was silently missed. It
is NOT stakeholder copy — keep it below a `---` so the Highs/Lows read clean.

### Output structure (STAKEHOLDER format — locked)

```markdown
# Liabilities — Transactions · Highs & Lows for <Month Year>

## Highs

1. **<headline: what shipped + scale>.**
    - Sub-bullets: timeline / scope / numbers.
    - Impact: <business / flow / platform outcome with measured numbers>.

2. ...

## Lows

1. **<headline: what failed / what didn't ship + why>.**
    - Sub-bullets: incident details / scope / mitigation.
    - Impact: <user-visible / ops-visible outcome>.

---

## Coverage & reconciliation (audit — not for stakeholder copy)

_From `derive/retro_census.py` over <window>. Every subject partitioned; nothing sampled._

**Census coverage:** <N> subjects · represented <M> · noise <K> · **unclassified 0** (`coverage_ok = true`).
**Ownership:** team <t> · sister <s> · external <e>.

| Census signal (team) | Disposition |
|---|---|
| <signal — item> | <High N / Low N / dropped-with-reason> |

**Open verification items (flagged, need owner confirm):**
- <every rollout confirmed=false + any unsure ownership>
```

**Stakeholder body = Highs + Lows ONLY.** No TL;DR, Metrics table, per-person notes, "Open threads", or "Inputs" footer in the stakeholder section. The owner's Feb + March stakeholder messages in slack are the precedent — copy that voice. The **reconciliation appendix below the `---` is REQUIRED** (the recall-audit proof) but is explicitly NOT stakeholder copy — it stays below the divider.

The internal engineering signals below are for SYNTHESIS — they help identify highs/lows. They do NOT appear in the output verbatim.

---

### Internal signals (for synthesis only — do NOT render directly)

The legacy retro shape (with Metrics / TL;DR / Per-person notes) is deprecated for /ask highs_lows + /retro. Keep these signals as inputs to Phase 2 synthesis. Below table is for HISTORICAL reference of what signals exist:

| Metric | Value | Notes |
|--------|-------|-------|
| PRs opened | <n> | from 1a |
| PRs merged | <n> | from 1a |
| Jira tickets created | <n> | from 1a |
| Confluence pages touched | <n> | from 1a |
| PR cycle time (avg) | <hours>h | from 1b |
| PR cycle time (max) | <hours>h | from 1b |
| Active contributors | <n> | from 1c, count of distinct actors with ≥1 pr_opened OR ≥3 review |
| Risk-flagged subjects | <n> | from 1f |
| Domains touched | <n> | from 1h |

| Metric | Value | Notes |
|--------|-------|-------|
| PRs opened | <n> | from 1a |
| PRs merged | <n> | from 1a |
| Jira tickets created | <n> | from 1a |
| Confluence pages touched | <n> | from 1a |
| PR cycle time (avg) | <hours>h | from 1b |
| PR cycle time (max) | <hours>h | from 1b |
| Active contributors | <n> | from 1c, count of distinct actors with ≥1 pr_opened OR ≥3 review |
| Risk-flagged subjects | <n> | from 1f |
| Domains touched | <n> | from 1h |

---

## Highs

### Workstreams active in window (cluster-grained — primary signal)

From 1k `/tmp/retro_active_clusters.json`: for each cluster with `status='ACTIVE'` and `last_activity_ts` in window, produce ONE bullet:

```
**<label>** — <synthesised from top 2 decisions_json entries>. <member_count> items across <source_breakdown_json>. Led by <top participant by contribution_count, with role if non-null>.
```

Sort by `member_count` desc. Cap at 8 bullets.

Skip clusters with `status='RECURRING'` (templates — not work). Skip `STALE` here (covered in stale-themes follow-up).

Cite evidence: include 1-2 `evidence_subject` URLs per cluster (slack/jira/page/github URL conventions from `derive/validate_embeddings.py:subject_url`).

### Shipped this window (event-grained — supplement, not replacement)

For each shipped epic from 1d: one bullet — `[<key>](<url>) — <title>` (strip `[Epic …]` prefix from title).

For each of top 5 domains in 1h not already covered by a cluster above: one bullet per domain. Format: `**<domain-slug>** — <one-line synthesis of recent items section>. <count> PRs, <count> contributors.` (Avoids double-counting workstreams that the cluster section already named.)

### Person highlights

For each of the top 3-5 actors from 1j: one bullet — `**<handle>** (<role from team.md if known>) — <one-sentence synthesis of their narrative + recent PRs section>.`

Be specific. Reference actual PR numbers when surfaced by MatterAI. Avoid generic praise ("did great work") — use the concrete signal.

### Domain ramp / new ownership

If any person × domain pair in window shows substantially higher activity than their 240d baseline (inferable from per-person profile), call it out. Skip section if no clear signal.

---

## Lows

### Incident themes (cluster-grained — primary signal)

From 1k `/tmp/retro_root_causes.json`: for each cluster with non-null `root_cause` AND `status != 'RECURRING'` AND `last_activity_ts` in window, produce ONE entry:

```
**<label>** — <root_cause>. Blockers: <comma-list of top 2 blockers_json[].text> (if any). <member_count> incidents.
```

Sort by `last_activity_ts` desc. Cap at 6 entries. For each, include 1-2 evidence URLs from `blockers_json[].evidence_subject` or top member.

This section replaces the old per-incident drilldown — it tells you *what kept failing as a real workstream*, not just *what individual templates fired*.

### Recurring noise (collapsed)

For RECURRING clusters with `root_cause` and activity in window: collapse to one summary line. Format: `<N> recurring alert clusters fired in window: <comma-list of top 3 labels (truncated)>. See cluster details via /ask if specific patterns matter.`

This is a noise-suppression block — explicitly does NOT enumerate individual recurring alerts.

### Anti-patterns

- **Drive-by merges (current state from alerts.md):** <count>. Top mergers: <list>. If a single name dominates: flag.
- **Stale PRs (current state from alerts.md):** <count>. Average age: <X> days. Names of owners: <list>.
- **Long cycle-time outliers:** PRs in window with merge time >max threshold (>72h or >2× window-avg). Subject + actor + hours.

### Risk surfaces

For each risk_flag in 1f, group by flag:

- **security** (<count>): one bullet per subject — `[<subject>](<url>) — <summary>`. Skip if none.
- **race**: same format.
- **data-loss / panic / migration / breaking-api**: same.

### Concentration risk

From 1c: if any single person's activity is >40% of total team activity, flag as bus-factor concern. Skip if balanced.

### Sprint underdelivery (optional, requires sprint context)

From 1e: if any closed sprint in window has `total_points` set but most of the tickets in it ended outside the window (status_change → Done after END_TS), flag. Best-effort only — sprint boundaries aren't stored exactly.

---

## Per-person notes

Sectioned narrative (NOT a table) per person — bullets covering authored/shipped/ops items only. Reviews go in the activity table, not narrative. Significant ops items (DR drill, incident, deployment) get their own bullet, not parenthetical. (Format per `feedback_people_summary_format` + `feedback_people_summary_doneitems` memory.)

For each active contributor (≥1 PR opened or merged in window):

### <name from team.md, fallback to handle>

- Activity table:
  | event_type | count |
  |---|---|
  | pr_opened | <n> |
  | pr_merged | <n> |
  | review | <n> |
  | jira issue_created | <n> |
  | comment | <n> |
- **Led / drove**: one line per cluster from 1l where person has role in (AUTHOR, RESOLVER, DECIDER). Format: `<cluster label> — <role> · <contribution_count> events`. Skip if no led/drove rows.
- **Responded to**: one line listing cluster labels where person is RESPONDER/REVIEWER, comma-separated. Skip if empty.
- Narrative: 3-5 bullets of authored/shipped/ops work, with PR numbers + MatterAI risk keywords if surfaced.

---

## Open threads (forward-looking)

- Stale PRs needing owner attention (top 5 from alerts.md).
- Risk flags not yet resolved (any open `pr_opened` from 1f with no `pr_merged`).
- Domains with concentration risk (bus-factor < 2).

---

_Inputs:_
- `index/events.db` (events + subject_summary + person_narrative + `topic_brief` + `topic_brief_member`)
- `derived/alerts.md`, `derived/projects/*.md`, `derived/people/*.md`
- `config/people.yaml`, `config/projects.yaml`
- `management/context/team.md` for role lookup
- `derive/ask_engine.py` primitives: `window`, `rootcauses` (Phase D cluster-grained framing)

_Window:_ `<START_TS>` to `<END_TS>` (<N> days)
```

## Hard constraints

- Do not fabricate data. If a section has no signal, write `_None observed in window._` rather than padding.
- Do not paraphrase MatterAI summaries — quote them when surfacing a specific PR.
- Numbers must come from the SQL queries above. If a query returns 0 rows, surface that — don't infer.
- Do not include any subject/PR not actually in the window.
- Strip `[Epic EX-N]` prefixes from titles in the rendered output.
- Strip leading "Comment on EX-N" prefix when surfacing comment events.
- **Cluster framing (Phase D)**: reference clusters by `label`, never by `cluster_id` (ids reshuffle on re-cluster). If `topic_brief` is empty or has zero clusters in window, render the Workstreams + Incident-themes sections as `_None observed in window. Cluster pipeline (Phase B+C) may not have run for this window — verify with sqlite3 events.db "SELECT COUNT(*) FROM topic_brief WHERE last_activity_ts BETWEEN '<START>' AND '<END>'"._`
- **Falls back gracefully**: cluster sections are PRIMARY signal but the event-grained sections (1a–1h) remain authoritative for raw counts and per-PR detail. Do not delete a section because the cluster equivalent exists.

## After write

Print:
```
✓ Retro written → management/retros/<START_DATE>-to-<END_DATE>.md

Window: <START_TS> → <END_TS> (<N> days)
Highs: <count_high_bullets> bullets
Lows: <count_low_bullets> bullets
Per-person notes: <count_people> contributors
```

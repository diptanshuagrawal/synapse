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

## Phase 0+1 — ONE gather call (census + every enrich signal)

```bash
cd $HOME/context/work-context
.venv/bin/python derive/retro_gather.py \
    --since "<START_TS>" --until "<END_TS>"
```

This single call (2026-07-18; replaces the old ~12 sequential commands — census
×2, 7 sqlite heredocs, 3 ask_engine runs, mom_extractor, alerts/projects/people
file reads) runs everything concurrently and writes the full bundle to
`/tmp/retro_gather.json` (+ the legacy per-piece files:
`/tmp/retro_census.json`, `/tmp/retro_active_clusters.json`,
`/tmp/retro_root_causes.json`, `/tmp/retro_projects.json`,
`/tmp/retro_moms.json`). Stdout is a compact summary — check its
`coverage_ok` / `errors`, then Read the bundle. Do NOT re-run
`retro_census.py`, the 1a-1h SQL, `ask_engine.py`, or `mom_extractor.py`
individually — their outputs are already in the bundle under the keys named
in the sections below.

**Jira-metrics single source of truth: `derive/jira_metrics.py`.** The bundle's
`team_velocity` / `ops_by_person` / `ownership_by_person` keys are computed by
that module inside the gather. Do NOT re-implement attribution / dedup /
ops-detection / ownership inline; if a metric needs to change, change the
module (contracts in `ask.md` Phase 0). Uses in retro:
- `team_velocity` → "Metrics" + "Lows" framing (top deliverers, sprint pacing).
- `ops_by_person` → ops-incident-response items per person.
- `ownership_by_person` → "Owned" framing in per-person context.

### Census (bundle key `census` — REQUIRED, primary discovery)

**Recall is guaranteed by census, not by sampling feeds.** Synthesising from
clusters + MoM alone silently misses anything not in those feeds (proven: a
100% go-live buried in a 73-reply thread; on-call incidents auto-stubbed as
noise; a zero-downtime year-end close mis-attributed to a sister team via its
broadcast-channel author). The census enumerates EVERY window subject and
partitions it exhaustively, so discovery is complete + auditable.

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

## Phase 1-enrich — supporting signals (ALL already in the bundle)

These ENRICH the census candidates with framing + measured impact. They are NOT
the discovery mechanism (the census is). Every signal below is a KEY in
`/tmp/retro_gather.json` — read it there; run NO further queries:

- **1a `event_volume`** — per source/event_type counts.
- **1b `pr_cycle`** — opened→merged hours (n_merged / avg / min / max).
- **1c `person_activity`** — per-actor event counts (bots + matterai excluded;
  actors are RAW ids — the bundle's `people_profiles` / `team_velocity` keys
  are already canonicalised).
- **1d `shipped_done`** — jira `status_change → Done` rows in window.
- **1e `sprints`** — sprint composition, top 10. (Membership is
  point-of-ingest, not point-in-window; cross-reference `sprint_change`
  events for an exact roster.)
- **1f `risk_flags`** — risk-flagged subjects, top 30 by confidence.
- **1g `alerts_md`** — `derived/alerts.md` text. Reflects the LATEST rollup,
  not the retro window — a current-state snapshot, never in-window-only
  attribution.
- **1h `domain_volume`** — top 10 domains by window subject count.
- **1i `project_rollups`** — `derived/projects/<slug>.md` text for the top-5
  domains (extract "Recent items" + contributor leaderboard). Files are capped
  at ~8K chars in the bundle; a truncation note names the file to Read if you
  need the rest.
- **1j `people_profiles`** — profile md text for the top 5-8 window
  contributors (extract `## Activity summary` + `## Narrative`). Same 8K cap.

### 1k. Topic clusters active in window (Phase D — cluster-grained framing)

The `topic_brief` table provides cluster-grained workstream framing on top of raw events — highs (ACTIVE clusters with new decisions in window) and lows (clusters whose `root_cause` is non-null + recent activity).

Bundle keys **`active_clusters`** + **`root_causes`** (the gather also wrote the
legacy `/tmp/retro_active_clusters.json` + `/tmp/retro_root_causes.json`).

Each cluster carries `label`, `status`, `decisions_json`, `blockers_json`, `root_cause`, `participants_json`, `member_count`, `source_breakdown_json`, `first_ts`, `last_activity_ts`.

Use these for:
- the **Workstreams** Highs section (replaces ad-hoc domain narrative from 1h/1i for richer framing)
- the **Incident themes** Lows section (cluster-level root_cause distillation)
- enriching Per-person notes (per-cluster roles from `participants_json`)

### 1k-bis. Project-level rollup (prefer this over cluster lists)

HDBSCAN splits big initiatives across multiple clusters (e.g. Revamp lives in clusters 352 + 297 + 281). The bundle key **`projects_window`** (legacy `/tmp/retro_projects.json`) aggregates via `cluster_project_map` to surface project-level deliveries.

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

Bundle key **`moms`** (legacy `/tmp/retro_moms.json`). Default scrape channel:
`C0EXAMPLE` (service-c-internal / service-c Weekly Sync) — if the team uses a
different MoM venue, re-run `derive/mom_extractor.py --since … --until …
--channels <ids>` manually (the only case where a separate command is needed).

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
3a. **Impact must be READ from the source, not summarised from the title** —
   per `.claude/shared/evidence-grounding.md`. For every High AND Low, open the source body
   (rollout/MoM thread, incident thread, ticket) and pull its MEASURED numbers; a generic
   impact line ("improves reliability") is INSUFFICIENT when the thread carries numbers.
   **Batch the opens (speed):** first decide the full High/Low candidate list, THEN
   issue the source-body reads (slack threads, tickets, pages) as parallel tool
   blocks — several independent reads per message, never one open per turn. This
   loop is the retro's dominant wall-clock; keep it wide.
   Example: the instant-pay 100% go-live thread states "RPS dropped to ~0" + "N lakh accounts,
   NN RPS peak, p99 NN ms" — all of that belongs in the impact line.
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

(The legacy internal-signals template — metrics table, cluster-bullet Highs/Lows
shapes, per-person notes, open-threads, inputs footer — was DELETED 2026-07-18.
It was ~150 lines of deprecated output shape that the synthesis contract above
fully supersedes, re-read on every run for nothing; it also carried a
verbatim-duplicated metrics table. Phase 1's numbered gathers (1a-1l) remain the
synthesis INPUTS; none of them render verbatim. Git history has the old template
if ever needed.)

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

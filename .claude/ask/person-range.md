# /ask · person_range — dispatch chunk

Loaded by the `/ask` router (Phase 3). Before rendering, ALSO Read:
- `.claude/ask/narrative-style.md` — output style, deep-read, translation hard
  rules, pre-save grep-check, length guidance.
- `.claude/ask/person-template.md` — the locked output template (TL;DR /
  Signals / gates / Confirmed / Silent / Novel / Gaps / Interventions / Detail).

"Translation rules" below = `narrative-style.md`; the field map + completeness
gate are in THIS file.

**Modes:** default = full narrative (~600 lines). `brief` = condensed
(~150 lines) — trigger on `/ask brief <person>`, `/ask <person> brief|short`,
or any signal the user wants the headline read, not the audit-quality writeup.
Brief = "skim before 1:1"; full = "performance-review evidence pack". Unsure →
ask.

**Brief output shape (3 sections ONLY — no Confirmed / Silent / Novel / Gaps /
Interventions):**
1. **TL;DR** — 5-6 bullets, ≤25 words each.
2. **Signals** — 3 short paragraphs: (a) shipped + tier-band read, (b) how they
   worked (review/comment/slack shape + behavioral), (c) workstreams in one
   paragraph.
3. **Detail** — 3 paragraphs: primary workstream + secondary + standout pattern.

## Step 0 — deterministic RENDER MANIFEST (selection authority) — the ONLY script call

```bash
.venv/bin/python derive/person_v4_manifest.py --name "<canonical>" \
    --since "<iso>" --until "<iso>" --bundle-dir /tmp
```

One call writes `/tmp/<canonical>_manifest.json` + `_v3.json` + `_deep.json`
(the manifest computes v3 + deepread internally; the bundle persists them).
Read the manifest next; Read v3/deep only when you need their fields/quotes.
NEVER run `person_v3.py` / `person_deepread.py` as separate commands — that
recomputes on-disk work and doubles tool-call latency.

The manifest has ALREADY MADE every selection decision — which tickets / docs /
PRs / threads to cite, in which section, ranked and capped; flags; caveats;
tier verdict; footprint breadth + bus-factor; role-drift; key threads. This
kills run-to-run variance: **the model PHRASES the manifest, it does not
curate it.** Render rules:

- `headline.tldr_facts[]` — one TL;DR bullet per fact, IN ORDER. Never
  reorder, drop, or add facts.
- `sections.{shipped,designed,db_platform,ops,workstreams,own_prs}` — cite
  every item; add nothing, drop nothing.
- `flags[]` — every flag MUST appear (TL;DR or matching Signals/Gaps section).
  `workload_sentiment` is the regression this exists to prevent — never omit.
- `caveats[]` — each renders in the Caveats section.
- `footprint` (breadth + `bus_factor_candidates`) / `role_drift` /
  `review_concentration` / `key_threads` / `narrative_signals` (tier verdict,
  sprint cadence, owned-domain shares, ops count, MatterAI, pace) — render per
  the field map + translation rules.
- `verify_manifest[]` — the verify gate's contract; not rendered.

Steps 1-2 below are the manifest's INTERNAL inputs, already on disk — use them
for phrasing fields + SUPPLEMENTARY citation quotes (a thread body, a comment
preview) the manifest didn't bundle. The manifest governs WHICH artefacts +
ordering; the field map below governs HOW to phrase each signal.

## Step 1 — v3 fields (Read `/tmp/<canonical>_v3.json`; never re-run the script)

Merges complete-recall discovery + signal taxonomy + workstreams + V1 rating,
with TRACK-ROUTING that fixes feature-SP mis-rating of platform/ops engineers.
Contains: `coverage` (+`coverage_ok`), `rating` (window_work_mix /
baseline_role_120d / feature_yardstick_applicable / v1_feature_verdict / note),
`workstreams` (led vs contributed), `delivery` (shipped / fixed / responded_to
/ designed / built — each own vs contributed), `own_by_signal`, `window_edge`
(delivered-before-window → exclude), `v1_signals` (sprint cadence, PR fate,
behavioral, domain_ownership, gates).

**TRACK-ROUTED verdict (REQUIRED):**
- `feature_yardstick_applicable = true` (feature/mixed window) → render the V1
  feature tier verdict. For `mixed`, also surface workstream leadership the
  SP/PR lens misses.
- `= false` (platform/ops window) → do NOT headline the feature tier verdict.
  State work-mix + baseline role, mark feature-SP not-the-yardstick, evaluate
  on delivery sections + workstreams.
- Always pair window_work_mix with baseline_role_120d ("platform this window,
  normally <baseline>") — distinguishes a genuine platform engineer from a
  feature dev in a migration-heavy month.

**Narrative shape from V3:** lead Signals "What shipped" from
`delivery.shipped` (own first, then contributed); platform track gets a
platform/ops shipped sub-block; Workstreams section from `workstreams` (Led =
AUTHOR/RESOLVER/DECIDER, Contributed = RESPONDER/participant — WINDOW role,
reconciled with `project_footprint`). Cite measured impact from the source
thread — read the body, don't summarise the title.

**RENDER CONTRACT (REQUIRED — read every field from v3; do NOT re-derive
field names from raw deepread JSON or hand-run probes).** Every narrative
section maps to named v3 fields — render ALL of them (a prior run silently
dropped the behavioral block by querying wrong keys; must not recur):

| Narrative section | person_v3 field | Notes |
|---|---|---|
| TL;DR verdict framing | `rating.*` | work_mix + baseline + yardstick + note |
| Signals · How worked | `contribution.*`, `v1_signals.*`, `review_concentration` | cite `substantive_pr_commits`, `pr_reviews_total`, `confluence_edits`, `substantive_slack_replies`, `cross_surface_breadth`; reviewer-of-record cluster. High commits-in-PR / 0-own-PRs inversion is a real signal — name it. |
| Signals · When worked | `behavioral.*` | first_responder/resolver/p50/p90/after_hours/weekend/followup — verbatim; if null: "n=`behavioral.samples`, not computed" |
| Signals · What shipped | `delivery.shipped/designed/built/fixed/ops` | own first, then contributed |
| Signals · Pace | `pace.*` | pr_cycle_median_days, slow>14d, same_day, shipped/abandoned/in_flight |
| Signals · Quality | `quality.*` | matterai p50, critical_flags, reverts, pr_count |
| Signals · Throughput | `completion.*` + `v1_signals.team_rank/team_sp_count/team_median_sp/team_top_sp/sp_attributed` | render BOTH primary + lookahead sp_completion; contextualize rank with median + top |
| Signals · Throughput (ops band) | `v1_signals.ops_track_deviation` + `verdict_suppressed_reason` | when yardstick=false AND CMR-heavy, the OPS-band verdict is the headline |
| Signals · Throughput (ticket fate) | `ticket_fate.resolved_in_lookahead` | tickets closed just after window — pair with lookahead story |
| Workstreams | `workstreams[]` | led-first; window role |
| Role drift / handoffs | `role_drift[]` | dedup by `project_slug`; lifetime→window role drop = POSSIBLE handoff — state as drift, never assert "handed off to X" without a confirming thread |
| Detail · review lead | `review_concentration` | name the cluster + c/rev/commit split |
| Project footprint | `project_footprint[]` | top by window_event_count — but ALSO call out every `top_role_in_project == AUTHOR` slug regardless of rank (a low-event sole-ownership slug must not truncate away) |
| Gaps · risk PRs | `v1_signals.risk_flagged_prs` | list in Gaps if non-empty; skip line if empty |
| Caveats · attribution | `v1_signals.attribution_chain` | `creation_fallback > changelog` → caveat: SP attribution less certain |

**Completeness gate:** before writing the file, confirm every section above is
present. Null/empty field → render the section with explicit "not computed
(n=…)" — NEVER silently omit. A narrative missing behavioral, pace, quality,
review_concentration, or role_drift is incomplete — regenerate it.

## Step 2 — deepread CITATION MATERIAL (Read `/tmp/<canonical>_deep.json`; never re-run)

V3 gives structure + verdict; deepread bundles the quote-able raw material for
Detail + Confirmed-by-data. Top-level keys:

- `profile` — full person_profile output (schema v3; contract below).
- `clusters[]` — top 10 clusters touched: brief + `person_role` +
  `person_contrib_count` + top 5 members. **Write the Detail workstream
  paragraphs from these.**
- `assigned_tickets[]` — every window ticket: issue_type / story_points /
  sprint / latest_status / title / creator. Cite in Confirmed + Detail.
- `prs[]` — every PR opened (title + opened_ts); pair with
  `profile.fate.pr_fate[]` for shipped/abandoned.
- `confluence[]` — every page event (title + body_bytes).
- `jira_comments[]` — top 20 by length on others' tickets, with preview.
  Investigation-depth evidence.
- `slack_threads[]` — top 20 thread_started by reply count (channel + count +
  preview). High-reply = escalations.

Only run older single-purpose scripts for a primitive deepread doesn't bundle
(e.g. `ask_engine.py search` for topic searches, `ask_engine.py window` for
attention windows).

**Profile contract (`schema_version=3`):** top-level `person`, `tier`,
`window`, `aliases`, `contribution`, `behavioral`, `throughput`, `quality`,
`narrative`, `fate`, `meta`. Render Signals VERBATIM from those numbers — do
NOT recompute. Reliability gates set `throughput.verdict.tier_deviation=null`
with `verdict_suppressed_reason` — honour it; never emit a suppressed tier
verdict. `cmr_heavy_role` flagged → use `throughput.verdict.ops_track_deviation`
(ops band) instead of the feature verdict.

**`narrative` block** (via `derive/jira_metrics.py`):
- `team_rank / team_sp_count / team_median_sp / team_top_sp / sp_attributed /
  tickets_attributed` — assigned-and-shipped SP + team rank → TL;DR ("ranks #N
  in team SP this window").
- `by_sprint[]` — {sprint_name, sp, tickets, state} → Detail sprint cadence
  (steady deliverer vs end-of-window spike).
- `attribution_chain` — {changelog, creation_fallback, unknown}; unknown>0 →
  Caveat; creation_fallback dominates → attribution less certain, flag.
- `domain_ownership[]` — per-domain PR-author share: OWNED (≥40%) / DROVE
  (≥25%) / CONTRIBUTED (≥1 PR <25%) / JIRA_ONLY (0 PRs) → TL;DR + Confirmed.
- `ops_tickets[]` — OPS_PATTERNS title hits → dedicated Ops & Incidents block
  in Detail if ≥3; skip otherwise.
- `risk_flagged_prs[]` — risk_flags on their PRs → Gaps if non-empty.

**`fate` block** (window-edge bias correction):
- `pr_fate[]` — every window pr_opened with terminal event: {status:
  shipped|abandoned|in_flight, terminal_ts, days_to_terminal,
  terminal_in_window}. Read PR work HONESTLY: shipped-in-window vs
  merge-fell-after-window vs abandoned.
- `pr_fate_summary` — counts incl. shipped_in_lookahead; >0 → TL;DR callout
  that domain_ownership undercounts at window edge.
- `ticket_fate` — in-flight-at-until tickets re-checked at until+lookahead:
  {in_flight_at_until_total, resolved_in_lookahead[], shifted_to_shipped,
  shifted_to_cancelled}.
- `lookahead_throughput.feature_track.sp_completion_rate_pct` — primary vs
  lookahead completion. Below-band primary + in-band lookahead = window-edge
  artefact, NOT under-delivery. Render BOTH in the throughput paragraph
  ("61.1% primary → 80.7% with 30d lookahead").
- `lookahead_domain_ownership[]` — which JIRA_ONLY labels were edge artefacts
  vs genuine 0% author share.

**Hard rule:** lookahead delta large (sp_completion shifts >15pp, or
shipped_in_lookahead>0 with pr_count_in_window<5) → the primary verdict MUST
read "in this window", never "under-delivering", with the lookahead number in
the same sentence.

**Pace = PR cycle time, NOT ticket lead time.** The team flips tickets
To-Do→In-Progress→Done in one batch at close (tickets recorded post-hoc), so
`lead_days` reads ~1 day for everything — useless. PR opened/merged timestamps
are real. Use `fate.pr_fate_summary.pr_cycle_median_days` +
`slow_pr_count_over_14d` as the §Signals Pace sub-section ("median PR cycle 3
days — fast" / "median 0 days, 5 same-day PRs — small-PR quick-ship pattern").
Citing a slow PR → name it + days ("his April-20 get-API refactor took 22 days
to merge"). `pr_cycle_median_days` None (no PRs) → say so; note ticket-level
pace can't be inferred from this team's workflow. NEVER cite
ticket-lead-time-days.

**Cluster rendering:** clusters ordered by reply_count desc. One section per
cluster (not per ticket), 1-2 highlight subjects each (cite URL). Skip
`status='RECURRING'` clusters unless reply_count dominates (then: "X spent 40%
of incident time on opsgenie acks").

**Cluster status vs window_state — read the right field.** `status` = TODAY
(ACTIVE/STALE/RESOLVED/RECURRING, labelled at last finalize). `window_state` =
cluster-lifetime ↔ window relationship (fully_in / started_in / ended_in /
spans / pre_window / post_window / unknown). Recent windows (last 30d) → frame
by `status`. Historical windows (>30d ago) → frame by `window_state`
(spans/fully_in = "active during <window>", started_in = "kicked off in",
ended_in = "wrapped up in"). NEVER call a historical-window cluster "STALE" —
that mislabels the past from present state.

**Cluster ownership filter — `home_team_owned_pct`** (from
owner_distribution_json; refreshed by /rollup + /refresh-embeddings applies).
Suppress sister-team noise when the question is about the owner's team:
highs/deliveries → keep ≥0.70; lows/incidents → keep ≥0.30; <0.30 = mostly
another team's work — exclude or attribute to them. Subject-level:
`owned_by_primary` + `co_owners_json`. Never expose the pct in prose —
express scope via real artefacts (no-cluster-language rule).

## Verify gate (MANDATORY for person_range — after the file is saved)

Deterministic check that every `verify_manifest[]` token — every cited
ticket/PR/page, every `flag:*`, every `caveat:*` — actually landed in the
prose. Kills the silent-drop bug class (a shipped ticket / workload flag /
caveat missing on a given run).

```bash
.venv/bin/python derive/verify_render.py \
    --manifest /tmp/<canonical>_manifest.json \
    --file management/narratives/per-person/<canonical>-<since>-to-<until>.md
```

- Exit 0 / PASS → done. End the chat reply with `**Verify:** PASS (all N
  manifest items present)`.
- Exit 1 / FAIL → the printed list names every missing item. SURFACE the list,
  rewrite to include them, re-run. Loop until PASS — or, if an item genuinely
  cannot be placed, say so explicitly with the reason; never drop silently.

person_range only (the manifest is person_range-scoped); other intents skip it.

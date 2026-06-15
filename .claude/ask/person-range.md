# /ask · person_range — dispatch chunk

Loaded by the `/ask` router (Phase 3). Before rendering, ALSO Read:
- `.claude/ask/narrative-style.md` — output style, deep-read, translation hard
  rules, pre-save grep-check, length guidance.
- `.claude/ask/person-template.md` — the locked output template (TL;DR /
  Signals / gates / Confirmed / Silent / Novel / Gaps / Interventions / Detail).

References to "translation rules" below mean `narrative-style.md`; the field
map + completeness gate are in THIS file.

**Modes:** default = full narrative (~600 lines). `brief` = condensed
(~150 lines). Trigger brief when user types `/ask brief <person>`,
`/ask <person> brief`, `/ask <person> short`, or otherwise signals they
want the headline read, not the audit-quality writeup.

**Brief output shape (only 3 sections, no Confirmed-by-data / Silent /
Novel / Gaps / Interventions blocks):**
1. **TL;DR** — 5-6 bullets, ≤25 words each.
2. **Signals** — 3 short paragraphs only: (a) what they shipped + how that
   reads against tier band, (b) how they worked (review/comment/slack
   shape + behavioral), (c) workstream summary in one paragraph.
3. **Detail** — 3 paragraphs. Primary workstream + secondary workstream +
   any standout pattern (ops shift, abandoned PRs, etc).

Brief is for "skim before 1:1" use. Full is for "write the actual
performance-review evidence pack". When unsure which one user wants, ask.

**Step 0 — build the DETERMINISTIC RENDER MANIFEST first (selection authority).**
`person_v4_manifest.py` runs `person_v3` + `person_deepread` internally and emits
ONE manifest that has ALREADY MADE every selection decision — which tickets /
docs / PRs / threads to cite, in which section, ranked and capped; the flags
(workload / risk / commit-without-PR); the caveats; the tier verdict; footprint
breadth + bus-factor; role-drift; key coordination threads. This removes
run-to-run variance: **the model PHRASES the manifest, it does not curate it.**

```bash
.venv/bin/python derive/person_v4_manifest.py --name "<canonical>" \
    --since "<iso>" --until "<iso>" > /tmp/<canonical>_manifest.json
```

Render rules from the manifest:
- `headline.tldr_facts[]` — one TL;DR bullet per fact, IN ORDER. Do not reorder,
  drop, or add facts.
- `sections.{shipped,designed,db_platform,ops,workstreams,own_prs}` — cite every
  item; do not add items not listed, do not drop listed items.
- `flags[]` — every flag MUST appear (TL;DR or the matching Signals/Gaps
  section). `workload_sentiment` is the regression this exists to prevent —
  never omit it.
- `caveats[]` — render each in the Caveats section.
- `footprint` (breadth + `bus_factor_candidates`) / `role_drift` (stepped-into vs
  stepped-back) / `review_concentration` / `key_threads` (release / oncall /
  coordination, by reply count) / `narrative_signals` (tier verdict, sprint
  cadence, owned-domain shares, ops count, MatterAI, pace) — render per the field
  map + translation rules below.
- `verify_manifest[]` — the gate's contract (verify gate below); not rendered.

Steps 1–2 below (`person_v3`, `person_deepread`) are the manifest's INTERNAL
inputs — do NOT run them separately for selection. Read their raw output only for
SUPPLEMENTARY citation quotes (a thread body, a jira-comment preview) the manifest
didn't bundle. The field map + completeness gate below still govern HOW to phrase
each signal; the manifest governs WHICH artefacts and locks selection + ordering.

**Step 1 — `person_v3` (manifest's primary engine; field reference for phrasing).**
It merges complete-recall discovery + signal taxonomy + workstreams + V1 rating,
with TRACK-ROUTING that fixes the feature-SP mis-rating of platform/ops engineers.

```bash
.venv/bin/python derive/person_v3.py --name "<canonical>" \
    --since "<iso>" --until "<iso>"        # add --format summary to eyeball
```

Returns: `coverage` (+`coverage_ok` — recall proof), `rating` (window_work_mix /
baseline_role_120d / feature_yardstick_applicable / v1_feature_verdict / note),
`workstreams` (led vs contributed, role-aware), `delivery` (shipped / fixed /
responded_to / designed / built — each split own vs contributed), `own_by_signal`,
`window_edge` (delivered-before-window, exclude), `v1_signals` (sprint cadence,
PR fate, behavioral, domain_ownership, gates).

**TRACK-ROUTED verdict (REQUIRED — this is the V3 fix):**
- `feature_yardstick_applicable = true` (window mix feature/mixed) → the V1
  feature tier verdict APPLIES; render it. For `mixed`, also surface workstream
  leadership the SP/PR lens misses (don't let SP-completion alone tell the story).
- `feature_yardstick_applicable = false` (platform/ops window) → DO NOT render the
  feature tier verdict as the headline. State the work-mix + baseline role, mark
  feature-SP NOT the yardstick, and evaluate on the delivery sections + workstreams.
- Always pair window_work_mix with baseline_role_120d: "platform this window,
  normally <baseline>" — distinguishes a genuine platform engineer (both platform)
  from a feature dev in a migration-heavy month (window platform, baseline feature).

**Narrative shape from V3:** lead Signals "What shipped" from `delivery.shipped`
(own first, then contributed); add a platform/ops "What shipped" sub-block when
the track is platform; build the Workstreams section from `workstreams` (Led =
AUTHOR/RESOLVER/DECIDER, Contributed = RESPONDER/participant — role is the
WINDOW role, reconciled with `project_footprint`). Cite measured impact from the
source thread (same rule as /retro — read the body, don't summarise the title).

**RENDER CONTRACT (REQUIRED — read every field from `person_v3`; do NOT
re-derive field names from the raw deepread JSON, and do NOT hand-run probes).**
`person_v3` emits every signal the narrative needs as a named field. Map each
narrative section to its field(s) and render ALL of them. This exists because a
prior run silently dropped the behavioral block (queried wrong keys) and missed
the review-concentration + role-drift signals — that must not recur.

| Narrative section | person_v3 field | Notes |
|---|---|---|
| TL;DR verdict framing | `rating.*` | window_work_mix + baseline_role_120d + feature_yardstick_applicable + note |
| Signals · How worked | `contribution.*`, `v1_signals.*`, `review_concentration` | engagement shape — cite `substantive_pr_commits` (commits-in-PR), `pr_reviews_total`, `confluence_edits`, `substantive_slack_replies`, `cross_surface_breadth`; the reviewer-of-record cluster. A high commits-in-PR / 0-own-PRs inversion is a real signal — name it. |
| Signals · When worked | `behavioral.*` | first_responder/resolver/p50/p90/after_hours/weekend/thread_followup — render verbatim; if null, say "n=`behavioral.samples`, not computed" |
| Signals · What shipped | `delivery.shipped/designed/built/fixed/ops` | own first, then contributed |
| Signals · Pace | `pace.*` | pr_cycle_median_days, slow>14d, same_day, shipped/abandoned/in_flight |
| Signals · Quality | `quality.*` | matterai p50, critical_flags, reverts, pr_count |
| Signals · Throughput | `completion.*` + `v1_signals.team_rank/team_sp_count/team_median_sp/team_top_sp/sp_attributed` | render BOTH primary + lookahead sp_completion; contextualize rank with median + top |
| Signals · Throughput (ops band) | `v1_signals.ops_track_deviation` + `verdict_suppressed_reason` | when `feature_yardstick_applicable=false` AND the engineer is CMR-heavy, render the OPS-band verdict as the headline instead of the (suppressed) feature verdict |
| Signals · Throughput (ticket fate) | `ticket_fate.resolved_in_lookahead` | tickets that closed just after window — pair with the completion-lookahead story |
| Workstreams | `workstreams[]` | led-first; window role |
| Role drift / handoffs | `role_drift[]` | dedup by `project_slug`; a lifetime→window role drop = possible handoff — STATE it as drift, do NOT assert "handed off to X" unless a thread confirms |
| Detail · review lead | `review_concentration` | name the cluster + the c/rev/commit split |
| Project footprint | `project_footprint[]` | top by window_event_count — but ALSO scan the full list and call out every `top_role_in_project == AUTHOR` slug regardless of rank (a low-event sole-ownership slug like counter-charge-engine must not be truncated away) |
| Gaps · risk PRs | `v1_signals.risk_flagged_prs` | list in Gaps if non-empty; skip the section line if empty |
| Caveats · attribution | `v1_signals.attribution_chain` | if `creation_fallback > changelog`, add a caveat that SP attribution is less certain this window |

**Completeness gate:** before writing the file, confirm every section above is
present. If a field is null/empty, render the section with an explicit "not
computed (n=…)" — NEVER silently omit a block. A narrative missing behavioral,
pace, quality, review_concentration, or role_drift is incomplete and must be
regenerated.

**Step 2 — `person_deepread.py` for CITATION MATERIAL.** V3 gives structure +
verdict; deepread bundles the raw quote-able material (PR titles, jira-comment
previews, slack-thread bodies, cluster members) for the Detail + Confirmed-by-data
sections. One bundle, chat reads, synthesises, writes markdown.

```bash
.venv/bin/python derive/person_deepread.py --name "<canonical>" \
    --since "<iso>" --until "<iso>"
```

Returns top-level keys: `profile`, `clusters`, `assigned_tickets`, `prs`,
`confluence`, `jira_comments`, `slack_threads`.

- `profile` — full `person_profile.compute_profile()` output (schema v3),
  with all the signals + fate + lookahead blocks documented below.
- `clusters[]` — top 10 clusters person touched, each with brief +
  `person_role` + `person_contrib_count` + top 5 members for citation
  material. **Use these to write the Detail workstream paragraphs.**
- `assigned_tickets[]` — every jira ticket assigned in window with
  issue_type / story_points / sprint / latest_status / title / creator.
  Use to cite specific tickets in Confirmed-by-data + Detail.
- `prs[]` — every PR person opened in window with title + opened_ts.
  Pair with `profile.fate.pr_fate[]` for shipped/abandoned status.
- `confluence[]` — every page event by person with title + body_bytes.
- `jira_comments[]` — top 20 jira comments BY LENGTH on others' tickets,
  with preview body. Use to surface investigation depth.
- `slack_threads[]` — top 20 thread_started ordered by reply count, with
  channel + reply count + body preview. High-reply threads = escalations.

Only run the older single-purpose scripts when you need a primitive that
`person_deepread.py` doesn't bundle (e.g. `ask_engine.py search` for topic
searches, `ask_engine.py window` for time-window attention queries).

The profile script emits a JSON contract (`schema_version=3`) with top-level
keys `person`, `tier`, `window`, `aliases`, `contribution`, `behavioral`,
`throughput`, `quality`, `narrative`, `fate`, `meta`. Render the Signals section
verbatim from those numbers — do NOT recompute. The script applies reliability
gates and sets `throughput.verdict.tier_deviation` to `null` with a
`verdict_suppressed_reason` when any gate fails; honour that — don't emit a
tier verdict when the script says it's suppressed. When `cmr_heavy_role` is
flagged, the script ALSO emits `throughput.verdict.ops_track_deviation`
against the ops band — use that instead of the feature-track verdict.

The `narrative` block (v2 addition — folds in the deleted /narrative skill's
signals via `derive/jira_metrics.py`):

- `narrative.team_rank` / `team_sp_count` / `team_median_sp` / `team_top_sp`
  / `sp_attributed` / `tickets_attributed` — assigned-and-shipped SP +
  team-relative ranking. Use in TL;DR ("ranks #N in team SP this window").
- `narrative.by_sprint[]` — `{sprint_name, sp, tickets, state}`. Use in
  Detail to show sprint-by-sprint cadence. Distinguishes steady-deliverer
  from end-of-window-spike.
- `narrative.attribution_chain` — `{changelog, creation_fallback, unknown}`.
  Surface as Caveats when `unknown > 0`. When `creation_fallback` dominates,
  attribution is less certain — flag.
- `narrative.domain_ownership[]` — per-project-domain PR-author share with
  labels OWNED (≥40%) / DROVE (≥25%) / CONTRIBUTED (≥1 PR <25%) / JIRA_ONLY
  (0 PRs). Use in TL;DR + Confirmed for crisp ownership claims.
- `narrative.ops_tickets[]` — title-regex hits against `OPS_PATTERNS` in
  `jira_metrics.py`. Surface as dedicated Ops & Incidents block in Detail
  if ≥3 hits; skip otherwise (don't pad).
- `narrative.risk_flagged_prs[]` — `subject_summary.risk_flags` on their
  PRs. Surface in Gaps if non-empty.

The `fate` block (v3 addition — window-edge bias correction):

- `fate.pr_fate[]` — every `pr_opened` in window with eventual terminal
  event (within `fate_max_days` of open). Per-PR `{status: shipped |
  abandoned | in_flight, terminal_event, terminal_ts, days_to_terminal,
  terminal_in_window: bool}`. Use this to read PR work HONESTLY —
  distinguishes shipped-in-window from shipped-but-merge-fell-after-window
  from abandoned-without-merge.
- `fate.pr_fate_summary` — counts: `{shipped, abandoned, in_flight,
  shipped_in_window, shipped_in_lookahead}`. If `shipped_in_lookahead > 0`,
  call out in TL;DR that domain_ownership undercounts at window edge.
- `fate.ticket_fate` — for tickets in_flight at `until`, diff their status
  at `until + lookahead_days`. Surfaces tickets that resolved just after
  window close. `{in_flight_at_until_total, resolved_in_lookahead[],
  shifted_to_shipped, shifted_to_cancelled}`.
- `fate.lookahead_throughput.feature_track.sp_completion_rate_pct` —
  primary sp_completion vs lookahead sp_completion. When primary reads
  below-band but lookahead reads in-band, the verdict is a window-edge
  artefact, NOT under-delivery. Render BOTH numbers in §Signals
  throughput paragraph: "61.1% primary → 80.7% with 30d lookahead".
- `fate.lookahead_domain_ownership[]` — domain_ownership recomputed on
  extended window. Tells you which JIRA_ONLY labels were window-edge
  artefacts vs genuine 0% code-author share.

**Hard rule:** when the lookahead delta is large (sp_completion shifts by
>15pp, or `shipped_in_lookahead > 0` for a person with `pr_count_in_window
< 5`), the primary verdict MUST be framed as "in this window" not as
"under-delivering". Surface the lookahead number in the same sentence.

**Pace signals — use PR cycle time, NOT ticket lead time.**

The team's workflow flips jira tickets To-Do → In Progress → Done in one
batch at close time. Ticket creation and Done timestamps frequently land
on the SAME DAY because tickets are recorded post-hoc. Result:
`fate.velocity.per_ticket[].lead_days` reads ~1 day for nearly everything
— useless as a pace signal.

PR opened/merged timestamps ARE real events (devs don't backdate PRs).
Use `fate.pr_fate_summary.pr_cycle_median_days` and
`fate.pr_fate_summary.slow_pr_count_over_14d` as the pace signal in
narrative. Surface as a "Pace" sub-section in §Signals:

- "Median PR cycle time was 3 days — fast"
- "Median PR cycle time was 22 days with 1 PR slow (>14d to merge) —
  long-running work this month, worth understanding"
- "Median 0 days, 5 same-day PRs — small PR pattern, lots of quick ships"

When `pr_cycle_median_days` is None (person opened no PRs in window),
say so plainly and note that ticket-level pace can't be inferred from
this team's workflow.

When you cite a slow PR in narrative, name the PR + the days_to_terminal
explicitly: "his April-20 get-API refactor took 22 days to merge".

Do NOT cite ticket-lead-time-in-days in narrative — it's almost always
1 because of the workflow quirk.

The cluster output is ordered by reply_count desc. Render the workstream
narrative as:
- Sectioned narrative (one section per cluster, not one per ticket)
- Bullet 1-2 highlight subjects per cluster (cite URL)
- Skip clusters with `status='RECURRING'` unless their reply_count is the dominant signal (then call out: "X spent 40% of incident time on opsgenie acks")

**Cluster status vs window_state — read the right field.**

Each cluster carries two state fields:

- `status` — current TODAY (`ACTIVE` / `STALE` / `RESOLVED` / `RECURRING`). LLM-labelled at last finalize_refresh. Reflects current state of cluster, NOT during the asked window.
- `window_state` — derived per-query, describes cluster lifetime ↔ window
  relationship. Values: `fully_in` / `started_in` / `ended_in` / `spans` /
  `pre_window` / `post_window` / `unknown`.

**For recent windows (last 30d):** use `status` for narrative framing
("active workstream", "stale workstream").

**For historical windows (>30d ago):** use `window_state`. A cluster
labelled STALE today may have been the team's primary focus during a
Feb retro window — `status` doesn't capture that, `window_state` does.
Render `spans` / `fully_in` as "active workstream during <window>",
`started_in` as "kicked off in <window>", `ended_in` as "wrapped up in
<window>".

Never frame a historical-window cluster as "STALE workstream" — that
mislabels the past based on present state.

**Cluster ownership filter — `home_team_owned_pct`.**

Every cluster from `ask_engine` (window / rootcauses / summarize) carries
`owner_distribution_json` (per-team share of member subjects) + derived
`home_team_owned_pct` (= home-team share). Use it to suppress
sister-team noise when the question is about the OWNER's team:

- Highs / deliveries framing → keep clusters with `home_team_owned_pct ≥ 0.70`.
- Lows / incidents framing → keep `≥ 0.30` (team co-owns more cross-team incidents).
- A cluster at <0.30 is mostly another team's work (PG webhooks, service-e failover,
  instant-pay-switch) — exclude or attribute to that team, don't claim as team's own.

Surfaced subject-level via `subject_summary.owned_by_primary` + `co_owners_json`.
Refreshed every `/rollup` apply + every `/refresh-embeddings` finalize apply.
Don't expose `home_team_owned_pct` as a number in prose — express scope via real
artefacts (tickets, PRs), per the no-cluster-language-in-narrative memory.

## Verify gate (MANDATORY for person_range — runs after the file is saved)

After writing the person_range file, run the deterministic verify gate. It
asserts every must-appear token in the manifest's `verify_manifest[]` — every
cited ticket / PR / page, every `flag:*`, every `caveat:*` — actually landed in
the prose. This makes "did all required facts land" deterministic even though
the wording is model-generated, and prevents the silent-drop class of bug (a
shipped ticket, a workload flag, a caveat going missing on a given run).

```bash
.venv/bin/python derive/verify_render.py \
    --manifest /tmp/<canonical>_manifest.json \
    --file management/narratives/per-person/<canonical>-<since>-to-<until>.md
```

- **Exit 0 / VERIFY PASS** → done. End the chat reply with `**Verify:** PASS
  (all N manifest items present)`.
- **Exit 1 / VERIFY FAIL** → the printed list names every manifest item missing
  from the prose. SURFACE the list, rewrite the narrative to include the missing
  items, and re-run the gate. Do NOT silently accept a fail. Loop until PASS — or,
  if an item genuinely cannot be placed, say so explicitly with the reason; never
  drop it silently.

This gate applies to `person_range` only (the manifest is person_range-scoped).
Other intents skip it.

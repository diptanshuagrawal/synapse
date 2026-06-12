Cross-source query router over the embedding + topic_brief pipeline. Routes natural-language questions to the right retrieval primitive in `derive/ask_engine.py`, then synthesizes a grounded answer with citations. Owner-invoked.

## Usage — `/ask <question>`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run any query.

**What it does:** Cross-source query router over the embedding + topic_brief pipeline (slack + jira + confluence + CMR + PRs + code). Classifies the question into ONE intent, dispatches to the matching retrieval primitive, then synthesizes a grounded, cited answer and saves it under `management/`.

**Intents it routes to:**
- `summarize` — what's happening with a topic / workstream.
- `person_range` — what a person shipped + how they worked over a date range (full or `brief`).
- `team_range` — whole-team narrative across all core ICs for a range.
- `attention` — what from the last N days needs the owner's attention (blockers, root causes).
- `ticket_gaps` — decisions/work that should have a Jira ticket but don't.
- `rootcauses` — categorise incidents/issues by root cause over a range.
- `dev_style` — a person's working style (delegates to `/dev-style`).
- `highs_lows` — stakeholder retro highs + lows (delegates to `/retro`).
- `feature_logic` — how a feature/computation works in code, reconciled with TRD/PRD + jira/slack (code graph).

**Usage:** `/ask <question>`
- `question` (required) — natural-language question across slack / jira / confluence / code.
- Modifiers: prefix `brief` (condensed person narrative) or `code` (engineer-facing feature_logic).

`$ARGUMENTS` is the question, e.g.:

```
/ask summarize everything done for instant-pay migration to service-a
/ask what did frank work on in march
/ask anything from yesterday that needs my attention
/ask any tasks for which jira ticket needs to be created
/ask go through issues in past month and categorise root causes
/ask working style of bob
/ask highs and lows of april
```

## Phase 1 — Classify intent

Parse the question. Pick exactly ONE intent. Resolve required parameters from the question; if missing, ask owner ONE clarifying question and stop.

| Intent          | Trigger phrases                                                   | Required params              | Route                         |
|---|---|---|---|
| **summarize**   | "summarize X", "what's happening with X", "what did we do for X"  | topic string                 | `ask_engine.py search`        |
| **person_range**| "what did <person> work on in <range>", "<person>'s work in <range>" | person, since, until         | `ask_engine.py person` + `person_profile.py` |
| **team_range**  | "what did the team do in <range>", "team narrative for <range>", "engineering retro narrative <range>" | since, until                 | loop `person_range` over `config/people.yaml` |
| **attention**   | "anything from yesterday / last N days I should care about", "what needs my attention" | since, until, optional me=owner | `ask_engine.py window`        |
| **ticket_gaps** | "tasks needing jira ticket", "untracked decisions", "unlinked work" | optional since/until         | `ask_engine.py gaps`          |
| **rootcauses**  | "root causes for X in past N days", "categorise issues in past month", "incident themes" | since, until                 | `ask_engine.py rootcauses`    |
| **dev_style**   | "working style of X", "how does X work", "X's response pattern"   | person                       | invoke `/dev-style <person>`  |
| **highs_lows**  | "highs and lows of <month>", "retro of <range>", "what went well/badly" | since, until                 | invoke `/retro <range>`       |
| **feature_logic** | "how is X aggregated / computed", "logic for Y in code", "how does the <feature> flow work", "where is X implemented" | feature/concept string, optional repo | service briefs → code-graph MCP + Confluence (see Phase 3 · feature_logic) |
| **event_metrics** | "how many times did X occur / fire", "count of X alerts in <range>", "how often did Y happen", "frequency of <alert/event> in <month>" | terms (keywords), since, until, optional channel/source | `ask_engine.py events` (see Phase 3 · event_metrics) |

If ambiguous between two intents (e.g. "what did bob work on AND his response style"), pick the structural one first (person_range) and offer to follow up with the other.

**`feature_logic` is the CODE intent — distinct from every other intent above.**
Every other intent answers from the *events* pipeline (slack/jira/CMR/confluence/PR
embeddings + clusters). `feature_logic` answers from the **code graph** (the actual
source of `service-a` + `service-c`). Route here when the question is about *how the code
works* — computation logic, data flow, where a thing is implemented — NOT about who
did what or what shipped. Disambiguation:
- "how is withholding aggregated" / "CGST/SGST charge logic" / "where is the ledger-balance
  computed" → **feature_logic** (asking about code behaviour).
- "what did the team do on withholding in May" / "ledger-balance issues this month" →
  **summarize** or **rootcauses** (asking about activity/incidents).
- Mixed ("why did ledger-balance break in May AND how is it computed") → answer the
  incident leg via `rootcauses`, then OPTIONALLY add a code-logic addendum from
  `feature_logic`. Keep the two legs visibly separate; do not blend tool jargon.

**`event_metrics` is the COUNT intent — distinct from `summarize` and `feature_logic`.**
Route here when the question wants a NUMBER or FREQUENCY over raw events — "how many
times did the ledger-balance mismatch alert fire in May", "count of instant-pay settlement
failures last week", "how often did X occur". This queries `events` directly, NOT
`topic_brief` clusters. Critical: automation channels (alert/recon/digest) are
EXCLUDED from clustering (see `derive/cluster_noise_filter.py`), so they are absent
from `summarize`/cluster routes — `event_metrics` is the ONLY route that sees them.
- "how many ledger-balance mismatches in May" → **event_metrics** (a count).
- "what's the ledger-balance reconciliation workstream" → **summarize** (a narrative).
- "where is the ledger-balance computed" → **feature_logic** (code).

**Isolation rule (design decision):** `feature_logic` is self-contained. It MUST NOT
alter `/retro` (stakeholder voice bans code/tech jargon) or `person_range` (real-artefact
framing; the code graph is author-agnostic and can't improve attribution). The only
sanctioned cross-over is `rootcauses`/incident queries reaching the code graph on-demand
for impact-radius / affected-flows — never automatically.

## Phase 2 — Resolve params

### Date range parsing

Today is provided by the cron-status SessionStart hook output as `currentDate`. Use IST relative dates:

- "yesterday"   → since = today-1d 00:00 IST, until = today 00:00 IST
- "last N days" → since = today-N 00:00 IST, until = now
- "march"       → since = current-year-03-01, until = current-year-04-01
- "april"       → since = current-year-04-01, until = current-year-05-01
- "past month"  → since = today-30d, until = today

Emit as ISO8601 (`YYYY-MM-DDTHH:MM:SSZ`). The engine compares against `events.ts` (already ISO).

### Person resolution

Match substring case-insensitive against `canonical` field in `config/people.yaml`. If multiple matches surface, list them and ask which.

## Phase 3 — Dispatch

```bash
cd $HOME/context/work-context
```

Run the matching primitive. Pipe output through Python for clean JSON parsing.

### summarize

```bash
.venv/bin/python derive/ask_engine.py search --query "<topic>" --k 30
```

Read JSON. Pick top 1–3 clusters by `hit_count`. Each cluster carries:
- `label`, `status` — for the section header
- `topic_brief.decisions_json` — bullet the decisions
- `topic_brief.blockers_json` — bullet the blockers (call out if `status='ACTIVE'`)
- `topic_brief.root_cause` — surface if non-null
- `topic_brief.participants_json` — top 3 contributors by `contribution_count`
- `top_subjects[]` — cite 3-5 with clickable URLs (use the URL conventions in `derive/validate_embeddings.py:subject_url`)

### person_range

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
- `verify_manifest[]` — the gate's contract (Phase 5); not rendered.

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

### team_range

Whole-team narrative. Loop `person_range` over every entry in
`config/people.yaml::people` whose `canonical` is in the core team (Tier-0
ICs — those with a `role` field set, e.g. SDE1/SDE2/SDE3). For each person:

1. Run `derive/person_profile.py --name <canonical> --since X --until Y` and
   capture JSON.
2. Run `derive/ask_engine.py person --name <canonical> --since X --until Y`
   for cluster material.
3. Render a per-person section using the same person_range template
   (TL;DR + Signals + Confirmed + Data silent on + Novel + Gaps +
   Interventions + Detail) — but trimmed: TL;DR + Signals + 1-paragraph
   Detail.

Prefix the file with a **Team overview** paragraph (3-5 sentences) and a
**Team velocity table** (per-person `sp_attributed`, tickets_shipped,
tier_deviation verdict). Section order: people with strongest signals
(OWNED ≥1 domain + above-band) appear first; within tier, sort by
event volume descending.

Output: `management/narratives/team/<since>-to-<until>.md`.

### attention

```bash
.venv/bin/python derive/ask_engine.py window --since "<iso>" --until "<iso>" --participant "owner"
```

Filter the response: clusters with `status='ACTIVE'` AND (blockers_json non-empty OR root_cause non-null). For each:
- "Cluster {label}: {N} blockers — {1-line top blocker}"
- "Cluster {label}: root_cause = {...} — last touched {ts}"
Cap output at 10 items. Sort by `last_activity_ts` desc.

### ticket_gaps

```bash
.venv/bin/python derive/ask_engine.py gaps --threshold 0.65
```

Add `--since`/`--until` only if owner specified a range. For each gap:
- "{slack URL}  →  cluster '{label}'  →  evidence: '{evidence_text}'"
- If `nearest_jira` is non-null AND similarity ≥ 0.55 (sub-threshold but interesting): "candidate dup: {jira} (sim={...})"

### rootcauses

```bash
.venv/bin/python derive/ask_engine.py rootcauses --since "<iso>" --until "<iso>"
```

Render as a categorised table:
- group by source domain heuristically from cluster.label (DB / Performance / Migration / etc.)
- per row: cluster_id | label | root_cause | member_count | last_activity_ts

### dev_style

Invoke the existing skill (do NOT reimplement):

```
/dev-style <person>
```

If the question also asks for a comparison ("Alice vs Bob"), run `/dev-style` twice and surface the deltas afterward.

### highs_lows

Invoke the existing skill:

```
/retro since=<iso> until=<iso>
```

`/retro` produces a STAKEHOLDER-FACING document — team-level voice, no dev names, ONLY actual production deliveries in Highs, real impact numbers from slack threads. Format matches the owner's prior monthly stakeholder updates (Feb + March precedent in `#example-monthly-update` channel).

Key rules `/retro` enforces:
- Team-level voice only. "The team delivered X" — never "Alice shipped Y" or "Bob owned Z".
- Highs = production deliveries only. Code merged / ticket-Done WITHOUT user-facing rollout = NOT a high. It belongs in Lows as "X dev complete, rollout slipping to <date>".
- Every high needs measurable impact (RPS, latency, success rate, accounts onboarded, downtime saved, cost reduction). Pull from team's own slack rollout-update posts.
- No internal jargon. No PR/ticket/cluster references. No IC-level metrics.

### feature_logic

**This intent answers "how does X work / what is Y / where is Z computed" from ALL
SOURCES — service briefs, jira, confluence, slack, AND code — never code alone.**
The service brief is the *map* (which service owns the concept, what tables, what
the terms mean); code tells you *how it currently runs*; the TRD/PRD on Confluence
tells you *what it's meant to do and why*; jira/slack tell you *what changed and
what broke*. A code-only answer
is partial and can be actively wrong — it can describe the wrong subsystem, or miss
that a doc-specified behaviour differs from what shipped. (Validated in practice: a
code-only "how does withholding work" answer described deposit-interest withholding and claimed it was
*async* to the transaction; the cash-withholding TRD revealed a *second* withholding
system — Section NNN cash-withdrawal withholding — that is deliberately *synchronous*. The
docs corrected both the scope and the core claim.)

**Step 1 — GATHER ALL SOURCES (mandatory, in parallel).** Fire these together:

- **Service briefs (the ROUTING layer — read FIRST).** Per-service plain-English
  maps built by `/service-brief` (deterministic Go extractor + chat semantics),
  stored as `source='service'` rows in `events.db` — embedded for retrieval but
  NOT clustered, so they never show up as a topic cluster. They answer "*which
  service owns this concept, what tables hold it, what do the domain words mean*"
  BEFORE you touch the code graph. A raw concept word ("posting", "charges") often
  buries the real subsystem under recurring-alert noise in the events search — the
  brief cuts straight to the owning service. Use them as the map, then drill.
  - List what exists: `SELECT DISTINCT subject FROM events WHERE source='service'`
    (via `from ingest.common import get_db`). Subjects are
    `service:<svc>#responsibility | #glossary | #data-model | #endpoints/<svc> | #kafka`.
  - Pull the relevant sections for the concept:
    `SELECT title, body FROM events WHERE source='service' AND subject LIKE 'service:<svc>#%'`.
    Always read `#responsibility` + `#glossary` + `#data-model` for the candidate
    service(s); add the matching `#endpoints/...` section.
  - **The `#glossary` section seeds WIKI §Key terms directly** — it is already
    plain-English domain definitions, per service.
  - Coverage is partial (Go services only; v1 = `service-b`, `service-a`). If no
    brief exists for the owning service (notably **service-c** has none yet), say so and
    fall back to the code graph + Confluence — don't pretend the map is complete.
  - A brief is a MAP, not the territory: it's fresher than a TRD (derived from
    current code) but COARSER than the graph. It tells you *where* and *what owns
    what*; it does NOT prove behaviour. Verify the actual computation in code
    (Step 2). Briefs also stop at the service edge — a flow that hands off to a
    downstream service's Kafka consumer won't be traced by the source service's
    brief; follow it into the consumer's brief/code (validated in practice: the
    posting brief mapped `staging → posted` but the final ledger commit lives in a
    downstream consumer the brief didn't trace).
- **Events pipeline** — `ask_engine.py search --query "<concept>" --k 25` from
  `work-context`. Returns clusters spanning jira/CMR/confluence/slack/PR. Note the
  cluster labels (they reveal adjacent subsystems — e.g. a "withholding" search surfaces
  both interest-withholding and cash-withdrawal-withholding clusters). Pull member subjects to cite
  and to read bodies. **Service-brief hits land in the `<unaffiliated>` bucket**
  (`cluster_id=None`, `topic_brief=None`) since briefs aren't clustered — treat
  those rows as authoritative service-structure context, not as a weak cluster;
  better to pull the brief directly via the SQL above than rely on this bucket.
  For a pure domain word, expect this search to surface mostly operational/alert
  noise (e.g. "posting" → recurring `CLEARING_OUTWARD_POSTING` oncall alerts) —
  that's the *operational* view, useful for §Why/ops notes, not the logic.
- **Confluence docs (direct)** — Atlassian MCP. `searchConfluenceUsingCql` with
  `text ~ "<concept>" AND type = page ORDER BY lastmodified DESC` (cloudId
  `YOUR_CONFLUENCE_CLOUD_ID`, site `your-org.atlassian.net`). Hunt for
  TRD / PRD / "API Contract" / design pages by title. `getConfluencePage` (markdown)
  for the full body of the 1-2 most relevant. **The TRD/PRD is the primary source
  for "what it is" + "why".**
- **Code graph** — `mcp__code-review-graph__semantic_search_nodes_tool` (keyword/
  hybrid; concept terms — single words land better than phrases) over `service-c` then
  `service-a`. `query_graph_tool` for callers/callees once a symbol is known.

If a source returns nothing, say so — don't silently answer from the others alone.
If code spans an unregistered repo (`deposits-orch` on disk, not registered), flag it.

**Step 2 — READ the primaries.** Read the TRD/PRD body (Confluence) AND the 2-4
functions that carry the logic (`get_minimal_context` / direct Read + grep).
**Never infer behaviour from a name or a doc title — read the actual body / code.**

**Step 3 — RECONCILE across sources, with a PRECEDENCE RULE.** Sources answer
different questions and have different trust for different claims. When they
conflict, resolve by *what kind of claim* it is:

- **"What does it do NOW / current behaviour / does X happen" → CODE WINS.** The
  TRD/PRD states *intent*, and intent goes stale: docs are written before code, get
  partially implemented, or drift as the code changes. Never assert current
  behaviour from a doc alone — confirm it in code. If the doc says one thing and the
  code does another, **the code is the true state**; say so plainly and note the doc
  is out of date (or describes an unshipped/changed design).
  - **Freshness scope:** the code graph is built from dedicated mirror clones pinned
    to each repo's REMOTE default branch (`main` for service-a, `master` for service-c),
    refreshed daily. So "code wins" means *merged* code wins — the graph does NOT see
    un-pushed or uncommitted local work or unmerged feature branches. If an answer
    seems to contradict in-progress local work, say it reflects merged `main`/`master`,
    not local WIP.
- **"Why / what was intended / what's the tradeoff" → TRD/PRD + jira/slack WIN.**
  Code can't tell you *why*; the design doc and the discussion can. Rationale claims
  lean on docs. If NO design doc surfaces (common for plumbing like posting), the
  "why" can only be *inferred* from code structure + inline comments — label it as
  inferred, don't present it as documented intent.
- **"Where does it live / which service / what tables / what's this term" →
  SERVICE BRIEF wins as the map**, then confirm in code. The brief is derived from
  current code so its structure claims are reliable, but it is COARSER than the
  graph and never proves behaviour — use it to locate, use code to verify. When the
  brief and code disagree on structure, the brief is stale; re-run `/service-brief`.
- **Distinguish stale-doc from not-yet-built.** If the code lacks something the TRD
  describes, it's either (a) shipped-differently → code is truth, doc is stale; or
  (b) not-built-yet → the feature is aspirational. Decide which using jira/slack
  status ("TRD in progress", "rollout branch-by-branch", open tickets) — and SAY
  which case it is. Absence in code ≠ "doc is wrong" without checking the status.
- **Verify behaviour, don't just confirm names exist.** Finding a function/table the
  doc names proves the *structure* exists, NOT that it *behaves* as the doc claims.
  For any load-bearing behavioural claim (sync vs async, idempotent, free-tier
  logic, the actual formula), read the function body — don't infer behaviour from a
  matching name or trust the doc's sequence diagram.
- **Multiple subsystems under one term:** if the search surfaced two features sharing
  a name (interest-withholding vs cash-withholding), disambiguate up front and answer the one asked.

In the wiki answer's §Sources, state the verdict for any conflict: "code is the
current truth; the TRD still says X (out of date)" or "doc describes Y, not yet in
code — planned, see <ticket>".

**Output voice — WIKI by default.** `feature_logic` answers in plain-language,
encyclopedia style — the reader is someone new to this feature (a new joiner, a PM,
an engineer from another team), NOT the author. They should understand the feature
WITHOUT reading code. Code is the *footnote that proves the explanation*, never the
headline. This is the inverse of every other intent's "show the artefact" rule:
here, lead with the concept, bury the `file:line`.

The owner may signal the engineer-facing variant with `/ask code <question>`,
"show me the code", "where exactly", "trace the flow". Then flip to the CODE shape
below. Default (any "how does X work / what is Y / explain Z") = WIKI shape.

**WIKI shape (default — Wikipedia-style):**
1. **What it is** — 1 short paragraph, pure plain English. No code, no file names,
   no jargon. A smart non-expert should get it in one read. Lead with the human
   purpose ("withholding is the tax the bank withholds on the interest it pays you").
2. **How it works** — the flow told as a STORY, in order, plain English. "When the
   bank runs its interest cycle, it credits interest, then withholds a slice as tax,
   then records both." Number the steps. NO code, NO SQL, NO `file:line` here.
3. **Key terms** — a 3-6 item glossary of any domain acronym/term used (withholding, ABB,
   EOD/BOD, GL, CMR). One plain sentence each. This is what makes it newcomer-safe.
   **Seed this from the service brief's `#glossary` section** when one exists — it's
   already per-service plain-English definitions. Skip a term if it's already obvious
   from context; never pad.
4. **Why it's built this way** — the design rationale / tradeoff, if discoverable.
   Pull from the jira/CMR/confluence/slack discussion (one `ask_engine search`).
   Often the interesting part ("they moved withholding off the live transaction because it
   was slowing the app"). Skip if no rationale surfaces — don't invent one. If the
   rationale is only *inferable from code* (no design doc surfaced), say so — "read
   off the code's structure, not a design doc" — don't dress inference as intent.
5. **Sources & under the hood** (LAST, the footnote) — the evidence trail. Lead with
   the authoritative docs (TRD/PRD Confluence links + key jira/CMR tickets); if none
   exist, SAY so ("no posting TRD surfaced — code is the only authority"). Next give
   the **service map** (the brief's responsibility + table chain, e.g.
   `order_request → transaction_staging → transaction`). THEN the code: `file:line`
   citations, the key SQL/formula as a short code block, the entry-point symbol.
   Note any doc-vs-code drift found in Step 3, and any service-edge boundary the
   brief didn't cross (e.g. "the final ledger commit is in a downstream consumer").
   This section is for the reader who wants to verify or go deeper; everything above
   stands on its own without it.

Hard rules for WIKI voice:
- No tool/code jargon above §5. No "stored proc", "repository", "JDBC", "ResultSet",
  "cluster" in the explanation — translate ("a database routine", "the code that
  reads the table"). Acronyms get defined in §Key terms before first use.
- Short sentences. Concrete nouns. Analogies where they help a newcomer.
- Honest about boundaries: if the rate/logic lives in a DB proc or an unregistered
  repo, SAY so in plain English ("the exact tax rate is set in a database routine we
  don't have indexed here"), don't hand-wave.
- If the question is broad ("how does the charge system work"), it's fine to answer
  at the feature/concept level and offer to drill into a sub-part — don't dump every
  file.

**CODE shape (only when owner asks for code / "where exactly" / "trace"):**
1. **TL;DR** — 3-5 bullets: what the logic does, where it lives (file + symbol),
   key inputs/outputs.
2. **How it works** — numbered prose walk of the flow: entry point → each transform
   → output. Cite `file_path:line` (clickable) at each step. Show the 1-2 lines that
   carry the actual computation as a short code block.
3. **Where it's wired** — callers + callees / dependencies, from `query_graph`.
4. **Decision context** (if discussion found) — thread/ticket/doc as inline links.

Cite code with `repo/path/to/file.ext:line` (link to the file when a URL convention
exists; otherwise plain path is fine — these are local repos). NEVER fabricate a
line number — if the read didn't return it, cite the symbol name only.

### event_metrics

A deterministic COUNT/FREQUENCY over `events` — the route for "how many times did X
occur in <range>". Reaches automation channels that are excluded from clustering, so
it is the ONLY intent that can answer alert-frequency questions.

1. From the question, extract **keyword terms** (the thing being counted) and the
   **window** (Phase 2 resolves since/until). Optionally narrow with `--channel`
   (a named channel) and/or `--source slack` to drop unrelated code/PR matches.
2. Run:

   ```bash
   .venv/bin/python derive/ask_engine.py events \
       --terms "trial balance" mismatch --any \
       --since <ISO> --until <ISO> [--source slack] [--channel <name>]
   ```

   - Default AND-matches all terms; pass `--any` for OR (e.g. synonyms).
   - `--source slack` excludes GitHub/Jira text hits (a PR mentioning "mismatch" is
     not an alert firing). Use it for alert-frequency questions.
3. Render from the JSON:
   - Headline number: `total` occurrences over `distinct_days` days.
   - **per_channel** breakdown — name the channels (e.g. `cbs_accounting_alerts`),
     since one logical alert often fans across channels. Drop `channel: null` rows
     (non-slack) unless the question is cross-source.
   - **per_day** spikes — call out the peak day(s) if the distribution is lumpy.
   - Cite 2-3 `sample_citations` (ts + channel + snippet) as evidence.
4. Honesty: this counts MESSAGE occurrences matching the terms, not deduplicated
   incidents — say so ("179 alert messages across 22 days; a single incident can
   emit several"). If the terms are ambiguous, state which terms you matched and
   offer to narrow (specific channel, AND vs OR, dedupe by thread).

This intent does NOT save cluster/embedding artefacts — it is a direct query. Still
save the answer file per Phase 5.

## Phase 4 — Synthesise + cite (NARRATIVE style — read this carefully)

### Output style: narrative grounded in contribution-centric signals

The reader is a manager who knows the team but does NOT know this tool's
internals. They should never see cluster IDs, "reply_count=17", or bare
bullet lists of EX-XXXX URLs. They should read meaningful prose that
extracts the *actual decisions, rollout dates, percentages, post-rollout
impact, who decided what* from the underlying threads/tickets/PRs.

This is the bar. Treat AI summaries in Linear / Notion / Slack-AI as the
reference quality. Lower-quality output (cluster-ID dumps, label-only
bullets, "AUTHOR/REVIEWER" jargon) means you have NOT done the synthesis
step — go back and read the JSON deeper.

### Contribution ≠ Authorship — critical distinction

**Who CREATED a ticket / opened a thread / opened a PR is a weak signal of
actual work.** Most consequential work shows up as:

- Substantive comments on others' tickets (length > 300 chars, contains
  decision content or rectification SQL or operational steps)
- Commits inside others' PRs (commit_in_pr where actor is not the PR opener)
- Page-updated events on Confluence (delta bytes > some threshold) — not
  just page_created
- Inline comments on others' pages
- Substantive Slack replies (length > 200 chars, contains decisions or
  approvals or post-rollout observations)
- State transitions triggered on others' tickets

When framing impact, lead with **substantive contribution**, not ticket
authorship. A heavy substantive responder on a multi-decision Epic is
often more impactful than the person who created the placeholder ticket.

### Mandatory deep-read step

Before writing the answer, for the top 5-8 clusters / subjects by relevance:
1. Pull the underlying member content from `events.db` — issue bodies,
   thread bodies, PR descriptions, MatterAI summaries, page bodies. Do
   NOT rely only on the cluster `label` + `decisions_json`. Those are
   summaries of summaries; the meaning lives in the raw bodies.
2. Extract concrete facts: numbers, percentages, dates, named people who
   approved/decided/rolled back, post-rollout observations, specific
   ticket actions ("disabled X job", "deployed beta-only PR #2268",
   "rectified ₹X TB diff on account NNNN").
3. Connect dots across sources — if a Confluence page documents the
   decision, a Slack thread asks for approval, and a Jira ticket
   executes it, weave them into ONE storyline ("decision made on
   Confluence X, approval requested in #channel Y on date Z, executed
   via ticket EX-NNNN").

### Output shape (sections — order matters)

The reader is a manager who knows the team but does NOT know this tool's
internals. They never see "cluster 56", "lookahead window", "boundary
artefact", "p50 latency", "substantive_pr_reviews" — those are tool-side
terms. Translate every signal into plain English BEFORE it lands in prose.

Section order (locked):

1. TL;DR — 5-6 bullets, ≤25 words each. Most consequential first.
2. Signals — 5-7 sub-sections (one per group: How he worked / When he
   worked / What shipped / Pace + velocity / Ops track / Workstreams /
   Quality). Each sub-section opens with a **bold lead phrase** then
   2-5 bullets. NO wall-of-text paragraphs — bullet every distinct
   observation. NO metric dumps, NO calculation breakdowns, NO
   internal terms.
3. Data silent on — what the data can't say. Plain English.
4. Novel observations — emergent patterns specific to this person/window.
5. Gaps / coaching opportunities — patterns to discuss in 1:1.
6. Interventions to consider (person_range only) — L1/L2/L3 menu.
7. Detail — multi-paragraph narrative, themes not clusters. Inline links.
8. Confirmed by data — last section. Bullet list with citations. This is
   the audit trail for the manager who wants to verify a specific claim.

Hard rules for natural-language translation:

- **NO internal entities or pipeline names ANYWHERE in the output (rule
  zero — applies to every section, audit included).** The reader sees a
  manager's narrative grounded in real, verifiable artefacts — never the
  machinery that produced it. Forbidden everywhere: cluster IDs / the word
  "cluster", engine/script/table names (`person_v3`, `person_deepread`,
  `ask_engine`, `topic_brief`, `events.db`, `jira_metrics`), JSON field
  paths or signal keys (`v1_signals`, `project_footprint`, `role_drift`,
  `domain_ownership`, `delivery.shipped`, `substantive_jira_comments`,
  `sp_completion_rate`, `attribution_chain`, anything with `::`, `_json`,
  `.primary`, `window_role` / `lifetime_role` as literal tokens), and
  pipeline jargon ("lookahead", "window edge", "boundary artefact",
  "sandbox"). Every claim — including in "Confirmed by data" — cites the
  artefact a human can open: a ticket ID (EX-NNNN), a PR (`owner/repo#N`),
  a doc title, a Slack thread link, or a plain-English description of the
  activity ("left long investigative comments on the Year-End-Job tickets").
  If you cannot express a fact without naming the engine, the fact does not
  belong in the output.

- **Never reference cluster IDs.** "Cluster 56" is not a thing the reader
  knows. Name the workstream instead: "the service-a balance-service and withholding
  workstream". Translate `participants_json::role` (AUTHOR / REVIEWER /
  RESPONDER) into plain English: "owns it" / "is the main reviewer" /
  "shows up in the discussion".

- **Prefer project-level voice (project_footprint block).** When the
  output has a `project_footprint` block (per-person) or
  `projects_active_in_window` rollup (team), render workstreams via
  project slug names — NOT cluster lists. Bad: "active on clusters 281,
  297, 352 + 8 others". Good: "drove service-c Revamp + instant-pay on service-a rollout +
  accounting refactor". The slug names in `projects.yaml::name` are
  human-readable; use those. Cluster IDs stay invisible to the reader.
  Map `top_role_in_project` to plain English: AUTHOR → "drove",
  DECIDER → "called the shots on", RESOLVER → "drove resolution of",
  REVIEWER → "reviewed", RESPONDER → "weighed in on".

- **Never render `cluster_count` / `window_event_count` /
  `member_count_total` as numbers in prose.** These are sandbox-only
  signals — used to DECIDE which workstreams to highlight, never shown to
  the reader. The reader does not know what an HDBSCAN cluster is and
  should never need to. Express scope via **real artefacts the reader can
  verify**: tickets shipped, PRs opened/merged/reviewed, comments left,
  Confluence edits, slack replies (from the `contribution` block +
  `assigned_tickets[]` / `prs[]` / `confluence[]` arrays).
  - Bad: "drove 11 workstreams under accounting-refactor", "touched 3
    clusters across 11 events", "1 cluster, 95 events on TD/withholding", "24
    distinct projects touched".
  - Good: "owned the accounting-refactor track — shipped 7 tickets and
    reviewed 12 PRs", "heavy reviewer on the TD/withholding stream — 81 long-form
    comments across 8 reviews".
  - Cluster is the engine; workstream is the surface. Cluster-derived
    counts and cluster IDs appear NOWHERE in the output — not in TL;DR,
    Signals, Novel, Gaps, Detail, AND NOT in the "Confirmed by data" audit
    section either. There is no audit-section exception. The reader never
    sees the engine.

  **Use window_role, NEVER lifetime_role.** Each
  `project_footprint::clusters[]` entry carries both `window_role`
  (derived from events in the asked window) and `lifetime_role` (from
  topic_brief.participants_json — cumulative across all time). For
  "what did X work on in <window>" questions, render against
  `window_role` always. `top_role_in_project` is already derived from
  window_role server-side; trust it.

  `lifetime_role` is for context only ("he's been AUTHOR on this
  workstream historically but only RESPONDER this window"). Surface
  lifetime_role explicitly only when owner asks about historical
  context or when `role_drift_cluster_count > 0` on a slug is
  interesting enough to call out (e.g. "AUTHOR historically, mostly
  reviewer this month — engagement shape shifted").

  **Judge window engagement by the person's events IN the window, not
  lifetime cluster size — but as sandbox reasoning only, never as rendered
  numbers.** A workstream with 70 lifetime members but only 3 of the
  person's events this window is light-touch: use that to decide emphasis
  (brief mention, or omit), then express it in artefact terms — "touched
  accounting-refactor lightly — one ticket, a couple of review comments" —
  NOT "3 clusters across 11 events". `window_event_count` and
  `cluster_count` inform the WHAT-to-write decision; they never appear in
  WHAT gets written (per the rule above).

- **Never say "lookahead" / "window edge" / "boundary artefact" / "primary
  vs companion window".** Translate to time: "looked one month later",
  "his April work mostly finished in May", "by end of May, those tickets
  had shipped". The reader should understand without learning tool jargon.

- **Never paste raw metrics with calculations.** Bad: "sp_completion 61.1%
  = 33.4 shipped / (33.4 + 19.25 in-flight + 2.0 cancelled)". Good:
  "shipped about two-thirds of his planned story points; the remaining
  third was still in-progress at month-end and most of it shipped in
  early May".

- **Behavioral signals translate to plain English.** Bad: "p50 first-reply
  latency 67.9 minutes, p90 5941 minutes". Good: "usually replies on
  slack within an hour, but a handful of asks went unanswered for several
  days". Bad: "after_hours_share 25.7%". Good: "about a quarter of his
  activity falls outside the team's 12-8 window — lowest of his peers".

- **Distinguish review VOLUME from review DEPTH.** The
  `substantive_pr_reviews` metric counts only long-form review prose
  (body > 200 chars). When that metric reads 0, ALSO check raw count of
  any-length reviews. Many ICs review heavily but with short "LGTM" or
  "Requested changes" comments. Frame as: "reviewed N PRs but always
  with short approves — never long-form" rather than "0 reviews".

- **Reviews are activity, NOT deliverables.** The narrative body (What
  shipped / Detail) covers only work the person AUTHORED or DROVE — PRs
  they wrote, tickets they drove to Done, docs they authored, ops actions
  they executed. Review activity ("156 reviews given") is a count — it
  lives in the activity-stats table / "How he worked" signal, never
  itemised as an accomplishment in the narrative. Do not list things they
  reviewed as their output.

- **MatterAI flags — only on the person's OWN PRs.** Critical/quality
  flags from PRs the person authored are narrative-relevant (risk in their
  output). Flags on PRs they merely reviewed belong to the AUTHOR's
  summary, not theirs — never attribute a reviewed-PR flag to the reviewer.

- **`status → Done` is NOT proof of work.** A dev running standup
  transitions tickets they never coded. For delivery / SP / ownership
  claims, credit by **assigned-at-close** (latest assignee before the Done
  event) — `jira_metrics.py` owns this computation; consume it, do not
  reimplement. Transitioner count is a separate clerical signal: render as
  "moved N tickets to Done (any assignee)", never "delivered N tickets".
  Domain ownership = PR-author share in that domain, not Done-transition count.

- **Section structure: one sub-section per domain / workstream**, each a
  bold lead phrase + bullets of specific items (subject ID + title + brief
  context). Tables are for raw activity counts ONLY — never lead with, or
  collapse the narrative into, a domain matrix.

- **Name the tier band naturally.** Bad: "sp_completion 61.1% reads below
  SDE2 band 70-80% per tier_expectations.yaml". Good: "shipped about
  two-thirds — under the SDE2 norm of three-quarters to four-fifths".

- **Sentences short.** 8-14 words. Drop hedging. Drop "intentionally" /
  "honestly" / "frankly" / "literally". Just say it.

- **Pre-save grep-check (mandatory).** Before writing the file, scan the
  ENTIRE rendered output — every section INCLUDING "Confirmed by data"
  (there is no exempt section) — for these forbidden strings and rewrite
  any line that contains them in plain English / artefact terms:
  ```
  cluster   clusters   cluster_count   cluster_id   window_event   member_count
  events?   lookahead   boundary   sandbox   _pct   sp_completion   p50   p90
  ::   _json   .primary   window_role   lifetime_role   role_drift
  v1_signals   project_footprint   domain_ownership   attribution_chain
  person_v3   person_deepread   ask_engine   topic_brief   events.db   jira_metrics
  substantive_   matterai   (plain English)   (analyzed)   (objective baseline)
  ```
  Any engine/script/table name, JSON field path, signal key, cluster
  reference, `_pct`/`p50` metric, or `(…)` heading suffix anywhere in the
  file = rewrite. A "Confirmed by data" bullet must read as "claim — real
  artefacts (EX-NNNN, repo#N, doc title, slack link) + plain-English
  activity", never as "claim — `field.path` = value". Any §Signals
  paragraph over ~4 sentences → break into bullets.

```
**Answer to: {question}**

## TL;DR

  • 5-6 bullets, ≤25 words each
  • Most consequential action/finding first
  • Each bullet traces to either Signals or Novel observations

## Signals (analyzed)

CONTRIBUTION SIGNALS (work signals — who did stuff)

  authorship                      N    (pr_opened + issue_created + thread_started)
  substantive_pr_commits          N    (commit_in_pr events authored by person)
  substantive_pr_reviews          N    (review comments > 200 chars)
  substantive_jira_comments       N    (comments > 300 chars on others' tickets)
  jira_state_transitions          N    (status_change events triggered by person)
  confluence_edits                N bytes / M events (page_updated by person)
  confluence_inline_comments      N    (on others' pages)
  substantive_slack_replies       N    (replies > 200 chars)
  coordination_spans              N    (own thread/ticket drawing ≥3 distinct replies)
  cited_by_others                 N    (raw/canonical id mentioned in others' content)
  resolver_phrases                N    (explicit "resolved/fixed/deployed/merged by X")
  cross_surface_breadth           N/4  (sources where ≥10 substantive events)
  active_workstreams              N    (clusters touched with status=ACTIVE)
  recurring_share                 X%   (events in RECURRING clusters — deflate if > 30%)

BEHAVIORAL SIGNALS (how / when they engage — slack-derived)

  first_responder_rate            X%   (% of threads-they-replied-to where they
                                        replied first, from actor_behavior_report)
  resolver_rate                   X%   (% of threads where they posted resolution
                                        marker, from actor_behavior_report)
  p50_response_latency            Xm   (median minutes between thread_started and
                                        their first reply — read from actor_behavior_report
                                        OR compute from events.db)
  p90_response_latency            Xm   (worst-case lag — flags if >> p50)
  after_hours_share               X%   (% of events outside 9am-7pm IST —
                                        workload-health signal)
  weekend_share                   X%   (% of events on Saturday/Sunday)
  thread_followup_rate            X%   (% of own thread_started where they
                                        posted ≥1 reply themselves — drop-off signal)
  question_vs_answer_ratio        Q:A  (rough: # replies ending in '?' vs # not)

THROUGHPUT SIGNALS (jira sprint productivity — from events.db schema fields)

  Read `config/tier_expectations.yaml::status_classes` for terminal-state
  classification. Resolve a ticket's current status via:
    SELECT to_status FROM events WHERE subject=? AND to_status IS NOT NULL
    ORDER BY ts DESC LIMIT 1

  Split into shipped (feature delivery), ops_closed (CMR/ops closure),
  cancelled, and in_flight. ICs with high ops_closed share get their
  velocity judged on the ops track, NOT the feature track.

FEATURE TRACK (SP-pointed work)

  story_points_committed          N    (SUM of story_points on tickets
                                        assignee=person AND ever-sprinted
                                        AND status not in cancelled)
  story_points_shipped            N    (same, but status in shipped class)
  sp_completion_rate              X%   (shipped / (shipped + in_flight) —
                                        EXCLUDES cancelled + ops_closed)
  tickets_shipped                 N    (count of tickets currently shipped)
  tier                            X    (read from people.yaml: SDE1/SDE2/SDE3)
  tier_expected_range             X    (read from tier_expectations.yaml)
  tier_deviation                  X    (above-band / in-band / below-band)
                                       ← see Reliability Gates BELOW
                                         before emitting this

OPS TRACK (CMR work — for ops-heavy ICs)

  cmr_authored                    N    (issue_type=CMR, actor=person)
  cmr_assigned                    N    (issue_type=CMR, assignee=person)
  cmrs_closed                     N    (CMRs with to_status in ops_closed
                                        OR shipped — both count as "closed
                                        ops work")
  ops_close_rate                  X%   (cmrs_closed / cmr_authored)
  rectifications_authored         N    (tickets with title LIKE "Fix%" /
                                        "Rectify%" / "Data correction%")

CANCELLATION + QUALITY DRIFT

  cancellation_rate               X%   (tickets in cancelled / total assigned)
                                       ← flag if > 20% — scope churn signal
  tickets_in_flight               N    (status in in_flight class)
  bugs_assigned_to_person         N    (issue_type=Bug, assignee, in window)
  bugs_authored_by_person         N    (issue_type=Bug, actor — finding bugs
                                        vs being blamed for them)

QUALITY SIGNALS (code + ticket quality indicators)

  pr_matterai_quality_p50         X%   (median MatterAI Code_Quality% across the
                                        person's pr_opened in window — extract from
                                        bot comments matching "Code_Quality-NN%")
  pr_matterai_critical_flags      N    (PRs where MatterAI review body contains
                                        "Critical" / "critical issues found")
  pr_revert_count                 N    (PRs with title LIKE "Revert%" by person
                                        OR linked-to person's prior PR)
  bug_to_feature_ratio            X:Y  (bugs_assigned / (Stories + Tasks authored))

Authorship is INTENTIONALLY low in the CONTRIBUTION list — weakest of those.

Behavioral signals are workload-health + style indicators, NOT performance
indicators. High p90 latency or high after-hours share is a flag for the
person's burnout/availability, not for their quality.

Throughput + Quality signals are productivity-and-craft indicators. Always
interpret against tier_expectations.yaml — never as absolute targets.
Surface deviations as flags to discuss, not as verdicts.

### Throughput verdict reliability gates (MANDATORY before emitting tier_deviation)

Before printing a tier_deviation verdict (in-band / below-band / above-band),
check these gates. If ANY gate fails, REFUSE to emit a tier verdict and
print the unreliability flag instead. The data tool's job is to be honest
about what it can and cannot say.

Gate 1 — SP coverage (only on sprinted tickets)

  Team convention: story_points are populated ONLY when a ticket is moved
  into an active sprint. Backlog-only tickets legitimately have story_points
  = NULL — they should NOT degrade SP-coverage.

  - Compute SP_eligible = tickets that have EVER been in a sprint
    (sprint_id IS NOT NULL on issue_created OR ticket has a sprint_change
    event in window). Exclude pure backlog tickets.
  - Compute SP_coverage = (SP_eligible AND story_points IS NOT NULL)
                          / SP_eligible
  - If SP_coverage < 0.70 AND SP_eligible >= 5 (sample-size gate):
    FLAG `sp_coverage_below_70pct` — print
    "Tier-band verdict suppressed: only X% of sprinted tickets have
    story_points set (Y / Z). SP-pointing is being skipped at sprint-load
    time. Surface as a process gap, not a velocity gap."
  - If SP_eligible < 5: FLAG `insufficient_sprinted_tickets` — print
    "Tier-band verdict suppressed: only N sprinted tickets in window —
    too few to compute meaningful velocity."
  - In both cases, surface as a Gap in section 5 with intervention
    candidates (L2 norms: enforce SP-tagging at sprint-load time).

Gate 2 — CMR / ops-heavy role
  - Compute CMR_share = (tickets where issue_type='CMR') / (total assigned)
  - If CMR_share >= 0.30: FLAG `cmr_heavy_role` — print
    "Tier-band verdict needs ops parallel-track: X% of tickets are CMRs
    (production change requests, not story-pointed by convention).
    Standard SP-velocity expectation does not apply cleanly. Compute
    parallel: cmr_closed_count per window vs an ops-band expectation
    (define separately)."

Gate 3 — Window-vs-sprint-cadence sanity
  - Window must span at least 1 sprint (`working_days * 1` per
    config/tier_expectations.yaml). If shorter, FLAG `window_too_short`
    and skip tier_deviation entirely.

When all gates pass, then and only then emit:
  - `tier_deviation: in-band | above-band | below-band` against
    `tier_expected_range`.
  - Frame as "in this window, against SP-tracked work only" — never as
    absolute productivity verdict.

### Ops parallel track (when CMR_share ≥ 0.30)

For ops-heavy ICs (Alice, Bob, others), compute these IN ADDITION
to SP throughput:

  ops_cmrs_authored               N    (issue_type='CMR' authored)
  ops_cmrs_closed_proxy           N    (CMRs with closure-marker comment:
                                        "Reviewed" / "DAM alert was updated" / "Done")
  ops_close_rate                  X%   (closed / authored)
  rectifications_authored         N    (tickets with title LIKE "Fix%" /
                                        "Rectify%" / "Data correction%")

Compare against ops_band_expectation if defined; surface as a separate
verdict from SP throughput. Two parallel tracks: feature velocity (SP)
+ ops velocity (CMR count).

## Confirmed by data

Per-claim mapping back to real, openable evidence — NO field names, NO
cluster IDs, NO engine names (see rule zero). Each bullet = the claim, then
the artefacts a human can open plus a plain-English description of the
activity. Example:

  - "Bob drove the year-end EOD/BOD disable" — he left long investigative
    comments on the Year-End-Job tickets, edited the Year-End-Job page three
    times, and opened the approval thread ([thread](url)). The decision
    rationale is written out in the body of [EX-2479](url), which he
    authored — authorship matters here because the ticket body IS the
    decision.

  - "Bob drove production ledger-balance rectifications" — he commented on
    and moved [EX-1959](url), [EX-2259](url) and [EX-2439](url) to
    done, with the correcting SQL written inline in the ticket bodies.

## Data silent on

User framings the data cannot confirm or deny. Examples:

  - "Highly celebrated performer" — data shows volume + breadth, doesn't
    measure peer perception or stakeholder testimony.
  - "Drove DR-drill" — depends on definition. Data shows ownership of
    EOD/BOD disable piece; multi-person ownership across full DR-drill
    (owner authored parent placeholder, example-dev4 flagged AWS prereq,
    example-dev5 ran deployments).
  - "High impact" — proxy gap: event counts measure volume not influence.
    Critical-path-only-them work, multiplier effects, whiteboard decisions
    don't appear.

ALWAYS surface this section honestly. Never pad. If user's framing IS
well-supported by data, say so explicitly: "framing 'X' is confirmed
by signals Y + Z, no silence here."

## Novel observations (subjective — reading the data, not the rubric)

Things THIS person + window surfaced that the baseline doesn't capture.
Examples of the shape (must come from actual data, not invented):

  - "Every deploy-console deployment ask this window followed the pattern:
    PR link shared first, approval requested second, sync done within
    20 minutes. No exceptions."
  - "Of 9 ledger-balance rectifications across 6 months, Bob wrote
    the SQL inline in 4; commented-on-someone-else's-SQL in 5."
  - "Touched service-b + service-a + deploy-console + 4 Slack channels —
    repos and channels span Platform, deposits, and Ops domains."

This section is mandatory but the SHAPE varies — emergent patterns
specific to this query. If the data is thin or you couldn't surface
anything beyond the rubric: say so plainly ("no novel patterns surface
in this window beyond baseline signals"). Don't pad.

## Gaps / coaching opportunities (signal-derived, NOT judgement)

Patterns the data surfaces that may indicate growth areas or
behavioral flags. Frame as observations to test in 1:1 / growth
conversations, not as verdicts. Distinguish from "Data silent on"
(which is about the data's limits, not the person).

Sources for this section:
- Contribution-signal asymmetries (e.g. ships heavily but reviews
  rarely; authors many tickets but never long-form comments).
- Behavioral signals from slack — p50/p90 response latency,
  after-hours share, thread-followup rate, first-responder vs
  resolver imbalance.
- Concentration risks (single-domain expertise, only one source touched).
- Engagement-channel gaps (engages on slack but never on jira; never
  on Confluence; etc.).
- Process patterns that suggest friction (revert/re-apply churn,
  many-revision contract pages without clear scope, drop-off on own
  threads).

Examples of the shape:

  - "0 substantive PR reviews while writing 69 commits — asymmetric.
    Ships heavily but doesn't engage in peer-review depth. Worth
    discussing whether intentional focus on shipping vs gap in
    reviewer practice."

  - "p90 response latency = 4h while p50 is 12m. Median is healthy
    but tail is long — usually means a few critical asks went
    unanswered overnight. Worth checking if oncall escalation paths
    are working."

  - "after_hours_share = 38% — meaningful share of work outside
    9am-7pm IST. Could be timezone-shifted workstreams, could be
    workload pressure. Worth confirming preference vs imposition."

  - "thread_followup_rate = 23% — opens 4× more threads than they
    follow up on. Could be quick-broadcast pattern (intentional) or
    could indicate dropped balls."

  - "0 substantive jira_comments — engages on others' work via slack
    (148 replies) and confluence (39 inline comments) but not via
    jira comments. May miss async/long-form contributors who default
    to jira."

  - "first_responder_rate = 52% + resolver_rate = 8% — picks up
    threads quickly but rarely the one who closes them. Strong
    triage signal, weaker resolution signal. Could be coaching
    opportunity on driving closure."

  - "5-revision contract page over 2 weeks — could be healthy
    iteration or design churn. Worth confirming scope was clear
    upfront."

  - "Recurring share 0.5% — clean signal, but ALSO no opsgenie
    rotation participation. Could be coverage gap if person is
    expected to share oncall."

  - "100% domain concentration on withholding/deposits/cash. Strength = depth.
    Risk = single-domain expertise; cross-rotation into adjacent
    surfaces would broaden."

This section is mandatory but may be EMPTY: write
`"no notable gaps surface in this window — signals are healthy across
contribution + behavioral dimensions"` if data genuinely doesn't suggest
any. Don't fabricate growth areas to look balanced. Don't pad.

Frame every entry as "worth discussing in 1:1" / "worth confirming"
/ "may indicate" — never as a verdict. The data sees patterns, the
manager + the person know context.

## Interventions to consider (person_range only — menu, manager decides)

ONLY emit this section for the `person_range` intent. Skip entirely
for summarize / attention / etc.

For each gap surfaced in the previous section, propose intervention
options across 3 layers. Frame as a MENU, not a prescription. The
manager picks; the data tool suggests. Skip layers that don't fit
a given gap. Drop the entire section if Gaps was empty.

Layer 1 — Person-level (1:1 conversations)

  Direct discussion prompts the manager can use in 1:1. Frame as
  open questions, not interrogations.

  Example:
    Gap = "0 substantive PR reviews while writing 69 commits"
    L1  = "Ask: 'What stops you reviewing more — bandwidth, comfort,
           or you don't get tagged?' Surface whether intentional vs
           gap in habit."

Layer 2 — Team / process changes

  Norms or process gates the team can adopt. Should be cheap and
  reversible.

  Example:
    Gap = "p90 response latency = 4h, p50 = 12m — tail of unanswered
           critical asks"
    L2  = "Define explicit SLA for @here/@subteam pings (e.g. ack
           within 4h, resolve within 24h). Track weekly via slack
           validate."

Layer 3 — Tool / system changes (build to make patterns visible)

  Auto-surfacing or measurement we can build. Avoid recommending if
  manual process (L1+L2) would suffice.

  Example:
    Gap = "after_hours_share = 38% — workload health unclear"
    L3  = "Auto-alert when after_hours_share spikes >20% over rolling
           4 weeks. Build into cron-status."

Hard rules for this section:

  - Never frame as "X should do Y" — frame as "consider Y" / "worth
    trying Y" / "menu options".
  - Distinguish from Gaps section: Gaps = pattern observation.
    Interventions = what to try about it.
  - One-to-one mapping per gap is NOT required — some gaps need
    only L1, some need L2+L3, some genuinely have no clean
    intervention beyond "more context needed in 1:1".
  - **Only render layers that have content.** If a gap only needs L1,
    show JUST L1 — do NOT print "L2: Skip" or "L3: Skip" lines.
    Skip-lines are clutter; absence already conveys it.
  - If a gap is "data is silent on this" (lives in caveats, not
    Gaps), don't propose interventions — the gap is in OUR data,
    not the person's behaviour.
  - The intervention menu's job is to make the 1:1 prep cheaper,
    not to replace manager judgement.

## Detail

  Multi-paragraph narrative, themes-not-clusters, inline citation
  links woven into prose. Grounded in Signals + Confirmed-by-data
  facts. NO subjects/URLs dumped at the bottom — every link is
  in flow. Short sentences. Reader is a manager — prose, not specs.
```

### Length guidance

- summarize: 3-5 paragraphs in Detail with 4-8 inline citations
- person_range: 4-6 paragraphs in Detail grouped by theme (NOT by
  cluster). Each paragraph = one workstream the person owned, with
  concrete actions, dates, decisions, and 2-3 inline citation links
  woven into the prose.
- attention: TL;DR is the worklist (each bullet states WHAT'S BLOCKED).
  Detail section may be skipped if TL;DR fully covers.
- ticket_gaps: TL;DR + bullet list of gaps. No Detail section needed.
- rootcauses: prose categorised by domain in Detail; cite tickets inline.

## Phase 5 — Save output to markdown file (MANDATORY)

**Every `/ask` run writes its rendered output to a markdown file under
`management/`.** Chat-inline output is fine but the file is the
durable artefact — owner reads + re-reads + grep-searches it later.

Filename convention by intent (use lowercase kebab-case for variable
parts; pick a 4-6-word `<topic-slug>` for free-text intents):

| Intent          | Path                                                                                        |
|-----------------|---------------------------------------------------------------------------------------------|
| person_range    | `management/narratives/per-person/<canonical>-<since>-to-<until>.md`                        |
| team_range      | `management/narratives/team/<since>-to-<until>.md`                                          |
| summarize       | `management/queries/summarize-<topic-slug>-<YYYY-MM-DD>.md`                                 |
| attention       | `management/queries/attention-<YYYY-MM-DD>.md`                                              |
| ticket_gaps     | `management/queries/ticket-gaps-<YYYY-MM-DD>.md`                                            |
| rootcauses      | `management/queries/rootcauses-<since>-to-<until>.md`                                       |
| feature_logic   | `management/queries/feature-logic-<topic-slug>-<YYYY-MM-DD>.md`                             |
| event_metrics   | `management/queries/event-metrics-<topic-slug>-<since>-to-<until>.md`                       |
| dev_style       | (handled by `/dev-style` skill)                                                             |
| highs_lows      | (handled by `/retro` skill — writes to `management/retros/<since>-to-<until>.md`)           |

**File header (mandatory, first 4 lines):**

```markdown
# <Concise title>

**Intent:** <person_range | summarize | …>
**Generated:** <YYYY-MM-DD HH:MM IST>
**Window:** <since> → <until>  (only when intent has a window)
**Question:** "<verbatim user question>"

---

<rendered content — TL;DR + Signals + … per Phase 4 spec>
```

**Body:** the full rendered output exactly as it would appear in chat.
The same content lives in both surfaces — chat reply is the live preview,
the file is the persistent copy. Inline citations + project-level voice
rules apply equally.

**Never overwrite.** If the target path exists, append `-2`, `-3`, …
suffix before `.md`. Older runs are evidence trail; don't lose them.

**Path conventions:**

- All paths are relative to repo root (`$HOME/context`).
- Create parent dirs (`mkdir -p`) before write.
- Use the Write tool — never `cat > file` via Bash (per file-writing policy).
- Date format `YYYY-MM-DD` (ISO short). Timestamps in IST.

**After writing, the chat reply MUST end with:**

```
**Saved to:** `<absolute path>`
```

So owner can open the file directly.

**Why mandatory:** ad-hoc /ask queries previously produced chat-only output
that disappeared once the session ended. The file preserves the analysis
+ citations for future reference (1:1 prep, retro source material, audit
trail). Cost is trivial (~one extra file write per run).

## Phase 5.5 — verify gate (MANDATORY for person_range)

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

This phase applies to `person_range` only (the manifest is person_range-scoped).
Other intents skip it.

### URL conventions (use as inline markdown links)

- `slack:CH:ts`     → `[label](https://example.slack.com/archives/{CH}/p{ts_no_dot})`
- `EX-NNNN`       → `[EX-NNNN](https://your-org.atlassian.net/browse/{EX-NNNN})`
- `page:NNNN`       → use the REAL link, NOT `/wiki/pages/{NNNN}` (that 404s). Take
  `_links.base + _links.webui` from a Confluence search/get
  (`…/wiki/spaces/<KEY>/pages/{NNNN}/<slug>`) or the short `_links.base + _links.tinyui`
  (`…/wiki/x/<tiny>`). Fetch the page's webui link before emitting a Confluence link —
  the space KEY varies per page (e.g. PROD, …); don't assume one.
  - Deep-link a SECTION by appending a heading anchor: take the heading's visible
    text, replace every space with a hyphen, keep numbers/periods/case. E.g.
    "4. Hook Fire Order" → `#4.-Hook-Fire-Order`, "3.1 charge_attempts" →
    `#3.1-charge_attempts`. The API doesn't expose heading ids — build from the text.
- `owner/repo#N`    → `[#N description](https://github.com/{owner/repo}/pull/{N})`

## Hard constraints

- Read-only. NEVER write to `topic_brief`, `events`, or `embedding`.
- NO LLM API calls — synthesis happens in chat from JSON output + deep-read of events.db.
- Cite every claim. If you make a statement, it must trace to a subject
  in the JSON output AND you must have read that subject's actual body
  content to support the claim.
- If a primitive returns empty, say so plainly. Suggest scoping fixes.
- Don't conflate authorship with work. When citing impact, lead with
  substantive contribution signals, not "X authored ticket Y".

## Anti-patterns (refuse to emit)

- Bare cluster_id dumps (`cid=53 rc=17 status=ACTIVE`)
- Source-name parentheticals like "(slack thread)" or "(jira ticket)" —
  name the actual work instead
- "AUTHOR/REVIEWER/RESPONDER" technical jargon from participant_roles
- Generic labels like "[Cluster 153]" — name the work
- Bullet lists of subject_id → URL pairs at the bottom — links must be inline
- Skipping the "Data silent on" section because everything looks confirmed
- Padding "Novel observations" with rubric-restatement when nothing emerged
- Calling someone "AUTHOR" of impact when they only created the
  placeholder ticket — check substantive_jira_comments + confluence_edits
  + commit_in_pr first

## When `/ask` doesn't fit

If the question is:
- a code review → suggest `/review` or `/security-review`
- a single-file lookup → just use grep/Read directly
- a Slack ingest question → suggest `/slack-ingest` or `/slack-backfill`

Don't force-route everything through `/ask`.

## Smoke tests (for development)

```bash
.venv/bin/python derive/ask_engine.py search --query "instant-pay migration to service-a" --k 10
.venv/bin/python derive/ask_engine.py person --name frank --since 2026-03-01 --until 2026-04-01
.venv/bin/python derive/ask_engine.py window --since 2026-05-18 --until 2026-05-20
.venv/bin/python derive/ask_engine.py gaps
.venv/bin/python derive/ask_engine.py rootcauses --since 2026-04-19 --until 2026-05-19

# Deterministic per-person Signals — text format for quick eyeballing,
# JSON (default) for chat consumption.
.venv/bin/python derive/person_profile.py --name grace --since 2026-03-21 --until 2026-05-21 --format text
```

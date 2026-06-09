# PR Quality & Friction Scorer (Post-Merge)

**Status:** Draft
**Designed:** 2026-06-03
**Owner:** owner@example.com
**Driver:** Manual PR review is a recurring bottleneck. We want to measure
*friction* on every merged PR over time, surface where it concentrates
(author / repo / change-type), and use that to decide what agentic review can
safely absorb — so human review stops being a blocker for the routine cases.

## Goal

Score every **merged** PR on how much review friction it generated, then
aggregate to find recurring, automatable friction patterns.

"Friction" = human effort + back-and-forth a PR cost before it landed. High
friction that is *repetitive and mechanical* is the target for agentic review.

## Non-goals

- Not a gate. Post-merge only — never blocks or auto-rejects a PR.
- Not a developer *ranking* or performance metric. Per-dev output is
  **formative/coaching** ("work on edge cases", "watch security coverage"),
  never a leaderboard or rating. (Mirrors `dev-style` read-only framing.)
- No new LLM calls in scripts. Mechanical signals are pure SQL/python.
  Classification of comment *nature* happens in a chat turn only
  (`project_chat_only_classification.md`).
- No real-time / webhook ingestion. Runs over already-ingested `events.db`.

## Data we have (events.db, github source)

~705 merged PRs, 2 repos (`example-org/service-a`, `service-b`),
2025-07 → 2026-06. All GitHub data is normalized into the unified `events`
table (no github-specific tables).

| Signal | Source today | Notes |
|---|---|---|
| Time-to-merge | `pr_opened.ts` → `pr_merged.ts` | direct |
| Review rounds | count of `review` events per PR | state in `title`/`body` |
| Changes-requested cycles | `review` events with CHANGES_REQUESTED state | rework driver |
| Discussion volume | `comment` + `issue_comment` per PR | bodies stored |
| Rework commits | `commit_in_pr` after first review ts | iteration signal |
| Comment / review *text* | `body` column | enables nature classification |
| MatterAI AI summary | `review` events by `matterai-example-org[bot]` | pre-classified severity/nature, every PR since 2025-11-07 |
| Author / reviewers / merged_by | `actor`, `pr_merged_by` | attribution |

## Data gaps (decide in build)

| Missing | Why it matters | Options |
|---|---|---|
| additions / deletions / files-changed | normalize "comments per LOC"; small PR with many comments = real friction | additive ingest field, or on-demand raw-API fetch per PR |
| CI / check status + failing tests | "tests failing post-merge / in-PR" is a core quality signal user named | GitHub checks API; additive ingest |
| Draft status, labels | filter noise (draft churn), segment by change-type | additive ingest |

**Decided:** backfill all three via raw GitHub API for the ~705 historical
PRs (additive ingest field), then capture forward.

## Proposed architecture (two layers, matches existing pattern)

**Layer 1 — mechanical (scripts, no LLM):** `derive/github_metrics.py`
- New module, peer to `derive/jira_metrics.py`. All PR-interpretation logic
  lives here; skills consume, never reimplement.
- Computes per-merged-PR mechanical signals (table above) into a
  `pr_friction` view/table in `events.db` (additive migration).

**Layer 2 — comment classification (chat turn):** `/pr-quality` skill
- Dumps every review/comment body needing classification — **both human
  reviewer comments AND MatterAI bot comments** — per PR.
- Chat tags each comment with a *root-cause category* (see taxonomy below),
  not just a severity. The category distribution is the diagnostic signal.
- Rules live in a `pr_review_rules.md` (peer to classification rules.md).
- Writes verdicts back; scripts never call the Anthropic API.

### Comment taxonomy (root-cause oriented)

The point is not "how bad" but "what *kind* of attention the PR was missing".
Category mix per PR → diagnosis of where the developer's process broke down.

| Category | Signals | What a cluster of these means |
|---|---|---|
| `business-logic` | wrong/missing requirement, flow gap, edge case from spec | developer didn't internalise the PRD / requirements |
| `correctness` | actual bug, wrong result, null/error handling | logic defects slipped through |
| `test-gap` | missing/insufficient/incorrect tests | weak test discipline |
| `design` | architecture, coupling, wrong abstraction | needs design review earlier |
| `security` | auth, injection, data exposure | security blind spot |
| `naming` | identifier clarity | low-cost, automatable |
| `nit` | style, formatting, typos | low-cost, automatable |
| `question` | reviewer needs clarification | PR description / context gap |
| `praise` | positive | excluded from friction |

Both sources get the same taxonomy so we can compare: *what does the human
catch that MatterAI misses, and vice-versa.* That delta is the key input to
"what can agentic review safely take over."

## Friction score (shape, weights TBD in validation)

Composite, normalized by change size. Illustrative inputs:
- review rounds + changes-requested cycles (rework)
- rework commits after first **human** review
- time-to-merge (outlier-capped)
- category-weighted comment count (from Layer 2): `business-logic` /
  `correctness` / `security` weighted **high**; `design` / `test-gap` mid;
  `naming` / `nit` near-zero
- in-PR test failures (once CI ingested)

MatterAI comments feed the score alongside human comments (same taxonomy, but
weighted **below** an equivalent human comment — a human blocker is stronger
signal than a bot flag).

Output: 0-100 friction score + the **dominant friction category** per PR
(e.g. "business-logic" → developer missed the PRD).
Explicitly **down-weight nits/naming** so 20 style nits ≠ 1 business-logic miss.

## Aggregation / output

`/pr-quality` produces, over a flexible date range:
- Per-PR friction line (score + dominant reason + link)
- Rollups: by repo, by change-type, by friction-reason
- **Friction patterns**: recurring mechanical reasons (e.g. "missing tests",
  "formatting nits", "naming") that are candidates for agentic review to absorb
- Trend over time: is friction dropping as agentic review expands?
- **Review coverage gap** (see below)

## Per-PR and per-dev actionables

Two levels of actionable output, both coaching-oriented:

**Per-PR** — one line per merged PR: friction score, dominant category, and a
short "what would've made this smoother" note.
- e.g. "#412 (Alice): clean, no blocking comments — good PR."
- e.g. "#398 (Bob): 3 correctness comments on edge cases — needs edge-case
  coverage before review."

**Per-dev** — roll a developer's merged PRs over the window into their dominant
recurring friction categories, phrased as 1-2 coaching actionables.
- e.g. "Bob — recurring `correctness`/edge-case comments → focus on edge
  cases and add tests for them."
- e.g. "Alice — `security` category under-covered across PRs → add a
  security self-check before raising."
- e.g. "Carol — low friction, mostly nits → no action."

Attribution uses PR author (`pr_opened.actor`), mapped to identity via
`people.yaml`. Categories come from Layer 2 classification, so the actionable
is grounded in actual reviewer comments, not vibes.

## Review coverage gap: human vs agentic

A first-class output, not just a side signal. For each PR we have two comment
streams under the *same taxonomy*: human reviewers and MatterAI. Comparing them
tells us where agentic review already suffices and where it can't yet.

Three buckets per category:
- **Bot-covered** — humans + MatterAI both flagged it. Agentic review already
  handles this category → candidate to drop/reduce human review here.
- **Human-only (the gap)** — humans flagged, MatterAI missed. This is the work
  that *still needs a human*. The category mix here is the real answer to "what
  can't be automated yet" (likely heavy on `business-logic`, `design`).
- **Bot-only** — MatterAI flagged, humans didn't. Either genuine value-add
  (bot caught something) or noise (bot false-positives humans ignore). Tag
  which, because noise erodes trust in agentic review.

### Bridging suggestions (the actionable layer)

From the gap profile, `/pr-quality` proposes concrete moves, e.g.:
- Categories where bot-coverage is high + stable → "safe to auto-handle;
  reserve human review for X." (reduces manual load directly)
- `human-only` categories that are *mechanical* (e.g. `test-gap`, `naming`) →
  candidate prompt/rule additions to the agentic reviewer to close the gap.
- `human-only` categories that are *judgement-heavy* (`business-logic`,
  `design`) → keep human; instead fix upstream (PRD clarity, design review).
- High `bot-only` noise in a category → tune the agentic reviewer down there
  to protect trust.

Output framing: "here is the gap, here is what to automate vs keep human, here
is the specific change that would close each closable gap."

## Decisions made

- **Build the skill** (not validate-first).
- **Backfill** diff-size + CI + labels for the ~705 historical PRs via raw
  GitHub API (additive ingest field).
- **MatterAI = signal feeding the score**, classified under the same taxonomy
  as human comments. We also track the human-vs-MatterAI category delta as the
  input to "what agentic review can take over."
- **Output levels: per-PR + per-dev** actionables (coaching, not ranking).
  Per-repo rollup optional/secondary.
- **Human comments weighted above MatterAI** comments in the score.
- **Rework window starts at first *human* review** — bot comments arrive
  near-instantly and would zero out the rework signal.
- **Classification is a separate `/pr-quality` pass, NOT folded into `/rollup`.**
  Reuses rollup's dump→classify→apply machinery but stays its own skill —
  different unit (per-comment vs per-subject), different table
  (`pr_comment_class` vs `subject_summary`), narrower scope (github review/
  comment bodies on merged PRs only). May be chained to run after `/rollup`,
  but never merged into it.

## Open questions

- None blocking. Per-repo "review-health" index is a possible later add-on.

## Build plan (phased)

Mirrors existing patterns: ingest (raw API, no LLM) → migration → pure-function
derive module → dump/classify/apply skill. Each phase is independently
shippable and testable.

### Data model (Migration `009_pr_quality.sql`)

Three new tables in `events.db` (additive, via `_add_column_if_missing` /
`CREATE TABLE IF NOT EXISTS` in `ingest/common.py::_ensure_schema`):
- `pr_meta` — keyed by subject (`owner/repo#num`): additions, deletions,
  files_changed, checks_status, checks_failed_json, labels_json, is_draft.
  PR-level facts that don't fit the append-only event log.
- `pr_comment_class` — keyed by event_id: category, source (`human`|`matterai`),
  confidence. One row per classified review/comment.
- `pr_friction` — keyed by subject: score 0-100, dominant_category,
  mechanical_json (rounds, rework, ttm), category_counts_json, computed_at.

### Phase 1 — Ingest: capture diff stats + CI (no LLM)
- Extend `ingest/github.py`: aggregate per-file `additions`/`deletions`/count
  (already in PR payload) → write to `pr_meta`. Add labels + draft flag.
- Add CI fetch: `/repos/{repo}/commits/{sha}/check-runs` per PR head →
  status + failing-check names → `pr_meta`. (Only genuinely new API call;
  watch rate limit on 705-PR backfill — batch + respect remaining headers.)
- Forward capture is free: github-ingest LaunchAgent already fires every 30min.

### Phase 2 — Backfill historical PRs
- One-shot: re-walk the 705 merged PRs to populate `pr_meta` (diff stats + CI).
- Owner-invoked script, idempotent (upsert on subject). No LLM.

### Phase 3 — `derive/github_metrics.py` (pure functions, no LLM)
- Peer to `derive/jira_metrics.py`; reuses its `load_people_lookup` for
  github-login → canonical.
- Exposes: `first_human_review_ts()` (excludes `matterai-example-org[bot]`),
  `mechanical_signals(pr)` (rounds, changes-requested cycles, rework commits
  after first human review, ttm), `compute_friction(pr)`,
  `aggregate_by_dev()`, `coverage_gap()` (human vs matterai buckets).
- Single source of truth; the skill consumes, never reimplements.

### Phase 4 — `/pr-quality` skill: classify (dump → chat → apply)
Standalone skill (not a `/rollup` sub-phase); reuses the rollup harness.
- **Dump** (`derive/pr-quality.sh dump`): emit unclassified review/comment
  bodies (human + matterai) → `state/pending_pr_comments.json` +
  `state/pending_pr_comments.json.rules.md` (the taxonomy). No API key in script.
- **Classify** (chat turn): tag each comment with taxonomy category →
  `state/verdicts.pr_comments.json`.
- **Apply** (`derive/pr-quality.sh apply`): upsert `pr_comment_class`, then
  recompute `pr_friction` (mechanical + category-weighted, human > matterai).
- Rules file: `config/pr_review_rules.md` (taxonomy + weights, peer to rules.md).

### Phase 5 — `/pr-quality` skill: report
- Command file `.claude/commands/pr-quality.md`, flexible date range.
- Renders: per-PR friction lines, per-dev coaching actionables, human-vs-agentic
  coverage gap + bridging suggestions, trend over time.
- Output obeys skill-format rules (not chat-reply style).

### Sequencing notes
- Phases 1-3 are mechanical and can land + be validated before any LLM work.
- Phase 4 classification can run incrementally (only new/unclassified comments).
- `pr_friction` is recomputed on apply, so mechanical-only scores exist from
  Phase 3 and get sharper once classification lands.

## Success criteria

- Every merged PR gets a friction score + dominant reason.
- We can name the top 3 recurring mechanical friction reasons per repo.
- A clear, defensible shortlist of friction types agentic review can take over.
- A human-vs-agentic coverage-gap profile per category, with concrete bridging
  suggestions (what to automate, what to keep human, what change closes each
  closable gap).
- Per-dev coaching actionables grounded in real reviewer comments (e.g. "work
  on edge cases", "watch security coverage"), not a ranking.

# PR Quality & Friction Scorer (Post-Merge)

**What this is:** score every *merged* PR on review friction, aggregate to find recurring/automatable friction patterns, and decide what agentic review can safely absorb.

**Status:** Draft · **Designed:** 2026-06-03 · **Owner:** owner@example.com

**Driver:** Manual PR review is a recurring bottleneck. Measure friction per merged PR over time, surface where it concentrates (author / repo / change-type), use it to offload routine cases to agentic review.

**Friction** = human effort + back-and-forth a PR cost before landing. Repetitive + mechanical friction is the target for agentic review.

## Non-goals

- **Not a gate.** Post-merge only — never blocks or auto-rejects.
- **Not a ranking / perf metric.** Per-dev output is formative/coaching ("work on edge cases", "watch security coverage"), never a leaderboard. Mirrors `dev-style` read-only framing.
- **No new LLM calls in scripts.** Mechanical signals = pure SQL/python. Comment-nature classification happens in a chat turn only (`project_chat_only_classification.md`).
- **No real-time / webhook ingestion.** Runs over already-ingested `events.db`.

## Data we have (events.db, github source)

~705 merged PRs, 2 repos (`example-org/service-a`, `service-b`), 2025-07 → 2026-06. All normalized into the unified `events` table (no github-specific tables).

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
| additions / deletions / files-changed | normalize "comments per LOC"; small PR + many comments = real friction | additive ingest field, or on-demand raw-API fetch per PR |
| CI / check status + failing tests | "tests failing post-merge / in-PR" is a core quality signal | GitHub checks API; additive ingest |
| Draft status, labels | filter noise (draft churn), segment by change-type | additive ingest |

**Decided:** backfill all three via raw GitHub API for the ~705 historical PRs (additive ingest field), then capture forward.

## Architecture — two layers (matches existing pattern)

**Layer 1 — mechanical (scripts, no LLM):** `derive/github_metrics.py`
- New module, peer to `derive/jira_metrics.py`. All PR-interpretation logic lives here; skills consume, never reimplement.
- Computes per-merged-PR mechanical signals (table above) into a `pr_friction` view/table in `events.db` (additive migration).

**Layer 2 — comment classification (chat turn):** `/pr-quality` skill
- Dumps every review/comment body needing classification — **both human reviewer AND MatterAI bot comments** — per PR.
- Chat tags each comment with a *root-cause category* (taxonomy below), not just a severity. The category distribution is the diagnostic signal.
- Rules in `config/pr_review_rules.md` (peer to classification rules.md).
- Writes verdicts back; scripts never call the Anthropic API.

### Comment taxonomy (root-cause oriented)

Point is not "how bad" but "what *kind* of attention the PR was missing". Category mix per PR → where the dev's process broke down.

| Category | Signals | A cluster means |
|---|---|---|
| `business-logic` | wrong/missing requirement, flow gap, spec edge case | didn't internalise the PRD |
| `correctness` | actual bug, wrong result, null/error handling | logic defects slipped through |
| `test-gap` | missing/insufficient/incorrect tests | weak test discipline |
| `design` | architecture, coupling, wrong abstraction | needs design review earlier |
| `security` | auth, injection, data exposure | security blind spot |
| `naming` | identifier clarity | low-cost, automatable |
| `nit` | style, formatting, typos | low-cost, automatable |
| `question` | reviewer needs clarification | PR description / context gap |
| `praise` | positive | excluded from friction |

Both sources use the same taxonomy → compare *what the human catches that MatterAI misses, and vice-versa.* That delta drives "what can agentic review safely take over."

## Friction score (shape; weights TBD in validation)

Composite, normalized by change size. Inputs:
- review rounds + changes-requested cycles (rework)
- rework commits after first **human** review
- time-to-merge (outlier-capped)
- category-weighted comment count (Layer 2): `business-logic` / `correctness` / `security` weighted **high**; `design` / `test-gap` mid; `naming` / `nit` near-zero
- in-PR test failures (once CI ingested)

MatterAI comments feed the score under the same taxonomy but weighted **below** an equivalent human comment (human blocker > bot flag).

**Output:** 0-100 score + **dominant friction category** per PR (e.g. "business-logic" → missed the PRD). Down-weight nits/naming so 20 style nits ≠ 1 business-logic miss.

## Aggregation / output

`/pr-quality`, over a flexible date range:
- Per-PR friction line (score + dominant reason + link)
- Rollups: by repo, by change-type, by friction-reason
- **Friction patterns**: recurring mechanical reasons (missing tests, formatting nits, naming) = candidates for agentic review to absorb
- Trend over time: is friction dropping as agentic review expands?
- **Review coverage gap** (below)

## Per-PR and per-dev actionables

Both coaching-oriented.

**Per-PR** — one line per merged PR: score, dominant category, short "what would've made this smoother" note.
- "#412 (Alice): clean, no blocking comments — good PR."
- "#398 (Bob): 3 correctness comments on edge cases — needs edge-case coverage before review."

**Per-dev** — roll a dev's merged PRs over the window into dominant recurring categories → 1-2 coaching actionables.
- "Bob — recurring `correctness`/edge-case → focus on edge cases, add tests."
- "Alice — `security` under-covered across PRs → add a security self-check before raising."
- "Carol — low friction, mostly nits → no action."

Attribution = PR author (`pr_opened.actor`) mapped via `people.yaml`. Categories come from Layer 2, so actionables are grounded in real comments, not vibes.

## Review coverage gap: human vs agentic

First-class output. Each PR has two comment streams under the *same taxonomy* (human reviewers, MatterAI). Comparing them shows where agentic review suffices and where it can't yet.

Three buckets per category:
- **Bot-covered** — both humans + MatterAI flagged → candidate to drop/reduce human review here.
- **Human-only (the gap)** — humans flagged, MatterAI missed → still needs a human. Category mix here = the real answer to "what can't be automated yet" (likely heavy on `business-logic`, `design`).
- **Bot-only** — MatterAI flagged, humans didn't → either genuine value-add or noise (false-positives humans ignore). Tag which; noise erodes trust in agentic review.

### Bridging suggestions (the actionable layer)

From the gap profile, `/pr-quality` proposes concrete moves:
- High + stable bot-coverage → "safe to auto-handle; reserve human review for X." (cuts manual load)
- `human-only` + *mechanical* (`test-gap`, `naming`) → candidate prompt/rule additions to the agentic reviewer to close the gap.
- `human-only` + *judgement-heavy* (`business-logic`, `design`) → keep human; fix upstream (PRD clarity, design review).
- High `bot-only` noise in a category → tune the agentic reviewer down there to protect trust.

Framing: "here is the gap, what to automate vs keep human, the specific change that closes each closable gap."

## Decisions made

- **Build the skill** (not validate-first).
- **Backfill** diff-size + CI + labels for ~705 historical PRs via raw GitHub API (additive ingest field).
- **MatterAI = signal feeding the score**, classified under the same taxonomy as human comments; track the human-vs-MatterAI category delta as the input to "what agentic review can take over."
- **Output levels: per-PR + per-dev** (coaching, not ranking). Per-repo rollup optional/secondary.
- **Human comments weighted above MatterAI** in the score.
- **Rework window starts at first *human* review** — bot comments arrive near-instantly and would zero out the rework signal.
- **Classification is a separate `/pr-quality` pass, NOT folded into `/rollup`.** Reuses rollup's dump→classify→apply machinery but stays its own skill: different unit (per-comment vs per-subject), different table (`pr_comment_class` vs `subject_summary`), narrower scope (github review/comment bodies on merged PRs only). May chain after `/rollup`, never merge into it.

## Open questions

None blocking. Per-repo "review-health" index is a possible later add-on.

## Build plan (phased)

Mirrors existing patterns: ingest (raw API, no LLM) → migration → pure-function derive module → dump/classify/apply skill. Each phase independently shippable + testable.

### Data model (Migration `009_pr_quality.sql`)

Three new tables in `events.db` (additive, via `_add_column_if_missing` / `CREATE TABLE IF NOT EXISTS` in `ingest/common.py::_ensure_schema`):
- `pr_meta` — keyed by subject (`owner/repo#num`): additions, deletions, files_changed, checks_status, checks_failed_json, labels_json, is_draft. PR-level facts that don't fit the append-only event log.
- `pr_comment_class` — keyed by event_id: category, source (`human`|`matterai`), confidence. One row per classified review/comment.
- `pr_friction` — keyed by subject: score 0-100, dominant_category, mechanical_json (rounds, rework, ttm), category_counts_json, computed_at.

### Phase 1 — Ingest: diff stats + CI (no LLM)
- Extend `ingest/github.py`: aggregate per-file `additions`/`deletions`/count (already in PR payload) → `pr_meta`. Add labels + draft flag.
- CI fetch: `/repos/{repo}/commits/{sha}/check-runs` per PR head → status + failing-check names → `pr_meta`. (Only genuinely new API call; watch rate limit on 705-PR backfill — batch + respect remaining headers.)
- Forward capture is free: github-ingest LaunchAgent fires every 30min.

### Phase 2 — Backfill historical PRs
- One-shot re-walk of the 705 merged PRs → populate `pr_meta` (diff stats + CI).
- Owner-invoked, idempotent (upsert on subject). No LLM.

### Phase 3 — `derive/github_metrics.py` (pure functions, no LLM)
- Peer to `derive/jira_metrics.py`; reuses its `load_people_lookup` for github-login → canonical.
- Exposes: `first_human_review_ts()` (excludes `matterai-example-org[bot]`), `mechanical_signals(pr)` (rounds, changes-requested cycles, rework commits after first human review, ttm), `compute_friction(pr)`, `aggregate_by_dev()`, `coverage_gap()` (human vs matterai buckets).
- Single source of truth; skill consumes, never reimplements.

### Phase 4 — `/pr-quality` skill: classify (dump → chat → apply)
Standalone skill (not a `/rollup` sub-phase); reuses the rollup harness.
- **Dump** (`derive/pr_quality_dump.py`): emit unclassified review/comment bodies (human + matterai) → `state/pending_pr_comments.json` + `state/pending_pr_comments.rules.md` (the taxonomy). No API key in script.
- **Classify** (chat turn): tag each comment with taxonomy category → `state/verdicts.pr_comments.json`.
- **Apply** (`derive/apply_pr_classes.py`): upsert `pr_comment_class`, then recompute `pr_friction` (mechanical + category-weighted, human > matterai).
- Rules file: `config/pr_review_rules.md` (taxonomy + weights, peer to rules.md).

### Phase 5 — `/pr-quality` skill: report
- Command file `.claude/commands/pr-quality.md`, flexible date range.
- Renders: per-PR friction lines, per-dev coaching actionables, human-vs-agentic coverage gap + bridging suggestions, trend over time.
- Output obeys skill-format rules (not chat-reply style).

### Sequencing notes
- Phases 1-3 are mechanical → land + validate before any LLM work.
- Phase 4 classification runs incrementally (only new/unclassified comments).
- `pr_friction` is recomputed on apply → mechanical-only scores exist from Phase 3, sharpen once classification lands.

## Success criteria

- Every merged PR gets a friction score + dominant reason.
- Can name the top 3 recurring mechanical friction reasons per repo.
- A clear, defensible shortlist of friction types agentic review can take over.
- A human-vs-agentic coverage-gap profile per category, with concrete bridging suggestions.
- Per-dev coaching actionables grounded in real reviewer comments (not a ranking).

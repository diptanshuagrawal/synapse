# PRD — Feature Narrative + Feature Score

**Status:** Draft (scoping)
**Owner:** owner
**Created:** 2026-06-08
**Related:** `prd/pr-quality-scorer.md` (scoring precedent), `.claude/commands/ask.md` (people-narrative precedent)

---

## 1. Summary

Two coupled capabilities, subject = **a feature** (not a person):

1. **Feature narrative** — the end-to-end delivery journey of a feature, time-ordered across its lifecycle stages: **planning → TRD → code dev → rollout**. States *when* and *what* happened at each stage, with linked artefacts.
2. **Feature Score** — a composite, normalized score over multiple delivery-health metrics (quality, test coverage, post-deploy bugs, speed, delays, impact). Designed to be **comparable across all features org-wide**, not a one-off readout.

The narrative is largely a re-skin of the existing `/ask` people path. The Feature Score is the hard part: org-wide comparability demands stable, automatic, well-defined metrics that hold across teams.

**Non-goal (v1):** real-time production telemetry (RPS/latency/error-rate). Those aren't in `events.db` today (see §8 gaps). v1 scores from what we have; impact is text-derived.

---

## 2. What already exists (reuse, don't rebuild)

- **`events.db`** — append-only `events` table across Jira / GitHub / Slack / Confluence. Every row has `source, event_type, ts, actor, subject, title, body, url` + source-specific columns (`issue_type, story_points, sprint_*, to_status, assignee`, PR fields, `thread_ts`, etc.). Timeline reconstruction is already possible from `ts` + `event_type`.
- **Feature grouping** — `projects.yaml` slugs map a feature to `{jira_epics[], confluence_pages[], keywords[]}`. `cluster_project_map` bridges embedding clusters → slugs. `topic_brief` / `topic_brief_member` group subjects into clusters.
- **Epic-first classification** — only `issue_type==Epic` auto-creates slugs; child tickets/PRs link to the slug. `epic_domain` field is the canonical feature anchor.
- **CMR release records (the rollout signal)** — `issue_type='CMR'` tickets ARE the release events. ~724 CMRs captured. A structured subset (~28%, ~200 CMRs) follows a parseable template in the body: `Service: … PR: <github url> Impacted Areas: … Owner of release: …`. The **PR link is the hard cross-stage glue** (rollout → code dev → feature). Universal across all CMRs: a real release lifecycle in `status_change.to_status` — `Approval Requested → Awaiting Approvals → Change Approved → Released` (and `Released with Emergency`, `Rolled Back`, `Cancelled`). Owner approval is captured as a `comment` ("Approved") + Automation `status_change` to `Change Approved` with timestamp + actor. Release channels `release-service-c` and `example-releases` are already in `config/slack_channels.yaml` and ingested. **Non-feature CMRs (DB ops, balance fixes, config) should be filtered** — consistent with existing ops-detection in `jira_metrics.py`.
- **PR-quality scorer** (`prd/pr-quality-scorer.md`, `derive/pr_quality_*.py`, `derive/migrations/009_pr_quality.sql`) — already computes per-PR friction: `pr_meta` (diff stats, CI status), `pr_comment_class` (root-cause categories), `pr_friction` (0–100 composite, weighted). **This is the test-quality + code-quality feeder for the Feature Score.**
- **`/ask` narrative contract** — locked section order, plain-English-only output (hard pre-save grep-check forbidding internal entity names). Feature narrative inherits this contract.
- **Conventions** — scripts strip Anthropic auth (chat does all LLM work); deterministic mechanical metrics in Python; chat-only classification.

---

## 3. Subject resolution: "a feature" → artefact set

Input can be (a) a Jira Epic key, (b) a `projects.yaml` slug, or (c) a natural-language topic.

Resolution chain (mirrors `dump_pending.py` / `link_clusters_to_projects.py`):

1. **Epic key** → find its slug via `projects.yaml::jira_epics`. If unmapped, run `/slug-epics` first.
2. **Slug** → direct.
3. **NL topic** → match `topic_brief.label/summary`, hop to `cluster_project_map.project_slug` (confidence ≥ 0.60), disambiguate with owner if multiple.

Reverse resolution (slug → all artefacts):
- **Jira:** epic + all child tickets (`parent_epic_key ∈ jira_epics[slug]`).
- **Confluence:** `confluence_pages[slug]` page IDs.
- **PRs:** subjects matching `…#N` in clusters mapped to the slug.
- **Slack:** `slack:…` subjects in those clusters.

**Open decision D1:** slug-grouping is "soft" (a subject can land in multiple clusters). Do we trust cluster membership for PR/Slack attribution, or require a harder Jira-link (PR body references `EX-NNNN` ∈ epic children)? Recommend **Jira-link as primary, cluster as fallback** for precision.

---

## 4. Stage model

Four stages. Each needs an automatic detection rule and a "stage entered" timestamp so the narrative is time-ordered and the score can measure stage durations (speed/delays).

| Stage | Detection signal | Entered-at timestamp | Confidence |
|---|---|---|---|
| **Planning** | `issue_created` where `issue_type='Epic'` for the slug; first child tickets created | epic `created_at` | High |
| **TRD / design** | Confluence page in `confluence_pages[slug]` created/updated; `trd_owners` row exists | earliest `page_created.ts` | Medium (depends on page being linked) |
| **Code dev** | first `pr_opened` on a PR linked to an epic child ticket | first `pr_opened.ts` | High |
| **Rollout** | **CMR ticket** (`issue_type='CMR'`) linked to the feature via PR-in-body or Impacted-Areas keyword; `status_change.to_status='Released'` (or `Released with Emergency`) | CMR `Released` `status_change.ts` | **High** (real release record, not a proxy) |

The CMR is the rollout artefact and it is rich:
- **Real release timestamp** = the `Released` `status_change.ts` — actual go-live, not "code merged".
- **Owner-approval timestamp + actor** = the `Change Approved` transition (preceded by the owner's "Approved" comment).
- **Approval latency** = `Approval Requested` ts → `Change Approved` ts.
- **Release-health flags** = `Released with Emergency`, `Rolled Back`, `Cancelled`, `Reopened` — direct negative-quality signals.
- **PR link in body** ties the release back to the exact PR(s) and thus to the feature.

**Stage gaps (must be stated honestly in narrative + score):**
- ~28% of CMRs use the structured `Service/PR/Impacted Areas` template (parseable + PR-linked). The rest (DB ops, balance fixes, config) are mostly non-feature ops CMRs — **filter them out**, don't score them as feature releases.
- CMR → feature attribution: structured CMRs link via PR→epic-child or Impacted-Areas→`projects.yaml` keywords. Unstructured CMRs may not attribute cleanly → render "release record not linked" rather than guess.
- TRD stage invisible if the TRD page isn't linked to the slug (`confluence_pages[]` empty for that slug). Narrative must render "TRD: not detected" rather than silently skip.
- Still no *numeric* production telemetry (RPS/latency/error-rate) — release happened and whether it rolled back is known, but quantitative prod impact is not. M6 stays text/lifecycle-derived in v1.

**Decision D2 — RESOLVED:** rollout is real, via CMR lifecycle. No separate deploy-feed ingest needed for v1. (A numeric prod-telemetry feed remains a v2 fidelity opportunity for M6 only.)

---

## 5. Deliverable 1 — Feature narrative

Re-skin of the `/ask` people path. Same engine shape (a `feature_v1.py` mechanical engine + a `feature_deepread.py` citation bundler), same locked output contract, same plain-English grep-check.

### Output structure (locked order)
1. **TL;DR** — 5–6 bullets, ≤25 words, most consequential first (e.g. "shipped in 6 weeks, 1 sprint slip", "3 post-deploy bugs, all P3", "Feature Score 72/100 — mid-band").
2. **Timeline** — the journey, one block per stage (Planning / TRD / Code dev / Rollout), each with entered-at date, duration, and what happened. Missing stage → explicit "not detected".
3. **Scope shipped** — tickets, PRs, SP, contributors (by artefact, never by cluster ID).
4. **Quality signals** — review friction (from `pr_friction`), test-gap category density, post-deploy bugs.
5. **Speed & delays** — stage durations vs baseline; sprint slips.
6. **Feature Score** — the composite + per-metric breakdown (see §6).
7. **Data silent on** — what we can't say (e.g. real prod impact).
8. **Detail** — narrative paragraphs with inline artefact links (ticket IDs, PR URLs, doc titles, Slack threads).
9. **Confirmed by data** — audit trail of every claim → artefact.

### Inherited hard rules
- **Plain-English only.** Pre-save grep-check forbidding: `cluster/cluster_id`, table names (`events.db/topic_brief/pr_friction`), engine names (`feature_v1/feature_deepread`), JSON field paths, metric keys (`_pct/_json`). Any match = rewrite.
- **Completeness gate.** Every score metric rendered, even if "not computed (n=…)".
- Express scope via real artefacts only.

---

## 6. Deliverable 2 — Feature Score

Composite 0–100, higher = healthier delivery. Built from sub-scores, each independently defined, normalized, and source-cited. **Comparability across features is the design constraint** — every metric must be computable automatically for *any* feature and normalized to remove size/team bias.

### 6.1 Metric definitions

| # | Metric | Definition | Data source | Normalization |
|---|---|---|---|---|
| M1 | **Code quality** | Mean `pr_friction.score` across the feature's PRs (inverted: lower friction = higher quality) | `pr_friction` (009 schema) | Already 0–100 per PR; size-normalized by LOC in `pr_meta` |
| M2 | **Test coverage** | Share of PRs with NO `test-gap` comment + presence of test files in diff | `pr_comment_class.category='test-gap'`; `pr_meta.files_changed`/labels | % of PRs clean of test-gap |
| M3 | **Post-deploy bugs** | Count of `issue_type='Bug'` tickets created within 15d after the CMR `Released` date, linked to the feature | `events` Bug issues; rollout anchor = CMR `Released` ts | Per-bug severity-weighted, normalized by feature size (SP or PR count) |
| M4 | **Speed** | Total cycle time: planning-entered → CMR `Released` ts | stage timestamps (§4), CMR release ts | Percentile vs all features of similar SP band |
| M5 | **Delays** | Sprint slips: # sprints the epic's children spanned vs planned; SP carried across `sprint_state` CLOSED→next | `sprint_id`, `sprint_state`, story_points | Slip ratio vs baseline |
| M6 | **Impact** | Text-derived rollout outcome (positive observations, adoption mentions) — **v1 weak, qualitative** | Slack post-rollout threads, chat-classified | Banded (low/med/high), low weight in v1 |
| M7 | **Release health** | CMR lifecycle: rollback rate, emergency-release rate, cancellation/reopen rate, approval latency (`Approval Requested`→`Change Approved`) | CMR `status_change.to_status` transitions | Penalty per `Rolled Back`/`Released with Emergency`/`Cancelled`; normalized per release count |

### 6.2 Composite

```
FeatureScore = Σ ( w_i · subscore_i )   over metrics with sufficient data
```

- Default weights (tunable, stored in `config/feature_score_rules.md` — source of truth, like `pr_review_rules.md`):
  - M1 quality 0.20, M2 test 0.15, M3 post-deploy bugs 0.20, M4 speed 0.10, M5 delays 0.10, M6 impact 0.05, M7 release-health 0.20.
  - (M7 weighted high — a rollback/emergency release is the strongest available negative-delivery signal.)
- **Insufficient-data handling (critical for fairness):** if a metric can't be computed (e.g. no TRD, no post-deploy window elapsed yet), it is **excluded and weights re-normalized over the present metrics** — never scored as 0. Narrative states which metrics were excluded and why.
- Score is banded for readability: 80–100 strong / 60–79 mid / <60 needs-attention. Raw number always shown alongside.

### 6.3 Comparability requirements (the org-wide constraint)
- Every metric must be **automatically computable** for any feature — no manual inputs.
- Every metric must be **size-normalized** (PR count / SP / LOC) so a 50-PR feature isn't punished vs a 3-PR feature.
- Speed/delays are **percentile-ranked within an SP band**, not absolute, so a large feature isn't penalized for taking longer.
- Weights live in config, versioned, single source of truth — so a re-weighting re-scores all features consistently.
- **D3 — RESOLVED:** canonical score = snapshot at **CMR `Released` + 15 days**. Post-deploy-bug window (M3) = 15d. If <15d have elapsed since release, score is marked `provisional` and M3 excluded (re-normalized) until the window matures.

---

## 7. Data model additions

New tables mirroring the `pr_*` table pattern. `feature_release` (011) and `feature_stage` (012) are built; `feature_score` and `feature_bug_link` remain Phase 2 (future migration). DDL is the canonical reference under `derive/migrations/`; the applied copy lives in `ingest/common.py::_ensure_schema` (no external migration runner):

- **`feature_stage`** (keyed by `slug, scope, stage` — DONE, migration `012_feature_stage.sql`): `entered_at`, `detection_source`, `confidence`, `artefact_count`, `detail_json`, `computed_at`. One row per stage per feature; `scope=''` is the whole-slug domain rollup, a non-empty `scope` is an anchor epic key (epic-bounded journey).
- **`feature_score`** (keyed by `slug, snapshot`): `composite` (0–100), `band`, `subscores_json` ({metric: {raw, normalized, weight, n, excluded}}), `metrics_present_json`, `window_days`, `computed_at`.
- **`feature_bug_link`** (keyed by `bug_subject`): `slug`, `linked_via` (epic_child | keyword | cluster), `created_at`, `severity`, `days_after_rollout`. Feeds M3.
- **`feature_release`** (keyed by `cmr_subject, slug` — DONE, migration `011_feature_release.sql`): `slug`, `linked_via` (project_ref | impacted_areas | none), `service`, `impacted_areas`, `pr_urls_json`, `release_owner`, `created_at`, `approval_requested_at`, `approved_at`, `approved_by`, `released_at`, `outcome` (released | emergency | rolled_back | cancelled | pending), `is_feature_release` (0 for DB-ops/balance/config CMRs). A release touching N features yields N rows; unattributed CMRs get a single `slug=''` row. Feeds M7, the rollout stage, and M3/M4 anchoring.

Reuse existing `pr_meta`, `pr_comment_class`, `pr_friction` for M1/M2 — no duplication.

---

## 8. Architecture / pipeline

Follows existing conventions (scripts deterministic + auth-stripped; chat does LLM classification only):

1. **`derive/feature_resolve.py`** — slug → artefact set (§3). Pure SQL/yaml.
2. **`derive/cmr_releases.py`** — parse CMR tickets into `feature_release`: extract `Service/PR/Impacted Areas/Owner` from structured bodies, walk `status_change` transitions for approval/release/rollback timestamps, flag `is_feature_release`, attribute to a slug via PR→epic-child or Impacted-Areas keyword. Deterministic.
3. **`derive/feature_stages.py`** — compute `feature_stage` rows from event timeline (rollout stage reads `feature_release`). Deterministic.
4. **`derive/feature_score.py`** — compute `feature_score` from `pr_friction` + stages + `feature_release` (M7) + bug links + sprint data. Deterministic, weights from `config/feature_score_rules.md`.
5. **`derive/feature_v1.py` + `feature_deepread.py`** — narrative engines (mechanical signal pack + citation bundle), mirroring `person_v3.py` / `person_deepread.py`.
6. **`.claude/commands/feature.md`** (new skill, e.g. `/feature <slug-or-epic>`) — orchestrates: resolve → stages → score → render narrative under the locked contract with pre-save grep-check.
7. *(deferred — not v1)* **Org-wide batch** — `derive/feature_score_all.py` over every slug. Per D6, v1 scores **only the feature(s) passed as argument** to `/feature`; no automatic org-wide pass. The schema (`feature_score` per slug+snapshot) already supports accumulating a comparable corpus on-demand, so a batch/leaderboard can be added later without rework.

### Gaps carried from data layer (state in PRD, don't hide)
- **G1 — RESOLVED:** rollout timing comes from the CMR `Released` `status_change.ts` (real release record + owner approval + PR link + rollback/emergency flags). Only ~28% of CMRs are structured/PR-linked; unstructured ops CMRs are filtered. Remaining v2 opportunity is *numeric* prod telemetry for M6 only.
- **G2:** No numeric production telemetry (RPS/latency/errors) → M6 impact is text-derived and weak in v1.
- **G3:** MatterAI comment pre-classification is GitHub-only and only since 2025-11-07 → older features lean on chat classification for M1/M2.
- **G4:** Cluster membership is soft → PR/Slack attribution should prefer hard Jira-links (D1).
- **G5 — epic→child link exists but is title-embedded:** the Jira ingest prefixes events with `[Epic <key>]` (via `ingest/jira.py::get_epic_key`) but never stores it structurally. `feature_resolve.epic_children()` recovers it by title regex. A future cleanup could persist it as `event_refs(ref_type='epic_parent')` for speed/cleanliness.
- **G6 — CMRs don't child to feature epics:** epic-prefixed CMRs roll up to the umbrella "EX Releases" epic (EX-185, 216 release CMRs), not to the feature epic they implement. So a feature epic's *releases* can't be isolated by the epic link; epic mode bounds the slug's releases by the epic's creation date instead. PR↔ticket refs are too sparse (375, commit-level, cross-project) to bridge this. Hardening would need either a release→epic field at CMR-creation time, or a richer PR↔ticket↔epic link.

---

## 9. Phasing

- **Phase 1 — Narrative (no score): DONE.** `cmr_releases.py` (feature_release) + `feature_resolve.py` (slug + epic modes) + `feature_stages.py` (4-stage timeline, scope-aware) + `feature_narrative.py` (markdown render → derived/features/). The dated release stream is the delivery spine; stages are a summary. Validated on real data.

  Build findings (see §8 G5/G6):
  - Epic→child link recovered free from the `[Epic <key>]` title prefix the Jira ingest already embeds — **no new ingest needed**. ~63% of tickets carry it.
  - CMRs mostly child to one umbrella "EX Releases" epic (EX-185), not to feature epics → **per-epic release isolation is not in the data**. Epic mode falls back to the slug's releases bounded by the epic's start date (works well for recent single-deliverable epics; degrades to a domain view for thin epics — flagged in the render's "Data silent on").
- **Phase 2 — Score (the feature passed in):** add `feature_score` over M1–M5 + M7 (defer M6), config-driven weights, insufficient-data re-normalization, snapshot at release+15d. `/feature <slug-or-epic>` shows narrative + score for that feature only.
- **Phase 3 (deferred) — Org-wide batch:** cross-feature comparison/leaderboard over all slugs. Not v1 (per D6).
- **Phase 4 (v2) — Fidelity:** ingest numeric prod telemetry (G2) to harden M6.

---

## 10. Open decisions (need owner input before build)

- **D1** — PR/Slack attribution: hard Jira-link primary vs cluster membership. (Recommend hard-link primary.)
- **D2 — RESOLVED:** rollout = CMR `Released` lifecycle (no separate deploy-feed needed for v1).
- **D3 — RESOLVED:** snapshot at CMR `Released` + 15 days; `provisional` if window not yet matured.
- **D4** — Default metric weights in §6.2 — owner to confirm/adjust.
- **D5** — Score banding thresholds (80/60) — owner to confirm.
- **D6 — RESOLVED:** score only the feature(s) passed as argument to `/feature`; no automatic org-wide pass in v1.

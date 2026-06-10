# PRD — Feature Narrative + Feature Score

**Status:** Draft (scoping) · **Owner:** owner · **Created:** 2026-06-08
**Related:** `prd/pr-quality-scorer.md` (scoring precedent), `.claude/commands/ask.md` (people-narrative precedent)

---

## 1. Summary

Two coupled capabilities where **subject = a feature** (not a person):

1. **Feature narrative** — end-to-end delivery journey, time-ordered across **planning → TRD → code dev → rollout**; *when*/*what* per stage with linked artefacts. Re-skin of the `/ask` people path.
2. **Feature Score** — composite 0–100 over delivery-health metrics (quality, test coverage, post-deploy bugs, speed, delays, impact). Hard part: **comparable across all features org-wide** → needs stable, automatic, well-defined metrics that hold across teams.

**Non-goal (v1):** real-time prod telemetry (RPS/latency/error-rate) — not in `events.db` today (§8). v1 scores from what we have; impact is text-derived.

---

## 2. What already exists (reuse, don't rebuild)

- **`events.db`** — append-only `events` table across Jira / GitHub / Slack / Confluence. Columns: `source, event_type, ts, actor, subject, title, body, url` + source-specific (`issue_type, story_points, sprint_*, to_status, assignee`, PR fields, `thread_ts`, …). Timeline reconstructable from `ts` + `event_type`.
- **Feature grouping** — `projects.yaml` slugs map a feature → `{jira_epics[], confluence_pages[], keywords[]}`. `cluster_project_map` bridges embedding clusters → slugs. `topic_brief` / `topic_brief_member` group subjects into clusters.
- **Epic-first classification** — only `issue_type==Epic` auto-creates slugs; child tickets/PRs link to the slug. `epic_domain` = canonical feature anchor.
- **PR-quality scorer** (`prd/pr-quality-scorer.md`, `derive/pr_quality_*.py`, `derive/migrations/009_pr_quality.sql`) — per-PR friction already computed: `pr_meta` (diff stats, CI status), `pr_comment_class` (root-cause categories), `pr_friction` (0–100 weighted composite). **This is the test-quality + code-quality feeder for the Feature Score.**
- **`/ask` narrative contract** — locked section order, plain-English-only output (pre-save grep-check forbids internal entity names). Feature narrative inherits it.
- **Conventions** — scripts strip Anthropic auth (chat does all LLM work); deterministic mechanical metrics in Python; chat-only classification.

### CMR release records (the rollout signal)
- `issue_type='CMR'` tickets ARE the release events. ~724 CMRs captured.
- ~28% (~200) follow a parseable body template: `Service: … PR: <github url> Impacted Areas: … Owner of release: …`. **The PR link is the hard cross-stage glue** (rollout → code dev → feature).
- Universal across all CMRs: a real release lifecycle in `status_change.to_status` — `Approval Requested → Awaiting Approvals → Change Approved → Released` (also `Released with Emergency`, `Rolled Back`, `Cancelled`, `Reopened`).
- Owner approval = `comment` ("Approved") + Automation `status_change` to `Change Approved` (ts + actor).
- Release channels `release-service-c`, `example-releases` are in `config/slack_channels.yaml` and ingested.
- **Filter non-feature CMRs** (DB ops, balance fixes, config) — consistent with ops-detection in `jira_metrics.py`.

---

## 3. Subject resolution: "a feature" → artefact set

Input: (a) Jira Epic key, (b) `projects.yaml` slug, or (c) NL topic. Resolution mirrors `dump_pending.py` / `link_clusters_to_projects.py`:

1. **Epic key** → slug via `projects.yaml::jira_epics`. If unmapped, run `/slug-epics` first.
2. **Slug** → direct.
3. **NL topic** → match `topic_brief.label/summary` → hop to `cluster_project_map.project_slug` (confidence ≥ 0.60); disambiguate with owner if multiple.

Reverse (slug → all artefacts):
- **Jira:** epic + child tickets (`parent_epic_key ∈ jira_epics[slug]`).
- **Confluence:** `confluence_pages[slug]` page IDs.
- **PRs:** subjects matching `…#N` in clusters mapped to the slug.
- **Slack:** `slack:…` subjects in those clusters.

**D1 (open):** slug-grouping is soft (a subject can land in multiple clusters). Trust cluster membership for PR/Slack attribution, or require a harder Jira-link (PR body references `EX-NNNN` ∈ epic children)? Recommend **Jira-link primary, cluster fallback** for precision.

---

## 4. Stage model

Four stages, each with an auto-detection rule + "entered-at" timestamp (drives time-ordering and stage-duration metrics).

| Stage | Detection signal | Entered-at | Confidence |
|---|---|---|---|
| **Planning** | `issue_created` where `issue_type='Epic'` for slug; first child tickets | epic `created_at` | High |
| **TRD / design** | Confluence page in `confluence_pages[slug]` created/updated; `trd_owners` row exists | earliest `page_created.ts` | Medium (needs page linked) |
| **Code dev** | first `pr_opened` on a PR linked to an epic child | first `pr_opened.ts` | High |
| **Rollout** | **CMR** (`issue_type='CMR'`) linked via PR-in-body or Impacted-Areas keyword; `to_status='Released'` (or `Released with Emergency`) | CMR `Released` `status_change.ts` | **High** (real release record) |

The CMR is the rollout artefact and is rich:
- **Real release ts** = `Released` `status_change.ts` (actual go-live, not "code merged").
- **Owner-approval ts + actor** = `Change Approved` transition (preceded by owner's "Approved" comment).
- **Approval latency** = `Approval Requested` ts → `Change Approved` ts.
- **Release-health flags** = `Released with Emergency`, `Rolled Back`, `Cancelled`, `Reopened` (direct negative-quality signals).
- **PR link in body** ties release → exact PR(s) → feature.

**Stage gaps (state honestly in narrative + score):**
- ~28% of CMRs are structured/PR-linked; the rest (DB ops, balance, config) are non-feature ops — **filter, don't score**.
- CMR→feature attribution: structured CMRs link via PR→epic-child or Impacted-Areas→`projects.yaml` keywords. Unstructured may not attribute → render "release record not linked", don't guess.
- TRD stage invisible if page not linked (`confluence_pages[]` empty) → render "TRD: not detected", don't skip silently.
- No *numeric* prod telemetry (RPS/latency/error-rate). M6 stays text/lifecycle-derived in v1.

**D2 — RESOLVED:** rollout is real via CMR lifecycle; no separate deploy-feed ingest for v1. (Numeric prod-telemetry feed = v2 fidelity for M6 only.)

---

## 5. Deliverable 1 — Feature narrative

Re-skin of `/ask` people path. Same engine shape (`feature_v1.py` mechanical engine + `feature_deepread.py` citation bundler), same locked contract, same plain-English grep-check.

### Output structure (locked order)
1. **TL;DR** — 5–6 bullets, ≤25 words, most consequential first (e.g. "shipped in 6 weeks, 1 sprint slip", "3 post-deploy bugs, all P3", "Feature Score 72/100 — mid-band").
2. **Timeline** — one block per stage (Planning / TRD / Code dev / Rollout): entered-at date, duration, what happened. Missing stage → explicit "not detected".
3. **Scope shipped** — tickets, PRs, SP, contributors (by artefact, never by cluster ID).
4. **Quality signals** — review friction (`pr_friction`), test-gap category density, post-deploy bugs.
5. **Speed & delays** — stage durations vs baseline; sprint slips.
6. **Feature Score** — composite + per-metric breakdown (§6).
7. **Data silent on** — what we can't say (e.g. real prod impact).
8. **Detail** — narrative paragraphs with inline artefact links (ticket IDs, PR URLs, doc titles, Slack threads).
9. **Confirmed by data** — audit trail of every claim → artefact.

### Inherited hard rules
- **Plain-English only.** Pre-save grep-check forbids: `cluster/cluster_id`, table names (`events.db/topic_brief/pr_friction`), engine names (`feature_v1/feature_deepread`), JSON field paths, metric keys (`_pct/_json`). Any match = rewrite.
- **Completeness gate.** Every score metric rendered, even if "not computed (n=…)".
- Express scope via real artefacts only.

---

## 6. Deliverable 2 — Feature Score

Composite 0–100, higher = healthier delivery. Built from independently-defined, normalized, source-cited sub-scores. **Comparability across features is the design constraint** — every metric auto-computable for *any* feature and normalized to remove size/team bias.

### 6.1 Metric definitions

| # | Metric | Definition | Data source | Normalization |
|---|---|---|---|---|
| M1 | **Code quality** | Mean `pr_friction.score` across feature PRs (inverted: lower friction = higher) | `pr_friction` (009) | Already 0–100/PR; LOC-normalized via `pr_meta` |
| M2 | **Test coverage** | Share of PRs with NO `test-gap` comment + test files in diff | `pr_comment_class.category='test-gap'`; `pr_meta.files_changed`/labels | % of PRs clean of test-gap |
| M3 | **Post-deploy bugs** | Count of `issue_type='Bug'` created within 15d after CMR `Released`, linked to feature | `events` Bug issues; anchor = CMR `Released` ts | Severity-weighted, normalized by feature size (SP or PR count) |
| M4 | **Speed** | Cycle time: planning-entered → CMR `Released` ts | stage timestamps (§4), CMR release ts | Percentile vs features of similar SP band |
| M5 | **Delays** | Sprint slips: # sprints epic children spanned vs planned; SP carried `sprint_state` CLOSED→next | `sprint_id`, `sprint_state`, story_points | Slip ratio vs baseline |
| M6 | **Impact** | Text-derived rollout outcome (positive observations, adoption mentions) — **v1 weak, qualitative** | Slack post-rollout threads, chat-classified | Banded (low/med/high), low weight in v1 |
| M7 | **Release health** | CMR lifecycle: rollback rate, emergency-release rate, cancel/reopen rate, approval latency (`Approval Requested`→`Change Approved`) | CMR `status_change.to_status` | Penalty per `Rolled Back`/`Released with Emergency`/`Cancelled`; normalized per release count |

### 6.2 Composite

```
FeatureScore = Σ ( w_i · subscore_i )   over metrics with sufficient data
```

- Default weights (tunable, stored in `config/feature_score_rules.md` = source of truth, like `pr_review_rules.md`):
  M1 quality 0.20 · M2 test 0.15 · M3 post-deploy bugs 0.20 · M4 speed 0.10 · M5 delays 0.10 · M6 impact 0.05 · M7 release-health 0.20.
  (M7 weighted high — a rollback/emergency release is the strongest available negative-delivery signal.)
- **Insufficient-data handling (critical for fairness):** uncomputable metric (no TRD, post-deploy window not elapsed) is **excluded and weights re-normalized over present metrics** — never scored 0. Narrative states which metrics excluded and why.
- **Banding:** 80–100 strong / 60–79 mid / <60 needs-attention. Raw number always shown.

### 6.3 Comparability requirements (org-wide constraint)
- Every metric **auto-computable** for any feature — no manual inputs.
- Every metric **size-normalized** (PR count / SP / LOC) so a 50-PR feature isn't punished vs a 3-PR feature.
- Speed/delays **percentile-ranked within an SP band**, not absolute.
- Weights live in config, versioned, single source of truth → re-weighting re-scores all features consistently.
- **D3 — RESOLVED:** canonical score = snapshot at **CMR `Released` + 15 days**. M3 post-deploy-bug window = 15d. If <15d elapsed, score marked `provisional` and M3 excluded (re-normalized) until window matures.

---

## 7. Data model additions

New tables mirror the `pr_*` pattern. `feature_release` (011) + `feature_stage` (012) are **built**; `feature_score` + `feature_bug_link` are **Phase 2** (future migration). DDL canonical under `derive/migrations/`; applied copy lives in `ingest/common.py::_ensure_schema` (no external migration runner).

- **`feature_stage`** (key `slug, scope, stage` — DONE, `012_feature_stage.sql`): `entered_at`, `detection_source`, `confidence`, `artefact_count`, `detail_json`, `computed_at`. One row per stage per feature; `scope=''` = whole-slug domain rollup, non-empty `scope` = anchor epic key (epic-bounded journey).
- **`feature_release`** (key `cmr_subject, slug` — DONE, `011_feature_release.sql`): `slug`, `linked_via` (project_ref | impacted_areas | none), `service`, `impacted_areas`, `pr_urls_json`, `release_owner`, `created_at`, `approval_requested_at`, `approved_at`, `approved_by`, `released_at`, `outcome` (released | emergency | rolled_back | cancelled | pending), `is_feature_release` (0 for DB-ops/balance/config). A release touching N features → N rows; unattributed CMRs → single `slug=''` row. Feeds M7, rollout stage, M3/M4 anchoring.
- **`feature_score`** (key `slug, snapshot`): `composite` (0–100), `band`, `subscores_json` ({metric: {raw, normalized, weight, n, excluded}}), `metrics_present_json`, `window_days`, `computed_at`.
- **`feature_bug_link`** (key `bug_subject`): `slug`, `linked_via` (epic_child | keyword | cluster), `created_at`, `severity`, `days_after_rollout`. Feeds M3.

Reuse existing `pr_meta`, `pr_comment_class`, `pr_friction` for M1/M2 — no duplication.

---

## 8. Architecture / pipeline

Follows conventions (scripts deterministic + auth-stripped; chat does LLM classification only):

1. **`derive/feature_resolve.py`** — slug → artefact set (§3). Pure SQL/yaml.
2. **`derive/cmr_releases.py`** — parse CMRs into `feature_release`: extract `Service/PR/Impacted Areas/Owner` from structured bodies, walk `status_change` for approval/release/rollback ts, flag `is_feature_release`, attribute to slug via PR→epic-child or Impacted-Areas keyword. Deterministic.
3. **`derive/feature_stages.py`** — compute `feature_stage` rows from event timeline (rollout reads `feature_release`). Deterministic.
4. **`derive/feature_score.py`** — compute `feature_score` from `pr_friction` + stages + `feature_release` (M7) + bug links + sprint data. Deterministic, weights from `config/feature_score_rules.md`.
5. **`derive/feature_v1.py` + `feature_deepread.py`** — narrative engines (mechanical signal pack + citation bundle), mirror `person_v3.py` / `person_deepread.py`.
6. **`.claude/commands/feature.md`** (new skill, `/feature <slug-or-epic>`) — orchestrates resolve → stages → score → render under locked contract with pre-save grep-check.
7. *(deferred)* **`derive/feature_score_all.py`** — org-wide batch over every slug. Per D6, v1 scores **only the feature(s) passed to `/feature`**; no auto org-wide pass. Schema (`feature_score` per slug+snapshot) already supports accumulating a comparable corpus on-demand, so a batch/leaderboard adds later without rework.

### Gaps carried from data layer (state, don't hide)
- **G1 — RESOLVED:** rollout timing from CMR `Released` `status_change.ts` (real record + owner approval + PR link + rollback/emergency flags). Only ~28% structured/PR-linked; unstructured ops CMRs filtered. Remaining v2 opportunity = numeric prod telemetry for M6 only.
- **G2:** No numeric prod telemetry (RPS/latency/errors) → M6 text-derived and weak in v1.
- **G3:** MatterAI comment pre-classification is GitHub-only and only since 2025-11-07 → older features lean on chat classification for M1/M2.
- **G4:** Cluster membership soft → PR/Slack attribution should prefer hard Jira-links (D1).
- **G5 — epic→child link is title-embedded:** Jira ingest prefixes events with `[Epic <key>]` (`ingest/jira.py::get_epic_key`) but never stores it structurally. `feature_resolve.epic_children()` recovers it by title regex. Future cleanup: persist as `event_refs(ref_type='epic_parent')` for speed/cleanliness.
- **G6 — CMRs don't child to feature epics:** epic-prefixed CMRs roll up to umbrella "EX Releases" epic (EX-185, 216 release CMRs), not the feature epic. So a feature epic's *releases* can't be isolated by epic link; epic mode bounds the slug's releases by the epic's creation date instead. PR↔ticket refs too sparse (375, commit-level, cross-project) to bridge. Hardening needs a release→epic field at CMR-creation, or richer PR↔ticket↔epic link.

---

## 9. Phasing

**Phase 1 — Narrative (no score): DONE.** `cmr_releases.py` (feature_release) + `feature_resolve.py` (slug + epic modes) + `feature_stages.py` (4-stage timeline, scope-aware) + `feature_narrative.py` (markdown → derived/features/). Dated release stream = delivery spine; stages = summary. Validated on real data.
- Epic→child link recovered free from `[Epic <key>]` title prefix — **no new ingest needed**. ~63% of tickets carry it.
- CMRs mostly child to umbrella "EX Releases" epic (EX-185), not feature epics → **per-epic release isolation not in data**. Epic mode falls back to slug's releases bounded by epic start date (works for recent single-deliverable epics; degrades to domain view for thin epics — flagged in render's "Data silent on").

**Phase 2 — Score (the feature passed in):** add `feature_score` over M1–M5 + M7 (defer M6), config-driven weights, insufficient-data re-normalization, snapshot at release+15d. `/feature <slug-or-epic>` shows narrative + score for that feature only.

**Phase 3 (deferred) — Org-wide batch:** cross-feature comparison/leaderboard over all slugs. Not v1 (D6).

**Phase 4 (v2) — Fidelity:** ingest numeric prod telemetry (G2) to harden M6.

---

## 10. Open decisions (need owner input before build)

- **D1** — PR/Slack attribution: hard Jira-link primary vs cluster membership. (Recommend hard-link primary.)
- **D2 — RESOLVED:** rollout = CMR `Released` lifecycle (no separate deploy-feed for v1).
- **D3 — RESOLVED:** snapshot at CMR `Released` + 15 days; `provisional` if window not matured.
- **D4** — Default metric weights (§6.2) — owner to confirm/adjust.
- **D5** — Score banding thresholds (80/60) — owner to confirm.
- **D6 — RESOLVED:** score only the feature(s) passed to `/feature`; no auto org-wide pass in v1.

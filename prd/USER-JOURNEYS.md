# User Journey Audit — Engineering Management Copilot

**Date:** 2026-05-12 · **Companion to:** [`PRD.md`](PRD.md), [`work-context/ARCHITECTURE.md`](../work-context/ARCHITECTURE.md)

> **Per-story detail docs:** see [`PRD.md` §13 Story index](PRD.md#13-story-index) for links to detailed PRDs of each user-facing skill. Add a new row there whenever a journey graduates from concept to its own detail doc.

> **Slack note (per user FYI):** Slack ingest is desired but the ingestion strategy is not yet defined (channels-to-track? threads vs messages? bots-to-skip? team-filter?). Journeys that depend on Slack data are flagged **🔵 BLOCKED-ON-SLACK** rather than feasibility-rated, since their feasibility depends on the Slack strategy decision.

---

## Legend

| Marker | Meaning |
|--------|---------|
| ✅ | Works today — data + classification already in place |
| ➕ | Small extension (1-2 new fields / queries) — ≤1 day |
| ➕➕ | Medium extension (new event type or query path) — ≤1 week |
| ➕➕➕ | Major extension (new architecture / source) — ≥1 week |
| 🔵 | Blocked on Slack ingest strategy |
| ⛔ | Out of scope — different product |

---

## Current data inventory (recap)

What `events.db` carries today, in one glance — every journey below presumes this baseline:

**Per event:** `id, source ∈ {github,jira,confluence}, event_type, ts (UTC ISO8601), actor, subject, title, body, url, refs{people,projects,tickets,pages}, raw_path, issue_type` (jira only).

**Event types:**
- github: `pr_opened`, `pr_merged`, `pr_closed`, `pr_merged_by`, `review`, `comment`, `commit_in_pr`, `commit_pushed`
- jira: `issue_created`, `status_change`, `assignment`, `comment`
- confluence: `page_created`, `page_updated`, `comment`

**Derived caches:**
- `subject_summary` — `(subject, content_hash) → (domains, summary, risk_flags, confidence)` — chat-classified or keyword-fallback
- `person_narrative` — `(actor, window_days, content_hash) → markdown body`

**Pre-computed outputs:**
- `derived/people/<handle>.md` — per-person 240d profile
- `derived/projects/<slug>.md` — per-domain rollup
- `derived/weekly/<YYYY-Wnn>.md` — weekly volume + cycle time + review coverage
- `derived/alerts.md` — stale PRs (≥7d) + drive-by merges (last 30d no human review)

**Identity map:** `config/people.yaml` — github/email/jira_id/git_name per person.

**Domain map:** `config/projects.yaml` — 88 slugs each with `keywords`, `jira_epics`, `jira_prefixes`, `confluence_pages`.

---

## 1. Daily EM operations

### 1.1 Morning standup prep ✅
**Trigger**: EM opens chat session at start of day.
**Inputs (have)**: `alerts.md` (stale PRs, drive-by merges), `weekly/<current>.md` (this-week velocity), per-person profiles for those with overnight activity.
**Output**: 30s scan-to-talking-points.
**Status**: Fully supported — SessionStart hook already surfaces alerts.md.

### 1.2 1:1 prep for a single report ✅
**Trigger**: EM types "prep 1:1 with <name>".
**Inputs (have)**: `people/<handle>.md` (240d profile + narrative + MatterAI summaries), prior `sessions/*.md` notes referencing the person, `management/context/team.md` open-thread section under their name.
**Output**: One-page prep doc into `management/drafts/`.
**Status**: Fully supported — `/dev-review` skill is one rung beyond this.

### 1.3 Stale PR triage ✅
**Trigger**: `alerts.md` lists ≥7d-inactive PRs.
**Inputs (have)**: Subject, actor, title, last activity timestamp.
**Output**: Per-PR triage decision (nudge author / reassign / close).
**Status**: Fully supported.

### 1.4 Drive-by merge audit ✅
**Trigger**: `alerts.md` lists merged-without-review PRs in last 30d.
**Inputs (have)**: Subject, merger, title, merge timestamp.
**Output**: List of merger × frequency for performance/policy follow-up.
**Status**: Fully supported.

### 1.5 Risk-flag triage ✅
**Trigger**: EM asks "any security/data-loss/race-condition flags in last week".
**Inputs (have)**: `subject_summary.risk_flags` (enum: security, data-loss, panic, race, migration, breaking-api), populated by chat classifier from title+body+MatterAI summary.
**Output**: Filtered PR list with summaries.
**Status**: Fully supported. Query: `SELECT subject, summary, risk_flags FROM subject_summary WHERE risk_flags LIKE '%security%'`.

### 1.6 Reviewer load tracking ➕
**Trigger**: EM asks "who is doing too many / too few reviews".
**Inputs (have)**: `events WHERE event_type='review' AND actor=<handle>` aggregated per actor.
**Gap**: Per-person review counts already in `people/<handle>.md` (top reviewers section). What's missing: cross-team comparison view + ratio (PRs-authored : reviews-given).
**Extension**: ➕ — add `derived/review-balance.md` summary (1 SQL query + format).
**Output**: Sorted list with ratio anomalies highlighted.

### 1.7 Today's mentions of me ➕
**Trigger**: EM asks "what was I tagged in yesterday".
**Inputs (have)**: `events_fts` over title+body for `@owner@example.com` and similar.
**Gap**: FTS5 search works but not as a slash command.
**Extension**: ➕ — add `/mentions [days]` slash command running FTS5 query.

---

## 2. Sprint / planning

### 2.1 Sprint planning — capacity inference ➕➕
**Trigger**: Sprint planning meeting.
**Inputs (have)**: Historical PR count per person + cycle time p50/p90 (from `build_weekly`).
**Gap**: No story-point ingestion. Capacity inferred from PR count is noisy (5× spread by complexity). No sprint_id on events.
**Extension**: ➕➕ — extend `ingest/jira.py::normalize_issue_created` to capture `customfield_10016` (story points, varies per Jira install) + `customfield_10020` (sprint). New column on events schema; backfill via Jira API like `backfill-jira-issue-type.py`.
**Effort**: 1-2 days for ingest, half-day for `build_capacity.py` summary.
**Output**: Per-person story-point throughput trailing 6 sprints + suggested commitment range.

### 2.2 Sprint retrospective — what shipped vs committed ➕➕
**Trigger**: Sprint-end retro.
**Inputs (have)**: PRs merged in sprint window, Jira status_changes to Done in window.
**Gap**: No sprint boundaries; can use rolling 2-week window as proxy but won't align to Jira sprint.
**Extension**: ➕➕ — same `sprint_id` field as 2.1. Once present, query `events WHERE sprint_id=X AND ts BETWEEN sprint_start AND sprint_end`.
**Output**: Sprint-bounded markdown summary with commit-vs-deliver delta.

### 2.3 Backlog grooming — what's "Ready" but unstarted ➕➕
**Trigger**: Backlog grooming meeting.
**Inputs (have)**: Jira `status_change` events show transitions; we know `current status` only by replaying the changelog.
**Gap**: No point-in-time issue state. Have to reconstruct from changelog.
**Extension**: ➕➕ — `derive/build_issue_state.py` materialises current issue state by replaying status_changes per ticket. Or — simpler — `ingest/jira.py` adds a `status` field to `issue_created` events and updates on each transition (denormalise current).
**Output**: Per-domain backlog list grouped by current status.

### 2.4 Velocity trend across quarters ➕
**Trigger**: Quarter-boundary check-in.
**Inputs (have)**: 240d of events. Velocity proxy = PR-merged count.
**Gap**: No quarter aggregation in `build_weekly`. Story points would improve fidelity (see 2.1).
**Extension**: ➕ — add `derived/quarterly/<YYYY-Qn>.md` similar to weekly.
**Output**: Per-quarter team volume, cycle-time, person-leaderboard delta.

### 2.5 Cross-sprint dependency graph ➕➕➕
**Trigger**: Sprint planning where tickets reference each other.
**Inputs (have)**: `refs.tickets` regex catches `[A-Z]{2,10}-\d+` in titles/bodies. Doesn't capture formal Jira issue links (blocks/blocked-by/relates-to).
**Gap**: Jira issuelinks not ingested.
**Extension**: ➕➕➕ — `ingest/jira.py` add `issue_link` event type from `/rest/api/3/issueLink` endpoint. New refs sub-type or dedicated table.
**Output**: Dependency graph per epic, blocker visualisation.

---

## 3. Retrospective / time-window summary

### 3.1 Monthly highs / lows ✅
**Trigger**: Last day of month or first day of next.
**Inputs (have)**: All events in trailing 30d, per-domain rollups, per-person narratives, alerts breakdown.
**Output**: Markdown summary: top shipped features per domain, biggest risk flags surfaced, anti-patterns (drive-by count, stale PR avg age), per-person highlights.
**Status**: Data fully there. Wrap in `/monthly-retro [YYYY-MM]` slash command (≤1 day).

### 3.2 Quarterly review / OKR check-in ✅
**Trigger**: Quarter boundary.
**Inputs (have)**: 240d window covers full quarter.
**Output**: Per-domain shipped epics (via Jira epic anchor + status_changes to Done) + headcount-weighted velocity + risk pattern table.
**Status**: Data there. Add `/quarterly-retro [YYYY-Qn]`.

### 3.3 Annual reflection / team-year-in-review ➕
**Trigger**: End of year.
**Inputs (have)**: Up to 240d. Need to extend rollup window to 365d.
**Gap**: `rollup.py --days N` accepts arbitrary N, just need to test that it doesn't OOM at 365d (~2-3K events).
**Extension**: ➕ — verify 365d run, add `/yearly-retro [YYYY]` skill.
**Output**: Year-in-review per-domain + per-person.

### 3.4 Custom-window retro for incidents ✅
**Trigger**: "Show me everything that happened around the database outage on a given day".
**Inputs (have)**: `events WHERE ts BETWEEN ts_minus_2d AND ts_plus_2d`.
**Output**: Chronological timeline.
**Status**: Fully supported via ad-hoc SQL or `/timeline [start] [end]` (new skill, ≤1 day).

---

## 4. People management

### 4.1 Promotion case writing ✅
**Trigger**: Promotion cycle.
**Inputs (have)**: 240d of activity for the candidate, MatterAI summaries on their PRs, narrative covering focus areas + review behaviour + Jira ownership + docs authored.
**Output**: Two artifacts via `/dev-review`: shareable promo doc + private manager note.
**Status**: Fully supported. `/dev-review` exists.

### 4.2 Performance review (full cycle / mid-cycle) ✅
**Trigger**: Review season.
**Inputs (have)**: Same as 4.1 plus `dev-review` skill from `anthropic-skills` plugin (full-cycle / mid-cycle modes).
**Output**: Formal review + manager note per person.
**Status**: Fully supported.

### 4.3 Underperformer documentation ✅
**Trigger**: When patterns concerning a specific report emerge.
**Inputs (have)**: Drive-by-merge involvement, stale-PR ownership, risk-flag patterns (frequent panic / race), low review-given count, cycle-time outliers.
**Output**: Evidence-backed manager-note in `management/drafts/`.
**Status**: Fully supported.

### 4.4 New-hire onboarding tracking ➕
**Trigger**: First 90 days of a new joiner.
**Inputs (have)**: Their PR count ramp, first review submitted, first independent ticket closed, domains they're touching.
**Gap**: No "join date" field on `people.yaml`.
**Extension**: ➕ — add `join_date` (and optional `level`) to `people.yaml`. New `/onboarding-status <handle>` skill checks: first PR by day 14? first review by day 21? cycle time vs team p50 by day 60?
**Output**: Pass/concern indicators for each onboarding milestone.

### 4.5 Mentor matching ➕
**Trigger**: Pairing a junior with a senior reviewer.
**Inputs (have)**: Per-person domain breakdown (where they work) + review-given graph (who reviews whom).
**Gap**: No explicit mentor-relationship store; mentor matching needs join-date + level metadata.
**Extension**: ➕ — same `level` field as 4.4; add `/suggest-mentor <handle>` querying "senior+ engineer in their primary domain with bandwidth (reviews-given < team p50)".

### 4.6 1:1 cadence tracking ➕
**Trigger**: "When did I last 1:1 with each report".
**Inputs (have)**: `sessions/*.md` contain 1:1 notes but unstructured.
**Gap**: No structured 1:1 record.
**Extension**: ➕ — convention: `sessions/<date>.md` entries titled `1:1 — <handle>` get parsed by a `/cadence-check` skill via grep. Returns per-handle days-since-last-1on1.

### 4.7 Boss's 1:1 prep — "what to tell my manager" ✅
**Trigger**: Before EM's own 1:1 with their boss.
**Inputs (have)**: This-week shipped features per domain, blockers from alerts.md, risk surfaces from `subject_summary.risk_flags`, cross-team interactions inferable from refs.tickets cross-project.
**Output**: Boss-update markdown bullets.
**Status**: Fully supported — write `/boss-update` skill.

### 4.8 Burnout / overcommitment detection ➕
**Trigger**: EM concerned about a report's workload.
**Inputs (have)**: PR volume, after-hours commit timestamps, weekend activity, review-given count.
**Gap**: No timezone normalisation per person — would compare local night/weekend per person.
**Extension**: ➕ — add `timezone` to `people.yaml`. Build `derive/build_workload.py` showing per-person after-hours hour distribution.

### 4.9 Skill-gap mapping ➕➕
**Trigger**: Team-wide skill audit.
**Inputs (have)**: Per-person domain breakdown.
**Gap**: Domains are projects, not skills. No technology / stack tagging.
**Extension**: ➕➕ — extend `projects.yaml` slugs with `tags: [go, sql, kafka, ...]`. Re-aggregate to skill-level via tag map. Or — separately — classify PR diffs into tech-stack tags during chat-classify pass.

---

## 5. Cross-team / stakeholder

### 5.1 TRD / PRD review ✅
**Trigger**: New TRD lands; EM reviews.
**Inputs (have)**: Atlassian MCP (fetch + comment), per-domain rollup for related work.
**Output**: Footer comments on the Confluence page.
**Status**: Fully supported. Memory rule "never paraphrase TRD from memory" already enforced.

### 5.2 Design review prep ✅
**Trigger**: EM joins a cross-team design review.
**Inputs (have)**: Fetch TRD page, read related per-domain rollup, scan recent PRs in the domain.
**Output**: Pre-meeting notes drafted to `management/drafts/`.
**Status**: Fully supported.

### 5.3 Cross-team dependency tracking ➕➕
**Trigger**: "Is my team blocking another team or vice versa".
**Inputs (have)**: Refs.tickets caught `XX-NNN` strings.
**Gap**: No formal Jira issuelinks (blocks/blocked-by).
**Extension**: Same as 2.5 — ingest Jira issue links.
**Output**: Per-domain "blockers we own vs blockers blocking us" table.

### 5.4 Stakeholder weekly update 🔵
**Trigger**: Friday stakeholder email/Slack post.
**Inputs (have)**: Per-domain shipped this week, narrative bullets per person.
**Gap**: Drafting is supported; sending via Slack is the post-Slack-ingest path.
**Status**: Markdown draft ✅ (today). Slack post 🔵 (after Slack strategy).

### 5.5 Architecture decision logging ➕
**Trigger**: New ADR-worthy decision made in chat or PR review.
**Inputs (have)**: Conversation in `sessions/*.md` + PR comments.
**Gap**: No structured ADR sink.
**Extension**: ➕ — `/adr` slash command writes to `$HOME/context/work-context/docs/adr/<n>-<slug>.md` with date / context / decision / consequences template. (context-mode skill `improve-codebase-architecture` already expects this dir to exist.)

---

## 6. Project / domain health

### 6.1 Project status snapshot ✅
**Trigger**: "How is project X tracking".
**Inputs (have)**: `derived/projects/<slug>.md` — PR/ticket/page counts, contributor leaderboard, recent items.
**Output**: One-page snapshot.
**Status**: Fully supported.

### 6.2 Roadmap update ➕
**Trigger**: Quarterly roadmap revision.
**Inputs (have)**: Domain rollups + Jira epic status (via status_changes).
**Gap**: No explicit roadmap/timeline data — but per-epic % complete inferable from `tickets-in-epic` × `tickets-done`.
**Extension**: ➕ — `/roadmap [domain]` slash command computes per-epic completion %.

### 6.3 Domain knowledge concentration ✅
**Trigger**: "Who knows the most about instant-pay-ATM".
**Inputs (have)**: Per-domain rollup contributor leaderboard.
**Output**: Top-3 contributors by PR + review count in domain.
**Status**: Fully supported.

### 6.4 Bus-factor / single-owner risk ➕
**Trigger**: Risk audit.
**Inputs (have)**: Per-domain contributor leaderboard.
**Gap**: Need to compute "domains where top-1 contributor has >X% of last 90d work".
**Extension**: ➕ — `derive/build_bus_factor.py` summarises domains with concentration > 70%.

### 6.5 Tech-debt inventory ➕➕
**Trigger**: Quarterly tech-debt planning.
**Inputs (have)**: `risk_flags` (migration / breaking-api / panic) + Jira labels (if we capture them).
**Gap**: Jira labels not currently ingested.
**Extension**: ➕➕ — `ingest/jira.py` capture `f["labels"]` array. Add `labels` table or denormalised column. Filter for `tech-debt`, `chore`, `refactor` labels.

### 6.6 Migration progress tracking ✅
**Trigger**: "How far along is the X migration".
**Inputs (have)**: Subjects with `migration` risk flag, or in a designated migration epic.
**Output**: PR count + cycle-time progress.
**Status**: Fully supported.

---

## 7. Incident / quality

### 7.1 Incident post-mortem prep ✅
**Trigger**: Incident at time T.
**Inputs (have)**: All events ±2 days of T, MatterAI summaries flag `critical/panic`, recent merges touching the affected service.
**Output**: Chronology + suspect commit list.
**Status**: Fully supported. Could add `/incident [date] [service]`.

### 7.2 Production deployment tracking ➕➕
**Trigger**: "When was X deployed".
**Inputs (have)**: `pr_merged` events (proxy for deploy if merges auto-deploy).
**Gap**: No deploy-time signal. Most teams have deploy-console/Kubernetes events not captured.
**Extension**: ➕➕ — new source: parse `deploy-console` PR comments or fetch deploy-console events via API. Adds a `deploy-console` source.

### 7.3 Bug pattern detection ➕
**Trigger**: "What kinds of bugs do we ship most".
**Inputs (have)**: Jira issues with `issue_type=Bug`, their domains, their MatterAI summaries.
**Gap**: No bug categorisation (race / null-pointer / regression / config).
**Extension**: ➕ — chat-classify bugs into category tags during normal rollup; or extend `risk_flags` enum.

### 7.4 Code quality drift ➕➕
**Trigger**: "Are we getting better or worse at reviews".
**Inputs (have)**: Review counts, drive-by merge rate.
**Gap**: No review-comment-depth signal (number of inline comments per review).
**Extension**: ➕➕ — `ingest/github.py::normalize_review` capture `comments_count` per review. Aggregate per-month to detect review-depth drift.

### 7.5 Flaky test / CI signal ⛔
**Trigger**: "Which test is flakiest".
**Inputs (have)**: None — CI events not ingested.
**Gap**: GitHub Actions / Jenkins events would need their own source.
**Status**: Out of scope today.

---

## 8. Org / strategy

### 8.1 Headcount justification ➕
**Trigger**: Asking for an additional headcount.
**Inputs (have)**: 6-month per-person workload, drive-by-merge rate (signal of overload), stale-PR ownership concentration.
**Gap**: Need a `/headcount-case` skill stitching these into a paragraph.
**Extension**: ➕ — new skill, ≤1 day.

### 8.2 Team-of-teams comparison ⛔
**Trigger**: "How does my team compare to team X".
**Status**: Explicitly **non-goal** per PRD §3. Single-team product.

### 8.3 Vendor / tool evaluation ➕➕➕
**Trigger**: "Should we adopt tool X".
**Inputs**: Not directly served by this corpus.
**Status**: Major extension or out of scope — would need to ingest issue search / community signals.

### 8.4 Compensation calibration ✅
**Trigger**: Comp cycle.
**Inputs (have)**: Same as performance review (4.2).
**Output**: Comp-justification evidence per person.
**Status**: Fully supported.

### 8.5 Team morale signal 🔵
**Trigger**: EM senses team energy.
**Inputs**: Slack tone, after-hours patterns, PR comment sentiment.
**Status**: 🔵 — Slack-dependent. Even with Slack, sentiment analysis adds another LLM dimension. Heavyweight.

---

## 9. Personal EM growth

### 9.1 "What did I actually do this quarter" ✅
**Trigger**: Self-reflection.
**Inputs (have)**: `sessions/*.md` (every session journal entry), `audit/log.jsonl` (every tool call), per-person profile (for own handle if EM also codes).
**Output**: Personal quarterly summary.
**Status**: Fully supported via `/dev-review <my-handle>` + sessions read.

### 9.2 Time allocation tracking ➕
**Trigger**: "Where did my week go".
**Inputs (have)**: `audit/log.jsonl` line count per tool / per day; session timestamps from filenames.
**Gap**: No category tagging.
**Extension**: ➕ — parse session titles ("1:1 — X" / "Design review — Y") and bucket time. Add `/time-audit [week]` skill.

### 9.3 Coaching topics for myself ➕
**Trigger**: "What should I be learning".
**Inputs (have)**: Domains my reports work in vs domains I'm familiar with.
**Gap**: No explicit "what I'm strong at" — could infer from PR authoring on my own handle if applicable.
**Extension**: ➕ — gap analysis report.

---

## 10. Slack-dependent (BLOCKED-ON-SLACK)

These journeys are unlocked once Slack ingest strategy is defined. Feasibility ratings deferred.

### 10.1 Channel-level activity per project 🔵
Slack channel × domain mapping. Track question volume, response time, who answers.

### 10.2 After-hours messaging burden 🔵
Per-person Slack message timestamps outside working hours.

### 10.3 PRD/TRD-link tracking from Slack 🔵
When someone shares a Confluence page in Slack, capture intent + reactions as a referral signal.

### 10.4 Support / interrupt load per person 🔵
Threads tagging a person across channels — "how often is X interrupted".

### 10.5 Decision provenance — "where was this decided" 🔵
Combined search across Slack + Jira comments + PR comments for a phrase.

### 10.6 Stakeholder update post 🔵
Auto-draft + send weekly update to a target channel.

### 10.7 Team morale / sentiment scan 🔵
Aggregate sentiment over team-only channels.

**Slack-strategy questions to settle before ingest:**
- Workspace + channel allow-list (all team channels, or curated set?)
- Threads vs top-level messages — both, or top-level only?
- DMs — never ingest (recommended)? or with explicit consent per user?
- Reactions — capture? (high signal: who agrees with what)
- Bot messages — skip pattern (slack-bot / opsgenie / pagerduty separate? capture as alerts source?)
- Retention — same as other sources (indefinite) or trimmed?
- Privacy — does anyone besides EM see this output? Affects acceptable scope.

---

## Summary table

| Category | ✅ Works | ➕ Small | ➕➕ Medium | ➕➕➕ Major | 🔵 Slack | ⛔ OOS |
|----------|---------|---------|------------|------------|----------|--------|
| Daily ops | 5 | 2 | 0 | 0 | 0 | 0 |
| Sprint / planning | 0 | 1 | 3 | 1 | 0 | 0 |
| Retrospective | 3 | 1 | 0 | 0 | 0 | 0 |
| People mgmt | 4 | 4 | 1 | 0 | 0 | 0 |
| Cross-team | 2 | 1 | 1 | 0 | 1 | 0 |
| Project/domain health | 3 | 2 | 1 | 0 | 0 | 0 |
| Incident / quality | 1 | 1 | 2 | 0 | 0 | 1 |
| Org / strategy | 1 | 1 | 0 | 1 | 1 | 1 |
| Personal growth | 1 | 2 | 0 | 0 | 0 | 0 |
| Slack-dependent | — | — | — | — | 7 | — |
| **Total** | **20** | **15** | **8** | **2** | **9** | **2** |

---

## High-impact additions ranked (recommendation order)

1. **Jira sprint + story-point fields** (➕➕, 1-2 days) — unlocks 4 journeys (2.1 sprint planning, 2.2 sprint retro, 2.3 backlog, 2.4 velocity trend). Highest leverage per day of work.
2. **Monthly retro skill** (➕, ≤1 day) — data already there; needs synthesis skill (3.1).
3. **Jira current-state denormalisation** (➕➕, 2-3 days) — backlog grooming (2.3) + bus-factor (6.4) + roadmap (6.2).
4. **Jira issue links** (➕➕➕, 3-5 days) — dependency tracking (2.5, 5.3).
5. **`people.yaml` join_date + level + timezone** (➕, ≤1 day) — onboarding tracking (4.4) + mentor match (4.5) + burnout detection (4.8).
6. **Jira labels capture** (➕➕, 1 day) — tech-debt inventory (6.5).
7. **GitHub review-comment count** (➕➕, 1 day) — code quality drift (7.4).
8. **Slack ingest** — separate design pass; unlocks 7+ journeys.

---

## Recommended phase plan

**Phase 8a (this week, ≤2 days of work):**
- Story points + sprint_id capture in jira ingest (#1 above)
- Monthly retro skill (#2)
- `/quarterly-retro`, `/timeline`, `/mentions`, `/boss-update` skills (each ≤2 hours)

**Phase 8b (next week, ≤3 days):**
- `people.yaml` join_date / level / timezone (#5)
- `/onboarding-status`, `/suggest-mentor`, `/time-audit` skills
- Jira labels (#6)
- `derive/build_review_balance.py` for 1.6

**Phase 9 (when ready, ≤1 week):**
- Slack ingest strategy decision
- Slack ingest implementation
- Top-3 Slack journeys: 10.4 interrupt load, 10.5 decision provenance, 10.1 channel × domain

**Phase 10 (≥1 week, opportunistic):**
- Jira issue links (#4)
- Production deploy events from deploy-console
- ADR sink directory + skill

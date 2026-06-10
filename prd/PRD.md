# PRD — Personal Engineering Management Copilot (`context/`)

**Owner:** Owner · **Status:** Live (v1 shipped) · **Last revised:** 2026-05-12

**What this is:** a local corpus of what your team actually did + a reasoning loop on top, run on one laptop with two API tokens.

> Companion docs: [`work-context/README.md`](../work-context/README.md) (run guide) · [`work-context/ARCHITECTURE.md`](../work-context/ARCHITECTURE.md) (code graph) · [`management/build-notes/README.md`](../management/build-notes/README.md) (build log).
> **Per-story detail docs:** see [Section 13 — Story index](#13-story-index) for detailed PRDs of each user-facing skill.

---

## 1. Problem

EMs lose grip on engineering reality once they stop coding daily. Standups, dashboards, and "how's it going" 1:1s sample selective slices of truth.

By the time a PR has been stale a week, a race has been argued out in PR comments, or a junior has shipped six unreviewed merges, the context lives in scattered GitHub/Jira/Confluence tabs nobody reads in aggregate.

Existing tools (Linear, Jellyfish, Code Climate) optimise for org-wide metrics — not the small-team EM who needs **week-level, person-level, project-level grounded narrative** to drive 1:1s, design reviews, and shipping decisions.

LLM copilots can do this synthesis but lack the data, so they hallucinate. The fix: **stop asking the LLM to remember; feed it a fresh, structured corpus of what your team did, then let it reason on top.**

---

## 2. Goals

1. **Single source of truth.** Every PR, review, comment, commit, Jira transition, Confluence edit by a named team member → queryable local index, no manual entry.
2. **Daily-refreshed narrative views** a Claude Code session reads at session start: per-engineer profiles, per-domain rollups, weekly team stats, drive-by/stale alerts.
3. **Zero ongoing AI spend.** All semantic classification runs through the active Claude Code session; no Anthropic API calls from background scripts.
4. **Resumable.** Pause a week, run `/rollup 30`, caught up in five minutes.
5. **Owner-only.** Personal tool — not multi-tenant, not Slack-installed, not SaaS. One EM on macOS with the LaunchAgent scheduler.

## 3. Non-goals

- **Not org-wide metrics** — no team-of-teams rollups, benchmarks, or "your team vs the company".
- **Not real-time** — daily incremental ingest only; no webhooks, no streaming.
- **Not multi-cloud** — macOS + LaunchAgents only. Linux/Windows reachable (`Darwin/Linux` branch in `manual-rollup.sh`) but unsupported.
- **Not a code-review tool** — doesn't comment on or auto-approve PRs. Read-only side-channel.
- **Not a data warehouse** — schema optimised for the EM-copilot read path, not ad-hoc third-party SQL.

---

## 4. Personas

| Persona | Description | Use case |
|---------|-------------|----------|
| **The EM (primary)** | Owner. Acme service-a team lead, 6 reports across SDE1/SDE2/SDE3, focus on Go microservices + charge engine + deposits. Direct, no-hedging style. Reviews PRs daily; drives 1:1s, design reviews, sprint planning. | "How's Eve doing this month? Any race-condition concerns? What's blocking the counter-schema TRD review?" |
| **Claude Code copilot (secondary)** | LLM reading `derived/` markdown on every session start. Acts for the EM on triage, drafting, search. Constrained by `management/CLAUDE.md` hard rules. | Surfaces stale PRs at session start, drafts 1:1 talking points, fetches TRD page on request. |
| **Another EM (tertiary, future)** | Hypothetical colleague cloning the repo for their team. Onboarding in top-level `README.md`. | Same as primary, scoped to their team. |

---

## 5. User journeys

### 5.1 Morning standup prep (primary daily flow)
1. EM opens a Claude Code session in `~/context/management/`.
2. SessionStart hook auto-runs `cron-status.sh` — confirms last night's ingest + rollup succeeded.
3. `CLAUDE.md` auto-reads `context/activity/alerts.md` — surfaces stale PRs (>7d open, no activity) + drive-by merges (merged last 30d, zero non-bot reviews).
4. EM: "What's Eve shipping this week?" → Claude reads `context/activity/people/org-evek03.md` → answers with MatterAI summary + risk flags + recent PRs.

### 5.2 1:1 prep
1. EM: "Prep 1:1 with Bob for tomorrow."
2. Claude reads `people/org-bobk.md` (activity, narrative, MatterAI summaries) + most recent `sessions/*.md` (prior 1:1 notes) + `management/context/team.md` (open threads under Bob's section).
3. Claude drafts into `management/drafts/bob-1on1-<date>.md`. EM edits.
4. After 1:1, EM dictates notes; Stop hook writes session journal; EM appends resolution to `team.md`.

### 5.3 Cross-functional design review
1. New TRD lands in Confluence.
2. EM: "Read TRD <page-id> and pull related work my team's done on this domain."
3. Claude fetches the Confluence page (no paraphrasing from memory — hard rule) + reads `projects/<slug>.md` for the matching domain.
4. EM gets one-shot context for a 30-min review without tab-switching.

### 5.4 Catch up after PTO
1. EM returns from 10 days off.
2. Runs `/rollup` in chat: dump pending → chat classifies → apply. ~5 min.
3. Reads `derived/weekly/<current-week>.md` for velocity / cycle time / who-merged-what.
4. Scans `alerts.md` for what slipped.

### 5.5 Promotion / quarterly review writing
1. EM runs `/dev-review` on a report (e.g. Eve).
2. Skill reads `people/org-evek03.md` — 240 days of activity, MatterAI summaries, narrative.
3. Outputs two artifacts: shareable review doc + private manager note.
4. EM lands the review in an afternoon, not a week.

---

## 6. Functional requirements

### 6.1 Ingest

| ID | Requirement | Source of truth |
|----|-------------|-----------------|
| F1 | Ingest GitHub PRs, reviews, comments, commits-in-PR, direct pushes, per-PR merger fetch | `ingest/github.py` |
| F2 | Ingest Jira issues (created), changelogs (status/assignment), comments. Capture `issue_type` (Epic / Task / CMR / Bug / Story / IAI) | `ingest/jira.py` + `ingest/backfill-jira-issue-type.py` |
| F3 | Ingest Confluence page versions (created/updated) + inline/footer comments. Team-only filter via `jira_id` set from `config/people.yaml` | `ingest/confluence.py` |
| F4 | Cursor-based incremental fetch (`state/cursors.json`); `--reset-cursor` for full backfill; duplicates de-duped via `INSERT OR IGNORE` on PK | `ingest/common.py` |
| F5 | Append-only raw JSONL backup per source/date (`raw/<source>/YYYY/MM/DD.jsonl`); every row carries `raw_path` pointer with 1-indexed line number | `ingest/common.py::append_raw` |
| F6 | Unified Event schema normalised at ingest: `id, source, event_type, ts, actor, subject, title, body, url, refs{people,projects,tickets,pages}, raw_path, issue_type` | `ingest/common.py::Event` |
| F7 | Refs enrichment: people (alias resolve via `people.yaml`), tickets (regex `[A-Z]{2,10}-\d+`), pages (regex `/pages/\d{8,12}`), projects (keyword + epic + page match) | `ingest/common.py::enrich_refs` |
| F8 | Idle gate per source: first success/day writes `state/last_<src>_success.date`; same-day re-run exits 0. `--reset-cursor` does NOT write the gate | `ingest/common.py::write_success_date` + run wrappers |

### 6.2 Derivation

| ID | Requirement | Source of truth |
|----|-------------|-----------------|
| F9 | Daily rollup: per-engineer profile, per-domain rollup, team weekly stats, alerts — markdown only, regenerated from `events.db` | `derive/rollup.py::main` |
| F10 | Per-engineer profile: activity counts, domains as author/reviewer/owner, per-domain item list with MatterAI summaries, recent PRs, top reviewers, multi-section narrative | `build_person_profile` + `narrative.py::narrate_people` |
| F11 | Per-domain rollup: PR/ticket/page counts, contributor leaderboard, recent items (linked to MatterAI summaries) | `build_project_rollup` |
| F12 | Weekly team stats: volume, cycle time (open→merged hours, p50/p90), review coverage | `build_weekly` |
| F13 | Alerts: stale PRs (≥7d no activity), drive-by merges (last 30d, zero non-bot reviews), classifier fallback banner if any subjects keyword-classified | `build_alerts` |
| F14 | Domain classification priority: (a) Jira epic anchor (deterministic) → (b) auto-slug from new Epics → (c) chat LLM via `/rollup` → (d) keyword fallback vs `projects.yaml` | `derive/llm_classifier.py` + `derive/dump_pending.py` |
| F15 | Auto-slug new Epics only: filter `issue_type == "Epic"`; kebab-case from title; bigram-only keywords; append to `projects.yaml` via `_persist_auto_slugs` | `derive/dump_pending.py::_detect_new_epic_slugs` |
| F16 | Two-cache design: `subject_summary` keyed by `(subject, content_hash)`; `person_narrative` keyed by `(actor, window_days, content_hash)`. Stable post-merge content → one classification per lifetime | `llm_classifier.ensure_schema` + `narrative.ensure_schema` |
| F17 | Verdict validation: slug ⊆ projects.yaml enum, risk-flag ⊆ enum, summary ≤200 chars, confidence ≥0.7, `needs_diff: true` rejected (dead flag), epic anchor re-applied | `derive/apply_verdicts.py::_validate` |
| F18 | All scripts (cron + manual-rollup) strip `ANTHROPIC_API_KEY` + `ANTHROPIC_AUTH_TOKEN` before invoking `rollup.py`. LLM classification happens **only** in active Claude Code session. `_call_claude` raises `RuntimeError` on retry exhaustion to fail loud if auth leaks in | `derive/manual-rollup.sh::run_rollup` + `derive/run-rollup.sh` + `llm_classifier._call_claude` |

### 6.3 Slash commands (chat workflow)

| ID | Requirement | Source of truth |
|----|-------------|-----------------|
| F19 | `/rollup [days]` — three-phase chat-classify cycle (dump → classify in chat → apply). Default 240 days | `.claude/commands/rollup.md` |
| F20 | `/classify` — phase 2 only (re-classify pending without re-dumping) | `.claude/commands/classify.md` |
| F21 | `/cron-status` — render scheduler health for current session | `.claude/commands/cron-status.md` |
| F22 | `/dev-review` — two artifacts (shareable + private note) for a given engineer over the rollup window | skill defined in `management/` |

### 6.4 Scheduler

| ID | Requirement | Source of truth |
|----|-------------|-----------------|
| F23 | macOS LaunchAgent per source; survive sleep/wake; replay missed fires on wake. Plists are org-agnostic templates (label prefix `com.example`, paths `__REPO__`/`__HOME__`); `bin/install-agents.sh` substitutes real prefix + paths at install via its `SERVICES` array | `work-context/launchagents/com.example.*.plist` + `bin/install-agents.sh` |
| F24 | Cadence: github/jira on `:00, :30` of every 12-22 IST hour; confluence on `:05, :35`. **Rollup is MANUAL via `/rollup` — no background LaunchAgent.** | LaunchAgent plists |
| F25 | Cron-status dashboard reports last success (IST), today status, next fire, retry policy, 24h event counts, DB totals, rollup classify breakdown | `bin/cron-status.sh` |
| F26 | SessionStart hook in both `context/` and `management/` runs cron-status and injects output as system reminder | `.claude/settings.json` hooks |

### 6.5 Management copilot

| ID | Requirement | Source of truth |
|----|-------------|-----------------|
| F27 | `management/CLAUDE.md` auto-loaded at session start; includes identity, team, projects, jira-config, confluence-pages | `management/CLAUDE.md` `@context/*` includes |
| F28 | Session-start sequence: read `context/activity/alerts.md` → `tail -n 30 audit/log.jsonl` → read most recent `sessions/*.md` → summarise open threads | `management/CLAUDE.md` |
| F29 | PostToolUse hook appends one JSON line per tool call to `audit/log.jsonl` | `management/.claude/hooks/audit.sh` |
| F30 | Stop hook writes session journal to `sessions/<YYYY-MM-DD>.md` (HH:MM title + Done / Open threads / Files touched) | `management/.claude/hooks/session-summary.sh` |
| F31 | Hard rules: Confluence cloudId always explicit (`YOUR_CONFLUENCE_CLOUD_ID`); never paraphrase TRD/PRD from memory; state intent before mutating outside `drafts/`; counter/charge config belongs in service-layer not DB; flag race-conditions on first-of-month patterns | `management/CLAUDE.md` |

### 6.6 Auto-memory

| ID | Requirement | Source of truth |
|----|-------------|-----------------|
| F32 | Per-project memory store under `~/.claude/projects/<encoded-path>/memory/` — frontmatter typed (user/feedback/project/reference), indexed in `MEMORY.md` | Claude Code auto-memory |
| F33 | Active memories: session-state hygiene, plan-share rule, no-assumptions rule, MatterAI signal value, activity rollup config, cron-status prefs, ingest retry policy, people summary format, rollup 429 history, Epic-first classification, chat-only classification policy | `~/.claude/projects/-Users-owner-context/memory/` |

---

## 7. Non-functional requirements

### 7.1 Performance

| Metric | Target | Measured |
|--------|--------|----------|
| Daily incremental ingest (all 3 sources) | <60s | ~15-30s |
| Full 240d rollup, cache-hit | <30s | ~10s |
| Full 240d chat classify cycle | <10 min | ~5 min (1552 subjects) |
| Cron-status SessionStart hook | <500ms | ~200ms |

### 7.2 Resilience

- **Idempotent ingest** via `INSERT OR IGNORE` on event PK; safe to re-run any number of times.
- **Idempotent rollup** via content-hash cache; same input → same output.
- **Append-only raw JSONL** audit trail — any event re-derivable from raw.
- **WAL mode + 30s busy_timeout** on every SQLite open — concurrent processes safe.
- **LaunchAgent retries** every 30 min until first success/day; failure auto-retries next fire.
- **Cursor + idle-gate + raw JSONL** = four-way safety net: no event silently lost.

### 7.3 Security

- All secrets at `~/.secrets/`, mode 600, never committed (`management/` git-ignores `~/.secrets/`).
- Tokens: `github_pat` (repo + read:org), `atlassian_token`, `atlassian_email`. No `anthropic_api_key` in normal flow (chat-only policy).
- No third-party data sharing — entirely local.
- `management/` = owner-only private git remote; `work-context/` = local-encrypted-disk only, no cloud sync.

### 7.4 Compatibility

- macOS only (LaunchAgent scheduler). Linux runs the Python paths but `bin/install-agents.sh` is darwin-specific.
- Python 3.11+ (3.13 in current venv).
- SQLite 3.x with FTS5 + WAL.
- Claude Code (any version supporting Skills + hooks + slash commands).

### 7.5 Observability

- Logs: `logs/ingest.log` (every ingest run), `logs/rollup.log` (every rollup), `logs/github-reset.log` (`--reset-cursor` runs).
- `bin/cron-status.sh` = single dashboard; emits to stdout, captured by SessionStart hook.
- Anomalies surface visibly: classifier fallback banner in `alerts.md`, auto-slug ℹ notice in `alerts.md`, `pending: N subjects need /rollup` line in cron-status when keyword-fallback missed events.

---

## 8. Data model

### 8.1 Schema

Six SQLite tables in `index/events.db` (full DDL: `ARCHITECTURE.md` §6.1):

- `events` — primary store, ~tens of thousands of rows.
- `event_refs` — one row per event × ref_type × ref_value (people/projects/tickets/pages).
- `events_fts` — FTS5 virtual table over `title + body`.
- `cursors` — per-source high-water mark.
- `subject_summary` — classifier cache, `(subject, content_hash)` PK.
- `person_narrative` — narrative cache, `(actor, window_days, content_hash)` PK.

### 8.2 Retention

- **events / event_refs / events_fts**: indefinite, full history. ~50 MB at 10K events/year.
- **raw JSONL**: indefinite, append-only.
- **subject_summary / person_narrative**: indefinite; invalidated on content_hash change (body edits, re-classification).
- **state/verdicts.<ts>.json**: archived per rollup; never deleted.
- **state/diff_cache/**: head-sha keyed; never explicitly evicted.

### 8.3 Config

- `config/people.yaml` — manual; ~6 entries; required `canonical`/`github`; `jira_id` mandatory for Confluence team filter.
- `config/projects.yaml` — manual + auto-grown; 88 entries as of 2026-05-12 (53 hand-curated + 35 auto-generated Epic slugs). Append-only via `_persist_auto_slugs`.
- `work-context/launchagents/com.example.*.plist` — 8 org-agnostic plist templates (github-ingest, jira-ingest, confluence-ingest, slack-ingest, slack-discover, leaves, housekeeping, codegraph); `bin/install-agents.sh` materialises them as owner-private user agents at install.

---

## 9. Success metrics

| Metric | Definition | Target | Current |
|--------|------------|--------|---------|
| **Daily reliability** | Days/week all 3 ingest sources succeed ≥once | 7/7 | Tracked via `cron-status.sh` |
| **Classification completeness** | % pending subjects classified per rollup cycle | 100% | 1552/1552 (2026-05-12) |
| **AI cost per month** | Anthropic API spend from scripts | $0 | $0 (chat-only policy) |
| **EM time saved per week** | Hours not spent manually compiling activity | 3-5h | Qualitative |
| **Time-to-resume after PTO** | Minutes to be caught up | <10 min | 5 min |
| **Memory-driven coherence** | Sessions where Claude recalls prior decisions/prefs without re-explanation | 100% | Tracked via `MEMORY.md` |

---

## 10. Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Atlassian/GitHub API rate-limit during full backfill | Medium | High | `--reset-cursor` doesn't write idle gate; failure auto-retries; fine-grained token scope |
| Chat session OAuth quota exhausted during `/rollup` | Low | Medium | Scripts strip auth → 0 background draw; fail-loud `RuntimeError` on retry exhaustion |
| projects.yaml auto-grows to near-duplicate slug bloat | High | Low | Periodic manual review (open thread, ARCHITECTURE.md §10); bigram-only keyword filter prevents over-broad matches |
| SQLite DB locked from zombie process | Low | High | WAL + 30s busy_timeout; `lsof` + `kill` + `wal_checkpoint TRUNCATE` recipe in README |
| Confluence write access flaky | Medium | Low | Documented in `management/context/confluence-pages.md`; verify after every `updateConfluencePage` |
| Person re-org / handle changes | Low | Medium | `people.yaml` alias list (github/email/jira_id/git_name); add new alias before old disappears |
| Anthropic model retirement | Annual | Medium | Skills updated via `setup-claude-code`; `SYSTEM_PROMPT` in `llm_classifier.py` is the only hard-coded model-coupling site |
| Disk space on raw JSONL growth | Low | Low | Monitor via `du -sh raw/`; rotate/compress yearly if >1 GB |

---

## 11. Out of scope (today) / future work

1. **Slack ingest** — 🔜 in `build-notes/README.md`. Adds `slack_id` to identity map, normalises channel messages + threads. Largest open extension.
2. **Slug consolidation tool** — periodic dedup of near-duplicate auto-slugs (e.g. `tweak-vaccum-configuration-order` vs `...-2635`). Currently manual.
3. **`needs_diff` flag full removal** — dead in chat path, alive in script path for legacy completeness. Remove after one more chat-only cycle confirms zero regression.
4. **`code-review-graph` hub/bridge tools** — broken (`'NoneType' object has no attribute 'resolve'`). Workaround via `query_graph`. Upstream fix tracked.
5. **Embeddings-based subject similarity** — considered + rejected 2026-05-12 (chose chat-only over local sentence-transformers + nearest-neighbour). Revisit if chat token cost grows.
6. **Cross-machine sync** — single-machine today. Future: read replica of `events.db` via private syncthing / encrypted rsync.
7. **Org-wide rollout** — explicit non-goal. Each EM clones; shared `projects.yaml` = cross-team coupling.

---

## 12. Open questions

1. **Body-edit re-classification frequency.** `content_hash` invalidates on body edit → a PR description edited 5×/day re-classifies 5×. Acceptable, or hash only on stable post-merge content?
2. **Confluence team filter strictness.** Pages by people not in `people.yaml` are silently dropped. Surface a `skipped: <N>` line in cron-status to catch a new joiner falling through?
3. **Per-person narrative window.** Currently 240 days. Allow `/rollup people 30` for a 30-day narrative without re-running the full pipeline?
4. **`--detail-summary` flag.** Implemented but off by default (~3× tokens). Opt-in per-person, or always-on for SDE2+ profiles?
5. **Cron-status retention.** ROLLUP block shows "0 pending"; also surface "N auto-slugs created in last 24h" for projects.yaml-growth visibility?

---

## 13. Glossary

- **Subject** — stable cross-event identifier: `repo#num` (GitHub), `KEY` (Jira), `page-id` (Confluence).
- **Refs** — enriched cross-source pointers attached at ingest: people, projects, tickets, pages.
- **MatterAI** — third-party bot leaving `🧪 PR Review is completed: …` summary as a review on every PR. Parsed by `extract_matterai_summary`.
- **Epic anchor** — deterministic domain tagging: Jira issue's epic_key → projects.yaml slug. Highest classification priority.
- **Drive-by merge** — PR merged in trailing 30d with zero non-bot reviews. Surfaced in `alerts.md`.
- **Stale PR** — open PR with ≥7d since any pr_opened / review / comment / commit_pushed event.
- **Chat-only policy** — 2026-05-12 decision: no programmatic Anthropic API call from background scripts; all semantic classification flows through `/rollup` in an active session.
- **Verdict** — `SubjectVerdict` dataclass: `{subject, content_hash, domains, summary, risk_flags, confidence, needs_diff?}`.

---

## 14. Story index

Each user-facing skill/feature has its own detailed PRD in this folder. This doc covers the platform; per-story docs cover usage, contracts, open issues, and roadmap for one slice.

| Skill / feature | Detail doc | Status |
|-----------------|------------|--------|
| `/narrative` — per-person + team narrative | [`narrative-skill.md`](narrative-skill.md) | Live |
| `/retro` — date-range engineering retrospective | _to be added_ | Live |
| `/rollup` — manual rollup (keyword → classify → apply) | _to be added_ | Live |
| `/classify` — chat-driven verdict generator (inside `/rollup`) | _to be added_ | Live |
| `/cron-status` — ingest cron + DB health snapshot | _to be added_ | Live |
| Slack ingest | [`slack-ingest.md`](slack-ingest.md) | **Live** (Phases A-F built, scheduled-tasks routines armed; backfill pending per channel) |
| `trd_owners` materialised view + post-Confluence-ingest hook | _to be added_ | Live |

**Convention:** new skill/feature → new `prd/<skill-name>.md` + row in this table. The overall PRD does not duplicate per-story detail.

---

## 15. Appendices

### 15.1 Key paths

| Thing | Path |
|-------|------|
| PRD folder | `~/context/prd/` |
| Pipeline root | `~/context/work-context/` |
| Copilot root | `~/context/management/` |
| Run guide | `~/context/work-context/README.md` |
| Code graph | `~/context/work-context/ARCHITECTURE.md` |
| Top-level setup | `~/context/README.md` |
| Build log | `~/context/management/build-notes/README.md` |
| SQLite DB | `~/context/work-context/index/events.db` |
| Raw backup | `~/context/work-context/raw/<source>/YYYY/MM/DD.jsonl` |
| Derived markdown | `~/context/work-context/derived/{people,projects,weekly}/*.md` + `alerts.md` |
| Memory store | `~/.claude/projects/-Users-owner-context/memory/` |
| Secrets | `~/.secrets/{github_pat,atlassian_token,atlassian_email}` |

### 15.2 Reference decisions

| Decision | Date | Source |
|----------|------|--------|
| Cron + direct API tokens over Claude Code routines | Phase 2 build | `build-notes/README.md` Key Decisions |
| JSONL + SQLite only (no Postgres / vector DB / Elasticsearch) | Phase 2 build | same |
| Two-pass LLM classifier with diff on-demand | Phase 6 build | same |
| Cache by content hash | Phase 6 build | same |
| Epic-first deterministic anchor | Phase 6 build | `project_epic_first_classification.md` memory |
| Strip Anthropic auth in scripts | Phase 7 (2026-05-12) | `project_rollup_429.md` memory |
| `issue_type` column + Epic-only auto-slug | Phase 7 (2026-05-12) | `feedback_no_assumptions.md` memory |
| Bigram-only keyword extraction | Phase 7 (2026-05-12) | `handoff-2026-05-12-1637.md` §5 |
| Chat-only classification (algo path killed) | Phase 7 (2026-05-12) | `project_chat_only_classification.md` memory |
| Code graph + ARCHITECTURE.md | Phase 7 (2026-05-12) | `ARCHITECTURE.md` |

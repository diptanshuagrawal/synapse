# Architecture & Code Graph

**What this is:** how the code is wired (module/community/flow reference + config tables). Companion to [README.md](README.md) — README = how to run; this = how it's wired.

Generated from the [code-review-graph](https://github.com/your-org/code-review-graph) MCP at `$HOME/.code-review-graph/graph.db`.

---

## 1. Graph overview

Graph last rebuilt **2026-06-03**.

| Metric | Value |
|--------|-------|
| Source files parsed | 100 (Python + Bash) |
| Total nodes | 829 |
| Total edges | 10,149 |
| Functions | 701 |
| Classes | 27 |
| Files | 100 |
| `CALLS` edges | 8,675 |
| `CONTAINS` edges | 730 |
| `IMPORTS_FROM` edges | 739 |
| Detected communities | 3 (directory-based) |
| Detected execution flows | 134 |

Build refresh: `mcp__code-review-graph__build_or_update_graph_tool(full_rebuild=true)` after any structural change.

---

## 2. Community map

**3 directory-aligned clusters** (2026-06-03 build); each reviewable in isolation.

```
┌──────────────────────────┐    ┌──────────────────────────┐
│  ingest-fetch    (145)   │◄───│       bin-read  (34)     │
│  cohesion 0.24 HIGH      │    │  cohesion 0.09 LOW       │
│  ingest/*.py             │    │  bin/backfill-*.py       │
└──────────┬───────────────┘    │  bin/migrate-*.py        │
           │                    │  bin/discover-*.py       │
           │ writes events.db   └──────────────────────────┘
           ▼
┌──────────────────────────┐
│   derive-cmd     (550)   │
│   cohesion 0.10 LOW      │
│   derive/*.py + *.sh     │
└──────────────────────────┘
```

> File tables in §2.1–2.3 are **representative** (original 26-file core), not exhaustive — the 2026-06-03 graph spans 100 files. §5 is the per-module reference for everything else.

### 2.1 `ingest-fetch` — 145 members, cohesion 0.24

**Highest cohesion in the codebase** — tightly-coupled normalisation layer; every node touches `Event` / `Refs` / SQLite cursor state. Representative members:

| File | Classes | Top-level fns | Role |
|------|---------|---------------|------|
| `ingest/common.py` | `Refs`, `Event` | 12 (`get_db`, `_ensure_schema`, `append_raw`, `insert_event`, `read_cursor`, `write_cursor`, `write_success_date`, `_load_people`, `_load_projects`, `_resolve_person`, `enrich_refs`, `store_event`) | Shared event model, SQLite schema, refs enrichment, cursor / idle-gate state. **Hub of ingest community.** |
| `ingest/jira.py` | `JiraClient` (`get`, `post`, `search_issues`, `issue_comments`) | 12 (`setup_logging`, `_ts`, `_user`, `_flatten_adf`, `get_epic_key`, `_prefix_epic`, `normalize_issue_created`, `normalize_changelog_entry`, `normalize_comment`, `ingest_project`, `main`) | Jira REST v3 → unified `Event`. Issue create / changelog / comment normalisation. ADF body flattening. Epic-key extraction. |
| `ingest/github.py` | `GitHubClient` (`get`, `paginate`) | 13 (`setup_logging`, `_ts`, `_actor`, `_email_to_github`, `normalize_pr`, `normalize_review`, `normalize_pr_comment`, `normalize_pr_commit`, `normalize_commit`, `fetch_commit_diff`, `ingest_repo`, `main`) | GitHub REST v3 → unified `Event`. PRs, reviews, comments, commits-in-PR, direct push commits, `pr_merged_by` follow-up calls. |
| `ingest/confluence.py` | `ConfluenceClient` (`get`, `get_page_title`, `paginate`) | 10 (`setup_logging`, `_ts`, `load_team_account_ids`, `normalize_page`, `normalize_comment`, `ingest_pages`, `ingest_comments`, `main`) | Confluence REST → unified `Event`. Filters non-team authors via `jira_id` set from `config/people.yaml`. |
| `ingest/backfill-jira-issue-type.py` | — | 4 (`auth`, `chunked`, `fetch_issue_types`, `main`) | One-shot: backfill `events.issue_type` for existing Jira rows via Jira `/rest/api/3/search/jql`, batched 50. Idempotent. |

### 2.2 `derive-cmd` — 550 members, cohesion 0.10

Largest community; the bulk of the codebase. Low cohesion — spans rollup, classify, narrate, embedding/cluster, ownership, leaves, slack, orchestrate. Representative members:

| File | Classes | Top-level fns | Role |
|------|---------|---------------|------|
| `derive/rollup.py` | — | 17 (`load_projects`, `load_people`, `person_aliases`, `detect_domains`, `extract_matterai_summary`, `_subject_source`, `collect_subjects`, `severity_count`, `fmt_iso`, `hours_between`, `percentile`, `build_person_profile`, `build_project_rollup`, `build_weekly`, `build_alerts`, `_emit_pending_slug_creation`, `main`) | **Primary rollup driver.** Reads `events.db`, joins classifier + narrative caches, emits `derived/{people,projects,weekly}/*.md` + `alerts.md`. People loop iterates canonical github handles only (raw `team_handles` emitted duplicate stub files). |
| `derive/llm_classifier.py` | `SubjectInput`, `SubjectVerdict`, `_Stats` | 18 (`ensure_schema`, `_trunc`, `_content_hash`, `_fallback_classify`, `_build_epic_to_slug`, `_collect_unmapped_epic_context`, `_apply_epic_anchor`, `_projects_context`, `_tools`, `_render_subject`, `_user_msg`, `_load_cached`, `_persist`, `_call_claude`, `_parse_tool_calls`, `_verdict_from_tool`, `classify_subjects`, `extract_epic_key`) | **Central shared module.** Owns types, schema (`subject_summary`), content-hash cache, SYSTEM_PROMPT, tool schema, epic anchor, retry-with-fail-loud `_call_claude`. Used by `rollup.py`, `apply_verdicts.py`, `dump_pending.py`, `narrative.py`, `dump_pending_narrative.py`, `apply_narratives.py`. |
| `derive/narrative.py` | `AuthoredPR`, `GivenReview`, `JiraOwned`, `JiraTransitioned`, `JiraCommented`, `ConfluencePage`, `PersonSignals` | 11 (`ensure_schema`, `_content_hash`, `load_cached`, `persist`, `_person_aliases`, `build_signals`, `_render_signals_block`, `_render_user_msg`, `_call_claude`, `narrate_people`) | Per-person narrative generator. Aggregates signals → LLM → `person_narrative` cache. |
| `derive/diff_fetcher.py` | `DiffFiles`, `GitHubDiffClient` | 6 (`_cache_path`, `_read_cache`, `_write_cache`, `_client`, `fetch_diff`) | Pass-2 diff fetcher for `llm_classifier`. Caches to `state/diff_cache/`. Used when pass-1 verdict has `needs_diff: true`. |
| `derive/dump_pending.py` | — | 8 (`_rules_md`, `_tokenize_title`, `_slug_from_title`, `_keywords_from_title`, `_detect_new_epic_slugs`, `_persist_auto_slugs`, `_sort_key`, `main`) | Dumps uncached subjects to `state/pending_classification.json` + `.rules.md`. Auto-generates kebab-case slugs from new Jira **Epics only** (`issue_type == "Epic"` filter, line 174). Bigram-only keyword extraction. |
| `derive/dump_pending_narrative.py` | — | 4 (`_active_actors`, `_verdicts_for`, `_rules_md`, `main`) | Dumps actors needing narrative regen to `state/pending_narrative.json`. Filters via `team_handles` (full identity set, mirrors `rollup.py`). |
| `derive/apply_verdicts.py` | — | 2 (`_validate`, `main`) | Validates chat-emitted verdicts (slug enum, risk_flag enum, conf threshold, `needs_diff` rejection, epic-anchor re-apply) → `subject_summary`. Stale subjects re-emerge next dump. |
| `derive/apply_narratives.py` | — | 1 (`main`) | Validates chat-emitted narratives → `person_narrative` cache. |
| `derive/manual-rollup.sh` | — | 6 bash fns (`preflight`, `run_rollup`, `phase_dump`, `phase_apply`, `phase_narrate_dump`, `phase_narrate_apply`) | Session-mode rollup orchestrator. Strips `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` before invoking `rollup.py` so the script path never hits Anthropic. Chat does all LLM work. |
| `derive/run-rollup.sh` | — | 1 bash fn (`log`) | Cron entry point. Idle-gates on `state/last_rollup_success.date`. Strips Anthropic auth. |

### 2.3 `bin-read` — 34 members, cohesion 0.09

One-shot maintenance scripts. Lowest cohesion (each independent). Representative members:

| File | Top-level fns | Role |
|------|---------------|------|
| `bin/backfill-confluence-titles.py` | `load_creds`, `main` | One-shot: fetch missing page titles for existing Confluence events. |
| `bin/backfill-jira-epics.py` | `load_creds`, `main` | One-shot: fetch epic links via Jira API for issues that pre-date epic capture. |
| `bin/discover-jira-epics.py` | `main` | Walk Jira project, print epic hierarchy. Discovery helper for `config/projects.yaml`. |
| `bin/migrate-commit-actors.py` | `load_email_to_github`, `parse_raw_path`, `read_raw_event`, `main` | One-shot: re-resolve `actor` on commit events using `email → github` map. |

Plus **non-graph bash scripts** (parsed as 0-function shell files):
- `bin/cron-status.sh` — scheduler health dashboard (runs on every SessionStart).
- `bin/install-agents.sh` — install/reload macOS LaunchAgents from `launchagents/*.plist`.

---

## 3. Execution flows

**134** entry-point-rooted flows in the 2026-06-03 build (`list_flows_tool`). Table = **top 18 by criticality** (depth × out-degree), not the full set. Re-list after a rebuild for current ranking.

| # | Entry point | Depth | Nodes | Files | Crit | Purpose |
|---|------------|-------|-------|-------|------|---------|
| 1 | `derive/rollup.py::main` | 5 | 30 | 2 | 0.475 | Full rollup: collect subjects → classify → build profiles/projects/weekly/alerts → write markdown |
| 2 | `ingest/jira.py::main` | 4 | 26 | 2 | 0.465 | Jira incremental ingest: cursor → search → normalize → store → cursor advance |
| 3 | `ingest/github.py::main` | 4 | 29 | 2 | 0.465 | GitHub incremental ingest: PRs + reviews + comments + commits per repo |
| 4 | `ingest/confluence.py::main` | 2 | 18 | 2 | 0.445 | Confluence incremental ingest: pages → comments, team-filtered |
| 5 | `derive/narrative.py::narrate_people` | 2 | 17 | 1 | 0.444 | Per-person narrative generation loop |
| 6 | `derive/llm_classifier.py::classify_subjects` | 3 | 19 | 1 | 0.393 | Two-pass LLM classify: pass-1 title/body, pass-2 with diff if needed |
| 7 | `derive/diff_fetcher.py::fetch_diff` | 2 | 10 | 1 | 0.370 | Cache-then-fetch PR diff for pass-2 classifier |
| 8 | `derive/manual-rollup.sh::phase_dump` | 1 | 2 | 1 | 0.360 | Session-mode: dump pending after auth-stripped rollup |
| 9 | `derive/manual-rollup.sh::phase_apply` | 1 | 2 | 1 | 0.360 | Session-mode: validate + insert verdicts + re-rollup |
| 10 | `ingest/common.py::get_db` | 1 | 2 | 1 | 0.360 | Open SQLite + bootstrap schema (called by every flow above) |
| 11 | `derive/dump_pending.py::main` | 1 | 4 | 1 | 0.360 | Dump pending classification + auto-slug new Epics |
| 12 | `derive/dump_pending_narrative.py::main` | 1 | 4 | 1 | 0.360 | Dump pending narratives |
| 13 | `derive/apply_verdicts.py::main` | 1 | 4 | 1 | 0.370 | Validate + insert verdicts into `subject_summary` |
| 14 | `derive/apply_narratives.py::main` | 1 | 2 | 1 | 0.485 | Validate + insert narratives into `person_narrative` |
| 15 | `bin/backfill-confluence-titles.py::main` | 1 | 3 | 2 | 0.435 | Backfill page titles |
| 16 | `bin/backfill-jira-epics.py::main` | 1 | 2 | 2 | 0.435 | Backfill epic links |
| 17 | `bin/discover-jira-epics.py::main` | 1 | 2 | 2 | 0.445 | Discover Jira epic hierarchy |
| 18 | `bin/migrate-commit-actors.py::main` | 1 | 4 | 1 | 0.422 | One-shot actor re-resolution |

### 3.1 Primary critical-path flow (rollup → markdown)

```
derive/rollup.py::main
  ├─ load_projects()              ← config/projects.yaml
  ├─ load_people()                ← config/people.yaml
  ├─ ingest/common.py::get_db()   ← index/events.db
  ├─ collect_subjects()           ← SQL over events table, joined with subject_summary cache
  │     └─ ingest/common.py::_resolve_person
  ├─ llm_classifier.ensure_schema()
  ├─ llm_classifier.classify_subjects()
  │     ├─ _load_cached()         ← subject_summary
  │     ├─ _content_hash()
  │     ├─ _projects_context()
  │     ├─ _tools()
  │     ├─ _call_claude()         ← only when auth NOT stripped
  │     ├─ _parse_tool_calls()
  │     ├─ _verdict_from_tool()
  │     ├─ _apply_epic_anchor()
  │     ├─ _persist()             → subject_summary
  │     └─ _fallback_classify()   ← when auth stripped (cron / manual-rollup)
  ├─ build_person_profile()        → derived/people/<handle>.md
  ├─ build_project_rollup()        → derived/projects/<slug>.md
  ├─ build_weekly()                → derived/weekly/<YYYY-Wnn>.md
  ├─ build_alerts()                → derived/alerts.md
  └─ _emit_pending_slug_creation() → state/pending_slug_creation.json (LLM slug pipeline)
```

### 3.2 Chat-classify flow (after 2026-05-12 algo-kill)

```
/rollup [days]   (slash command in .claude/commands/rollup.md)
  │
  ├─ Phase 1 ── derive/manual-rollup.sh dump
  │                 ├─ run_rollup()        ← ANTHROPIC_* stripped → keyword fallback only
  │                 └─ dump_pending.main()  → state/pending_classification.json (+ rules.md)
  │
  ├─ Phase 2 ── chat reads rules.md FIRST → reads pending JSON → classifies
  │                 │  thin GitHub PRs: `gh pr diff <num> --repo <owner/repo>` inline (needs_diff flag is dead)
  │                 └─ writes state/verdicts.json
  │
  └─ Phase 3 ── derive/manual-rollup.sh apply
                    ├─ apply_verdicts.main()        ← validate + INSERT subject_summary (incl. ownership cols)
                    ├─ ownership_corrections.main()  ← deterministic ownership post-pass (idempotent; 6 hard rules)
                    ├─ cluster_ownership_rollup.apply() → topic_brief.owner_distribution_json
                    └─ run_rollup() again            → cache-hit, fast path, archives verdicts
```

Removed 2026-05-12: `derive/algo_classify.py` (algorithmic bulk path with `CMR_BODY_HINTS`) + `/bulk-rollup` slash command. All semantic classification now flows through chat. See `project_chat_only_classification.md`.

### 3.2a Ownership classification (added 2026-05-29)

Each subject carries a TEAM-OWNERSHIP verdict alongside its domain classification. Chat emits `owned_by_primary` (one team-id from `config/teams.yaml` 26-team enum), `co_owners[]`, `owned_by_confidence`, `ownership_reasoning`. `apply_verdicts` validates against the enum + nulls ownership below 0.6 confidence (domain classification independent). `subject_summary` gained 4 cols: `owned_by_primary`, `co_owners_json`, `owned_by_confidence`, `ownership_reasoning`.

**Content-first post-pass** — `derive/ownership_corrections.py` resolves ownership from the WORK, not who posted (idempotent). Signal priority:

1. **Structural noise** — slack channel-join/leave → `external`.
2. **Content** — `domains` → owning team(s) via `config/domain_team_map.yaml` (`derive/ownership_resolve.resolve`). Dominant domain = primary; rest = co_owners. Primary mechanism; fixes cross-team recall hole (e.g. year-end-close work mis-attributed to sister team via broadcast-channel author now resolves home by content).
3. **Chat verdict** — kept when subject has no mappable domains.
4. **Identity tiebreaker** — author/root-actor → team, ONLY when content + chat both empty (thin content). Last resort.

`config/domain_team_map.yaml`: `default_team` (home) + sister-team `overrides` + a `review:` list of ambiguous-primary slugs (resolve to default; owner confirms). Census `ownership_audit` block surfaces identity-fallback count + review-domains-in-window each run.

**Cluster rollup** — `derive/cluster_ownership_rollup.py` joins `topic_brief_member ⋈ subject_summary`, aggregates `owned_by_primary` per cluster into `topic_brief.owner_distribution_json`. `ask_engine._topic_brief_row` derives `home_team_owned_pct` so `/ask` + `/retro` filter sister-team noise (Highs ≥0.70, Lows ≥0.30). Refreshed on BOTH triggers: `manual-rollup apply` (subject ownership changed) + `finalize_refresh apply` (cluster membership changed).

### 3.2b MoM retro signal (added 2026-05-29)

`derive/mom_extractor.py` scrapes weekly-sync MoM threads (channel `C0EXAMPLE`) for go-live dates + measured impact (% rollout, ₹, branch counts) that don't form embedding clusters. `/retro` Phase 1m consumes it as a co-primary signal alongside cluster framing — prevents point-in-time deliveries being missed by cluster-only synthesis.

---

## 4. Cross-community coupling

**112 cross-community edges** in the 2026-06-03 build (was 4 in the 26-file core). Detector flags **high coupling (105 edges) between `derive-cmd` and `ingest-fetch`** — known/accepted.

| Coupling | Edges | Why |
|----------|-------|-----|
| `derive-cmd` ↔ `ingest-fetch` | 105 | **(a) derive → ingest:** most `derive/*` modules `from ingest.common import get_db / DB_PATH / _load_people` — shared DB + people helpers (actor_behavior, ask_engine, cluster_*, build_thread_summary, apply_leaves, …). **(b) ingest → derive:** Slack ingest apps import `derive.slack_upsert` / `derive.slack_team` / `derive.slack_backfill_helper`; `ingest/{github,jira}` import `derive.identity_signals`. |
| `bin-read` → `ingest-fetch` | 7 | Backfill/discover one-shots reuse `ingest/{jira,confluence}` REST clients + `get_epic_key`. |

> The original "zero `derive/` → `ingest/` edges" claim no longer holds. The clean ingest→events.db→derive contract still describes the GitHub/Jira/Confluence data path, but is no longer a hard module-dependency boundary (derive imports `ingest.common`; Slack couples both directions at the Python level).

---

## 5. Module reference

### 5.1 `ingest/common.py` — shared event model + DB layer

```python
@dataclass
class Refs:
    people: list[str]
    projects: list[str]
    tickets: list[str]
    pages: list[str]

@dataclass
class Event:
    id: str           # source-prefixed unique key
    source: str       # github | jira | confluence
    event_type: str   # pr_opened, issue_created, page_updated, ...
    ts: str           # ISO 8601 UTC
    actor: str        # canonical github handle
    subject: str      # github: repo#num · jira: KEY · confluence: page-id
    title: str
    body: str
    url: str
    refs: Refs
    raw_path: str     # raw/<source>/YYYY/MM/DD.jsonl#line
    issue_type: str   # jira only: Epic | Task | CMR | Bug | Story | ...
```

Hot functions:
- `get_db(path, timeout=30s)` — WAL + 30s busy_timeout. Auto-calls `_ensure_schema`.
- `_ensure_schema(conn)` — creates `events`, `event_refs`, `events_fts`, `cursors`. Idempotent. Uses `PRAGMA table_info` to add `issue_type` without dropping.
- `enrich_refs(event, projects, people)` — populate Refs from title+body+actor.
- `store_event(conn, event, dry_run)` — `append_raw` + `insert_event` + refs upsert.
- `write_success_date(source)` — touches `state/last_<source>_success.date` (idle-gate marker). Written only on clean exit; **not** by `--reset-cursor`.

### 5.2 `ingest/jira.py` — Jira REST → Event

`JiraClient`: bearer-auth, paginates via `next_page_token` (Jira API v3).
- `get_epic_key(issue)` — pulls `customfield_10014` (epic link field).
- `normalize_issue_created(domain, issue)` — extracts `f["issuetype"]["name"]` → `Event.issue_type`. Title prefixed `[EPIC-KEY] …` via `_prefix_epic` for classifier visibility.
- `normalize_changelog_entry` — splits one Jira history blob into N status/assignment events.
- `_flatten_adf(node)` — recursive flatten of Atlassian Document Format → plain text for FTS.
- `ingest_project(client, domain, project, since, ...)` — per-project loop. JQL `project = X AND updated > since`.

### 5.3 `ingest/github.py` — GitHub REST → Event

`GitHubClient`: token-auth, `paginate` follows `Link: <…>; rel="next"`.
- `_email_to_github(email)` — resolves commit author email via `config/people.yaml`. Falls back to email-local-part if no map hit.
- `normalize_pr` — handles `merged_by: null` (list API limitation); separate `pr_merged_by` event emitted by `ingest_repo` for merged PRs.
- `normalize_pr_commit` vs `normalize_commit` — first is commit-in-PR (linked to PR subject), second is direct push (subject = repo#sha).
- `fetch_commit_diff(client, repo, sha, log)` — optional diff fetch (`--with-diff` only; not on default cron path).
- `ingest_repo(repos, since, ...)` — main loop. Fetches PRs, then per-PR fetches reviews + comments + commits + merged_by.

### 5.4 `ingest/confluence.py` — Confluence REST → Event

`ConfluenceClient`: bearer-auth. `get_page_title` separated for backfill use.
- `load_team_account_ids()` — reads `jira_id` from every person in `config/people.yaml`. Non-team account IDs silently skipped.
- `normalize_page(domain, page, is_first_version)` — branches `page_created` vs `page_updated` on version number.
- `ingest_pages` / `ingest_comments` — two loops; comments fetched per-page to associate page title for context.

### 5.5 `ingest/backfill-jira-issue-type.py` — issue_type backfill

One-shot. Reads all Jira subjects where `issue_type IS NULL OR issue_type = ""`. Batches 50, hits Jira `/rest/api/3/search/jql?fields=issuetype`, updates in place. Already run once: 2278 subjects updated (143 Epic / 685 CMR / 1322 Task / 90 Bug / 34 Story / 4 IAI).

**Companion `derive/jira_backfill_status.py`** — backfills `to_status` on existing jira `status_change` events (legacy rows pre-column). Walks changelog per issue, sets terminal `to_status`. Idempotent; skips populated rows.

### 5.5a `ingest/slack_api_client.py` — Slack Web API → ParsedMessage

`SlackClient`: tier-3 rate-limit (45/min internal throttle, headroom under 50/min hard cap), `Retry-After`-aware 429 handler, exponential 5xx backoff. Methods: `auth_test`, `conversations_info`, `history`, `replies`, `iter_history`, `iter_replies`, `users_info`, `iter_users_list`, `build_users_cache`, `usergroups_list`, `build_subteams_cache`.

`build_users_cache(force_refresh=False)` — disk-cached at `state/slack_users_cache.json` (24h TTL). Cold ~24s for 6747 users; warm ~0s. `name_resolver` closure does lazy `users.info` for misses with negative-cache for deactivated users.

`api_message_to_parsed(msg, users_cache, name_resolver, subteams_cache)` — JSON → `ParsedMessage`. Expands `<@U…>` → `<@U…|Name>` via `_expand_mentions`, `<!subteam^S…>` → `<!subteam^S…|@handle>` via `_expand_subteams`, captures `files[]` into `files_json` via `_files_to_struct`, sets `is_bot`/`edited`/`reactions_json`/`reply_count`.

Fail-loud: `_assert_auth_clean()` refuses to run if `ANTHROPIC_API_KEY` set or `SLACK_USER_TOKEN` absent / not `xoxp-` prefix.

### 5.5b `ingest/slack_backfill_app.py` — one-shot channel backfill

CLI: `python -m ingest.slack_backfill_app <channel> [--days N|all] [--dry-run] [--no-threads] [--cursor-mode resume|fresh|force]`.

Channel resolved from `config/slack_channels.yaml`. `is_im` hard-skip, `is_mpim` skip unless yaml has `allow_mpim: true`. Two phases: history paging (no PAGE_CAP — drains the window) → thread-reply paging for `pending_thread_parents` + `stale_thread_parents`. Writes cursor to `state/slack_cursors.json` post-success.

**`ingest_mode: team_involved` (Tier 3; subteam + bot-root extension 2026-05-28):** when yaml row has `ingest_mode: team_involved`, `fetch_history` invokes `is_team_involved(actor_id, body, team_slack_ids, team_subteam_ids)` per top-level message. Matches three patterns: (a) author is a team UID, (b) body has `<@U…>` for a team UID, (c) body has `<!subteam^S…>` for a subteam id from `config/team_subteams.yaml`. Non-team / bot-authored parents with `reply_count > 0` get their thread fetched inline; thread retained if ANY reply satisfies the filter. **Bot-skip for root is deferred until after the reply walk** — incident-alert templates (PagerDuty, OpsGenie, AlertBot) are bot-authored but the team triages in replies; dropping the root early orphans high-signal threads. Batches of 200. Default mode = `full` (omit field).

### 5.5c `ingest/slack_ingest_app.py` — steady-state cron path

CLI: `python ingest/slack_ingest_app.py [channel] [--dry-run]`. Default = all yaml channels.

Per-channel flow:
- **Auto-bootstrap (Tier 3):** null `read_cursor(channel_id)` → synthesize bootstrap cursor at `now − BOOTSTRAP_LOOKBACK_DAYS (365)`. New yaml channels self-seed without `/slack-backfill`; PAGE_CAP=10 absorbs ~2000 msgs/fire, 1-3 fires of catch-up on a 365d window.
- **`ingest_mode` filter:** `team_involved` → only `is_team_involved(...)` messages upserted. Whole-thread keep + bot-deferred root retention as in backfill.
- Phase 2b: top-level history since cursor, `PAGE_CAP=10` pages (~2000 msgs/fire); spillover next fire.
- Phase 2.5: stale-thread reconcile via `derive/slack_backfill_helper.py` (pending + stale lists), `STALE_CAP=50` parents/fire.
- Phase 2.7: trailing-window (24h) edit/delete reconcile for top-level via `reconcile_window_capped`. `history(oldest=now-24h)` → `reconcile_window` upserts edits + tombstones deletes.
- Phase 2.7b: reply-edit reconcile — for window parents with `reply_count > 0` (`RECONCILE_THREADS_CAP=25` most-recent), `iter_replies` → `upsert_event` (body-diff catches edits).
- Phase 3: `build_thread_summary.py` refresh if any inserts/edits.
- Cursor advance to newest_ts if any messages fetched (never backwards).

Sentinel log lines (`Slack ingest starting...`, `Done. total_new=N total_dup=0 edits=N deletes=N`) parsed by `bin/cron-status.sh::parse_runs`.

### 5.5d `ingest/slack_mpim_oneshot.py` — explicit-consent MPIM ingest

Bypasses yaml + `is_mpim` hard-skip via `--confirm-mpim`. Refuses `is_im` always. Optional `--persist-cursor` writes newest_ts to `state/slack_cursors.json` for recurring ingest (pair with yaml row + `allow_mpim: true`).

### 5.5e `ingest/run-slack.sh` — launchd wrapper

No daily-success gate (slack volume justifies every-fire). Invokes `slack_ingest_app.py` directly (NOT `-m`; launchd cwd doesn't guarantee package on sys.path). Post-fire refreshes `state/last_slack_validate.json` via `derive/slack_validate.py --json`. Launchagent `com.example.slack-ingest` hourly at :00 IST hours 12-22 (widened from :00/:30 on 2026-06-03: a full ~30–40min sweep overran the 30min spacing → launchd SIGTERMed the prior fire).

### 5.6 `derive/rollup.py` — main derivation driver

17 top-level functions. Major flows:
- **`collect_subjects(conn, since, ...)`** — SQL joining `events`, `event_refs`, `subject_summary`. Propagates `issue_type` to `SubjectInput`. Returns (subject, source, event_type, title, body, issue_type) tuples.
- **`build_person_profile(conn, actor, since, ...)`** — 279 LOC. Per-person markdown: activity counts, domains (author/reviewer/owner), per-domain item list with MatterAI summaries, recent PRs (paginated), top reviewers, narrative block from `person_narrative`.
- **`build_project_rollup(conn, proj, since, ...)`** — 74 LOC. Per-domain markdown: PR/ticket/page counts, contributor leaderboard, recent items.
- **`build_weekly(conn, ws, we, ...)`** — 84 LOC. Team weekly stats with `percentile` cycle-time.
- **`build_alerts(conn, people, ...)`** — 86 LOC. Stale PRs, drive-by merges, no-review merges. Banner if fallback classifier ran.
- **`_emit_pending_slug_creation(unmapped_ctx)`** — writes `state/pending_slug_creation.json` + rules.md when child subjects reference Jira epics absent from `projects.yaml`. Context (epic title + 15 most recent child titles/bodies) consumed by `/slug-epics`, which synthesises human-readable slugs + bigram keywords (+ optional `merge_into`). `derive/apply_epic_slugs.py` folds verdicts into `projects.yaml` and invalidates `subject_summary` rows for the epic + children so they re-anchor next rollup. Replaces prior `_persist_auto_slugs` (`epic-<key>` fabricated slugs).

### 5.7 `derive/llm_classifier.py` — central classifier module

**Do not delete** — shared types/schema/helpers used by 6 files. Even after the chat-only policy (2026-05-12), `SubjectInput`, `SubjectVerdict`, `ensure_schema`, `_content_hash`, `extract_epic_key`, `_apply_epic_anchor` are imported as `lc.*` everywhere.

Public API:
- `SubjectInput(subject, source, event_type, title, body, matterai_summary, issue_type, ...)` — input record.
- `SubjectVerdict(subject, content_hash, domains, summary, risk_flags, confidence, ...)` — output record.
- `ensure_schema(conn)` — creates `subject_summary` cache table.
- `classify_subjects(conn, subjects, projects, ...)` — two-pass classifier. **When auth env stripped** (cron + manual-rollup), short-circuits to `_fallback_classify` (keyword-match against `projects.yaml` keywords only; no LLM call).
- `extract_epic_key(text)` — regex `\[([A-Z]+-\d+)\]` over title prefix.
- `_apply_epic_anchor(v, epic_key, epic_to_slug)` — guarantees epic's slug appears in verdict.domains.
- `_call_claude(client, ..., max_retries)` — **raises RuntimeError on retry exhaustion** (fail-loud; was previously silent fallback).

Pass logic:
- **Pass 1** — title + body + MatterAI summary + epic context → tool-call verdict.
- **Pass 2** — for `needs_diff: true`: `diff_fetcher.fetch_diff(subject)` → re-classify with diff. Post-2026-05-12 the chat-classify path bypasses `needs_diff` and fetches diff inline via `gh pr diff` (flag dead in chat path; alive in script path for legacy completeness).

### 5.8 `derive/narrative.py` — per-person narrative (LEGACY)

> **Superseded 2026-05-22** by `person_profile.py` + `person_deepread.py` + `ask_engine.py` (§5.8a–c). Still wired in `manual-rollup.sh narrate-dump|narrate-apply` but **off by default** (`NARRATIVE=0`). Kept for `derived/people/<handle>.md` compat; removed once consumers migrate to `management/narratives/per-person/*.md`.

7 signal dataclasses (`AuthoredPR`, `GivenReview`, `JiraOwned`, `JiraTransitioned`, `JiraCommented`, `ConfluencePage`, `PersonSignals`). `build_signals(conn, actor, since, ...)` heavy SQL aggregation. `narrate_people(conn, actors, since, ...)` loop + cache via `person_narrative` keyed by `(actor, window_days, content_hash)`.

### 5.8a `derive/person_profile.py` — deterministic per-person signals (schema v3)

Replacement for `narrative.py`. **Deterministic, not LLM-emitted.** Reads `events.db` + `topic_brief` clusters + `config/tier_expectations.yaml` reliability gates. Emits JSON for `/ask person_range` to render.

Output blocks:

| Block | Purpose |
|-------|---------|
| `person, tier, window, aliases` | header |
| `contribution` | substantive + raw counts for PR reviews / jira comments / slack replies (raw–substantive split = review-volume vs depth) |
| `behavioral` | window-scoped first-responder + resolver counts + p50/p90 reply latency; `work_hours` (12-20 IST) for `after_hours_share` |
| `throughput` | feature track (sp_completion, sprinted_tickets, cancelled_share) + ops track (cmr_share, ops_band) + gates `sp_coverage_min` (0.70), `cmr_share_threshold` (0.30), `min_sprinted_tickets_for_verdict` (5). Emits `tier_deviation` / `ops_track_deviation` / `verdict_suppressed_reason` |
| `quality` | risk-flagged PRs, reverts, drive-by merges |
| `narrative` | jira_metrics: `domain_ownership`, `by_sprint`, `team_rank`, `attribution_chain`, `ops_tickets`, `risk_flagged_prs` |
| `fate` | per-PR `shipped/abandoned/in_flight` + `days_to_terminal` + `terminal_in_window`; per-ticket `in_flight at until` vs status at `until+lookahead_days`; `pr_cycle_median_days`, `slow_pr_count_over_14d`, `same_day_pr_count` (real pace from PR opened→merged ts) |
| `lookahead` | `compute_lookahead_throughput` + `compute_lookahead_ownership` on extended window (`until + lookahead_days`) — surfaces window-edge bias |

Hot fns: `compute_contribution`, `compute_behavioral`, `compute_throughput`, `compute_velocity` (workflow-limited — see §10.7), `compute_pr_fate`, `compute_ticket_fate`, `compute_lookahead_throughput`, `compute_lookahead_ownership`, `compute_narrative_signals`.

### 5.8b `derive/person_deepread.py` — one-shot bundle + disk cache

Replaces 5–6 round-trip fetches (profile + clusters + tickets + PRs + Confluence + comments + slack threads) with one CLI call. Cache at `state/cache/person_deepread/<sha1(name|since|until)[:12]>.json`. Stale check: `cache.stat().st_mtime >= events.db.stat().st_mtime`. `--no-cache` busts. Logs `[cached]` or `[computed → cached]` on stderr.

Returns: `{profile, clusters[], assigned_tickets[], prs[], confluence[], jira_comments[], slack_threads[]}`. Consumed by `/ask person_range` as CITATION material (V3 is the primary engine — §5.8b-ii).

### 5.8b-i `derive/person_census.py` — per-person coverage census (V2 discovery)

Person-scoped analogue of `retro_census`: enumerates EVERY subject the person authored/assigned/participated-in (no top-N cap), partitions by signal-type (reusing retro_census detectors), proves coverage (`coverage_ok`, unclassified=0). Emits `sections` (shipped/fixed/responded_to/designed/built/ops, each primary=own vs contributed), `own_by_signal`, `window_edge` (pre-window deliveries). Role tiers: author/assignee = own; participant = contributed. Oncall-rota → `ops_duty` (excluded from shipped).

### 5.8b-ii `derive/person_v3.py` — merged per-person bundle (PRIMARY for /ask person_range)

Orchestrates V2 discovery (`person_census`) + V1 rating (`person_profile`) + cluster workstreams + TRACK-ROUTING. `_classify_track` from own-work signal mix: feature (authored PRs + PR-owned domains) / platform (design + CMR) / ops (incident); `_baseline_role` over trailing 120d for role-stability. `window_work_mix` per-window; `baseline_role_120d` stable role. `feature_yardstick_applicable` gates whether V1's feature tier verdict headlines (false for platform/ops → evaluate on delivery evidence, not feature-SP). Fixes V1 systematically mis-rating platform/design engineers `below-band`. Comparison harness: `derive/compare_person_versions.py` → markdown.

**Complete render-contract (2026-06-03 holistic fix).** `build_v3` emits every signal the narrative needs as a named top-level field so `/ask` synthesis never re-derives field names from raw deepread JSON nor hand-runs probes:
- `contribution` — engagement shape (substantive_pr_commits/commits-in-PR, pr_reviews_total, confluence_edits, substantive_slack_replies, cross_surface_breadth; high commits-in-PR / 0-own-PRs inversion is a real signal)
- `behavioral` — first_responder/resolver/p50/p90/after_hours/weekend/thread_followup + samples
- `pace` — pr_cycle_median_days, slow>14d, same_day, fate counts
- `quality` — matterai p50, critical_flags, reverts
- `completion` — primary + lookahead sp_completion
- `project_footprint` — renders ALL `top_role_in_project==AUTHOR` slugs regardless of rank (low-event sole-ownership slug not truncated)
- `review_concentration` — top cluster by person's review/comment/commit_in_pr events ("reviewer-of-record"; formerly a manual SQL probe)
- `role_drift[]` — projects where window-role differs from lifetime-role (DECIDER→RESPONDER handoff)
- `v1_signals` adds `team_median_sp`/`team_top_sp`/`team_sp_count` (rank context), `risk_flagged_prs` (Gaps), `attribution_chain` (SP caveat when creation_fallback > changelog), `ops_track_deviation`/`verdict_suppressed_reason` (OPS-band verdict — correct yardstick for CMR-heavy engineers, rendered instead of suppressed feature verdict)
- top-level `ticket_fate.resolved_in_lookahead` lists tickets closed just after window

**Lookahead completion is denominator-consistent** with primary: `compute_lookahead_throughput` calls `compute_throughput` with `until` extended by lookahead_days — same `shipped/(shipped+in_flight+cancelled)` denom. So a primary-vs-lookahead gap is window-edge timing, and a V1-doc-vs-V3-doc completion-% difference is stale-snapshot vs current-data, not a metric bug. `--format summary` prints all of them.

**Role reconciliation:** `_workstreams` derives role from the WINDOW role (`_infer_window_role`, same method as `compute_project_footprint`) not lifetime `participants_json` role — so workstreams and footprint never disagree; `lifetime_role` + `role_drift` carried alongside. The matching `/ask` render-contract + completeness-gate (in `ask.md`) forbids silently dropping any block.

### 5.8c `derive/ask_engine.py` — cluster window-state + lifetime-overlap

Replaces "active in window = `last_activity_ts` in window" with **lifetime-overlap**: `first_ts < until AND last_activity_ts >= since`. Each result decorated with derived `window_state`:

| Value | Meaning |
|-------|---------|
| `fully_in` | created and last-touched inside window |
| `started_in` | created inside, still active after window end |
| `ended_in` | predates window, terminal inside |
| `spans` | predates window AND last-touched after window end |
| `pre_window` | predates and terminal before window (filtered out by default) |
| `post_window` | created after window end (filtered out) |

Fns: `_compute_window_state(first_ts, last_ts, since, until)`, `clusters_active_in_window(...)`, `root_causes_in_window(...)`. Optional `include_recurring` flag.

**Use `window_state` (not `topic_brief.status`) for historical retros** — status reflects NOW; window_state reflects the asked range (§10.6).

`_topic_brief_row` also returns `owner_distribution_json` + derived `home_team_owned_pct` (= dist["home-team"]) on every cluster — consumed by `/retro` + `/ask` ownership filters.

### 5.8c-i `derive/ownership_corrections.py` — content-first ownership post-pass

Runs after `apply_verdicts` in `manual-rollup apply`. `correct(conn)` resolves ownership per subject by priority: (1) slack channel-join/leave → `external`; (2) **content** — `domains` → team(s) via `ownership_resolve.resolve`; (3) chat verdict if no domains; (4) identity tiebreaker (author/root-actor → team) only when content+chat empty. Idempotent; `--dry-run` reports. Emits basis stats (content/chat/identity/noise/unresolved) — identity count = thin-content residual.

### 5.8c-i-bis `derive/ownership_resolve.py` + `config/domain_team_map.yaml` — domain→team

`resolve(domains, chat_primary, chat_co, map)` → `(primary, co_owners, basis)` where basis ∈ {content, chat, none}. Map = `default_team` (home) + sister `overrides` (primary + co) + `review:` ambiguous-primary slugs. Content-first means a sister-team dev's PR on a home domain is home work (by domain), not the author's team — the principle that fixed year-end mis-attribution.

### 5.8c-ii `derive/cluster_ownership_rollup.py` — per-cluster ownership

Joins `topic_brief_member ⋈ subject_summary`, aggregates `owned_by_primary` → `topic_brief.owner_distribution_json` (NULL members → `(unowned)`). Importable `apply(conn)` for pipeline hooks (manual-rollup + finalize_refresh); `main()` for CLI with home-team bucket stats.

### 5.8c-iii `derive/mom_extractor.py` — weekly-sync MoM scraper

Read-only retro helper. `collect_moms(conn, since, until, channels, max_replies)` returns MoM threads (root_body + replies + slack permalink) from `MOM_CHANNELS` (default `C0EXAMPLE`). Title filter: `MOM_TITLE_PATTERNS` minus `MOM_ANTI_PATTERNS` (drops "please join" reminders). Consumed by `/retro` Phase 1-enrich.

### 5.8c-iv `derive/retro_census.py` — coverage-guaranteed retro census

`/retro` **Phase 1** primary discovery engine (recall by construction, not feed-sampling). `build_census(conn, since, until)` enumerates EVERY window subject and partitions exhaustively by ownership (`team/sister/external`) × signal-type. Signal detectors STRUCTURAL-first (channel role for incidents, jira `issue_type`, source, `status→Done`), keywords as fallback — so phrasings no keyword catches still land (on-call-channel threads = incidents regardless of wording). Emits `coverage_ok` + `unclassified` (HARD GATE: must be 0), `incidents[]`/`rollouts[]` sub-censuses with evidence URLs + rollout `confirmed`, `buckets` (candidate pools), `ownership_audit` (identity-fallback residual + review-domains), `window_edge[]` (delivery candidates whose terminal status predates the window — synthesis must not claim these as in-window Highs). Terminal/delivered states config-driven from `tier_expectations.yaml::status_classes` (shared with `person_profile`, substring-tolerant for emoji variants like `Change Released 🧩`). LLM judges signal WITHIN the complete candidate set + emits reconciliation appendix.

### 5.8d `config/tier_expectations.yaml` — reliability gates + windows

Source of truth for `person_profile.py`. Schema:

| Block | Keys |
|-------|------|
| `work_hours` | `start_hour=12, end_hour=20, timezone_offset_minutes=330` (12-20 IST) |
| `window` | `lookahead_days=30, lookbehind_days=0, fate_max_days=90` |
| reliability gates | `sp_coverage_min=0.70`, `cmr_share_threshold=0.30`, `min_sprinted_tickets_for_verdict=5` |
| tier bands | SDE1 `0.80-0.90` / SDE2 `0.70-0.80` / SDE3 `0.50-0.60` sp_completion |
| `status_classes` | `shipped / ops_closed / cancelled / in_flight` |
| `ops_band` | `3-6` CMRs/sprint |

### 5.9 `derive/diff_fetcher.py` — pass-2 diff cache

`GitHubDiffClient.files(repo, num)` → `/pulls/{num}/files`. `_cache_path(subject)` → `state/diff_cache/<safe-subject>.json`. `fetch_diff(subject)` is the single public entry.

### 5.10 `derive/dump_pending.py` — pending dumper + auto-slug

- `_detect_new_epic_slugs(records, ...)` — filters `issue_type == "Epic"` (was over-applying to all jira types → 13 bad CMR-derived slugs).
- `_slug_from_title(title, fallback_key)` — kebab-case from title, fallback to epic-key only if title empty.
- `_keywords_from_title(title)` — **bigrams only** (unigrams caused `transaction-failure-enhancement-customer` to match 525 unrelated subjects).
- Sort order: Epics → non-Epic jira → confluence → github.
- `_load_team_enum()` + `_rules_md()` embed the ownership schema (team-id enum from `config/teams.yaml`, team descriptions, author→team table, ownership decision tree) into `pending_classification.json.rules.md` so chat emits ownership fields per subject.

### 5.11 `derive/dump_pending_narrative.py` — narrative dumper (LEGACY)

> **Superseded by §5.8a–c.** Kept for `manual-rollup.sh narrate-dump`. Not invoked by `/ask` or `/retro`.

`_active_actors(conn, since, team_handles)` — uses **full identity set** from `config/people.yaml` (mirrors `rollup.py`), not just github handles (previously dropped jira+confluence-only actors).

### 5.12 `derive/apply_verdicts.py` — verdict validator + writer

`_validate(v, pending, slug_set, epic_to_slug, team_id_set)`:
1. Slug enum check (`domains` ⊆ `projects.yaml` slugs).
2. Risk flag enum check (`{security, data-loss, panic, race, migration, breaking-api}`).
3. Summary length cap (200 chars, truncated with `…`).
4. **Reject if `confidence < 0.7`** → subject stays pending, re-emerges next dump.
5. **Reject if `needs_diff: true`** → flag dead in chat path; rejection forces inline diff fetch.
6. Re-apply epic anchor via `lc._apply_epic_anchor`.
7. **Ownership validation** — `owned_by_primary` + `co_owners` ⊆ `teams.yaml` enum (`_load_team_ids`); reasoning capped 300 chars; ownership NULLED if `owned_by_confidence < 0.6` (domain classification kept independently).

Writes accepted verdicts to `subject_summary` — 16 cols incl. `owned_by_primary`, `co_owners_json`, `owned_by_confidence`, `ownership_reasoning`. Followed by `ownership_corrections.py` (§5.8c-i) + `cluster_ownership_rollup.apply()` (§5.8c-ii).

### 5.13 `derive/apply_narratives.py` — narrative writer (LEGACY)

> **Superseded by §5.8a–c.** Kept for legacy `narrate-apply`.

Minimal — single `main()`. Reads `narratives.json`, INSERTs into `person_narrative`, archives input.

### 5.14 `derive/manual-rollup.sh` — session-mode orchestrator

6 bash functions:
- `preflight` — prints auth status (informational; auth stripped before rollup call).
- `run_rollup` — **explicit `local -x ANTHROPIC_API_KEY="" ANTHROPIC_AUTH_TOKEN=""`** before invoking `rollup.py`. Defends against the 429 issue.
- `phase_dump` → `phase_apply` for verdict cycle. `phase_apply` chains `apply_verdicts.py` → `ownership_corrections.py` → `cluster_ownership_rollup.py` → `run_rollup`.
- `phase_narrate_dump` → `phase_narrate_apply` for narrative cycle.

### 5.15 `derive/run-rollup.sh` — cron entry

Idle-guard via `state/last_rollup_success.date`. Strips auth same as manual-rollup. Fixed `--days 240 --week --skip-narrative`.

### 5.16 `derive/slack_upsert.py` — Slack row writer + reconcile helper

`ParsedMessage` dataclass (10 fields: `actor_id`, `actor_name`, `ts`, `body`, `is_bot`, `edited`, `thread_parent_ts`, `reactions_json`, `reply_count`, `files_json`, `raw_block`).

- `upsert_event(conn, msg, channel_id, thread_parent_ts, slack_users_cache)` — INSERT or UPDATE by `_event_id`. On body-diff or `edited=True`, overwrites body + sets `edited_ts` + re-extracts refs via `enrich_refs`. Reactions refresh silently. Never deletes (reconcile owns tombstones).
- `reconcile_window(conn, channel_id, window_start_iso, api_msgs, thread_parent_ts_map, slack_users_cache)` — two-phase: (1) upsert every api_msg, (2) tombstone (`deleted_ts = now`) any DB row in window whose ts not in api_ts_set. Used by Phase 2.7.
- `_url(channel_id, ts, thread_parent_ts=None)` — clickable `https://<workspace>.slack.com/archives/<ch>/p<ts-no-dot>` (workspace from `sources_config.slack_workspace()`). Reply form appends `?thread_ts=<parent>&cid=<ch>`.

### 5.17 `derive/slack_validate.py` — Slack health validator

Layered per-channel checks: counts, reply_drift, cursor_lag, orphan_replies (PK-based — earlier strftime-on-ISO query rounded high-fraction-second ts up by 1s, false-positive on 29 rows), raw_mentions (regex-strict `<@U…>` without `|name`; rich `<@U…|Name>` legitimate), bot_leaks, dup_ts (refined to same `(ts, event_type)` — thread_broadcast legitimately creates same-ts pair).

Exit codes: 0 clean, 1 findings, 2 env error. `--json` consumed by `bin/cron-status.sh` via `state/last_slack_validate.json`.

### 5.17a `derive/{confluence,github,jira}_validate.py` — per-source health validators

Source-specific siblings of `slack_validate.py`. Same contract: layered per-source checks (row counts, cursor lag, null-field drift, dup detection, orphan refs), exit `0`/`1`/`2`, `--json`. Run ad-hoc or wired into per-source ingest wrappers. Keep in sync with `slack_validate.py` when the schema changes.

### 5.18 `derive/slack_{expand_mentions,backfill_files,backfill_helper}.py`

- `slack_expand_mentions.py` — one-shot retro-fill of legacy `<@U…>` bodies to `<@U…|Name>` using current `users_cache` + `subteams_cache`. Idempotent. Retro-fixed 949 opsgenie rows.
- `slack_backfill_files.py` — one-shot retro-fill of `events.files_json` for rows with `[files: …]` suffix but NULL column. Top-level + thread replies. Filled 168/168 legacy rows.
- `slack_backfill_helper.py` — drain-channel / drain-threads / pending-threads / stale-threads / stale-threads-all / **active-threads** / status / seed-reply-count subcommands. Operational glue; still used for stale-thread derivation in API path. Filename parser regex-fixed to strip MCP cursor-pagination `_c<8hex>` suffix from `parent_ts`. **`active-threads <cid> [--days 90]`** lists parents whose newest *known* reply is within N days (cooldown-filtered via `drain_attempted_at`) — the late-reply fix (§10.9): catches replies on threads whose parent scrolled below the cursor, which both cursor-bound history and the 24h reconcile (gates on parent age) miss. `/slack-ingest` unions this into Phase 2.5; `fetch_threads_capped` re-clamps `reply_count` + stamps `drain_attempted_at` per drained parent so the cooldown throttles each to ~once/day.
- `slack_reparse_empty.py` — one-shot re-derive of `events.body` for rows left empty by the legacy parser (ignored `attachments[]`/`blocks[]`). Re-runs current body extractor over raw payload. Idempotent; targets only empty-body rows.

### 5.19 `derive/slack_team.py` — team-membership helpers

Reads `management/context/team.md` (7 direct reports) → resolves emails to slack_ids via `config/people.yaml`. Reads `config/team_subteams.yaml` for Slack user-group ids the team is addressed by. Public surface:

- `load_team_slack_ids() -> dict[str, str]` — canonical team-membership set ({UID → canonical}).
- `load_team_subteam_ids() -> set[str]` — Slack subteam ids (SID) from `config/team_subteams.yaml`. Empty set if file absent (keeps legacy single-UID behaviour).
- `is_team_involved(actor_id, body, team_slack_ids, team_subteam_ids=None) -> bool` — true if author ∈ team UID, OR body @-mentions a team member (`<@U…>`), OR body pings a team subteam handle (`<!subteam^S…>`). Shared by `slack_ingest_app.ingest_channel`, `slack_backfill_app.fetch_history`, `slack_team_filter_cleanup.py`. `team_subteam_ids` optional — 3-arg callers still work (subteam check skipped).

**Source of truth = `team.md` + `team_subteams.yaml`, not `people.yaml`** — people.yaml carries cross-team identities (32+ entries) and would over-include. `team_subteams.yaml` captures subteam handles the team is paged via (e.g. `S0EXAMPLE` = `service-c-team-devs`/`ex-team`, `S0EXAMPLE` = `service-c-oncall`) — owner-managed because the bot token's `usergroups:read` scope is often not provisioned.

**Bot-rooted incident threads:** `is_team_involved` False on a bot-authored root does NOT immediately drop the message. All three call sites walk the thread's replies first; if any reply satisfies the filter, the bot root is retained — preserves incident-alert headers (PagerDuty / OpsGenie / AlertBot) where the team triages in replies.

### 5.20 `derive/slack_discover_channels.py` — yaml population

CLI: `python derive/slack_discover_channels.py [--auto-mode] [--top N] [--apply] [--json-out PATH] [--min-team-msgs 5] [--min-mpim-msgs 1]`.

Pipeline:
1. `users.conversations` walk → all channels owner is member of.
2. Skip channels already in yaml + bot prefixes (`opsgenie-`, `alert-`, `pagerduty-`, `datadog-`, `github-`, `sentry-`, `jenkins-`).
3. Per candidate, fetch last 90d via `history`, count team-INVOLVED messages via `slack_team.is_team_involved(...)` — counts if **author ∈ team OR body @-mentions a team member OR body pings a team subteam handle**. Subteam-ping coverage surfaces oncall/incident/alert channels (`oncall-stats`, `job-monitoring-alerts`, `rca-retro`, `banking-ops-public`) where the team authors almost nothing.
4. MPIMs: parse `mpdm-<name1>--<name2>--…-N` name, count team handles. Prefix-match for Slack's 80-char truncation. Team handles are Slack `user.name` (e.g. `grace.example`) NOT `slack_handle` display-name — startup builds resolver via per-team-member `users.info` (~7 calls, ~1s).
5. `_decide_mode` decision tree (top-down):

   | Condition | Verdict |
   |---|---|
   | `is_im` or archived | `skip` |
   | **non-MPIM alert channel** (`_is_alert_channel` AND `_name_has_team_domain`) | **`auto_full`** — bypasses floor |
   | `team_msgs < floor` (5 non-MPIM / 1 MPIM) | `needs_review` |
   | `is_mpim` AND `team_handle_count >= MPIM_TEAM_THRESHOLD (3)` | `auto_full` |
   | `is_mpim` AND team_count < threshold | `needs_review` |
   | name matches bot-prefix / `ANNOUNCE_NAME_PATTERNS` | `auto_team_involved` |
   | `team_msgs/total_msgs >= TEAM_RATIO_FULL_THRESHOLD (0.5)` | `auto_full` |
   | `total_msgs >= 20` (low ratio, active) | `auto_team_involved` |
   | else | `needs_review` |

   **Alert-channel branch (2026-06-03):** team-owned alert firehoses (`service-a-alerts`, `example-tracker`, `example-txn-alerts`, …) are bot-authored — team is a member but rarely posts/@-mentions, so author+mention scoring ~0 and the floor would drop them. Gate = `_is_alert_channel(name, bot_ratio)` (name token `alert`/`tracker`/`opsgenie`/`sentry`/`pagerduty`/`notifications`/`-logs`, OR ≥80% bot-authored) **AND** `_name_has_team_domain(name)` (token-aware: whole `-`/`_`-split tokens with startswith for plurals — keywords `accounting`/`recon`/`service-c`/`EX`/`transaction(s)`/`txn`/`service-a`/`account-freeze` + compound `ledger-balance`/`pending_txn`). Mode = `full` (`team_involved` would drop every bot alert). `deposits`/`td` excluded (sister-team). Token-aware matching prevents substring mis-fires like `gl` inside `breakglass`.
6. `--apply` appends `auto_*` rows to `config/slack_channels.yaml` (MPIM rows get `allow_mpim: true`; `team_involved` rows get `ingest_mode: team_involved`). `--json-out` writes proposals for cron-status DISCOVERY block.

Subteam-aware scoring (2026-06-03) moved 34 channels out of `needs_review` in one dry-run (0→20 auto_full + 0→14 auto_team_involved).

### 5.21 `derive/slack_prune_stale_mpims.py` — MPIM hygiene

CLI: `python derive/slack_prune_stale_mpims.py [--quiet-days 30] [--apply]`. Dry-run default.

Per MPIM row (`allow_mpim: true`): `SELECT MAX(ts) FROM events WHERE source='slack' AND channel_id=?`. Decision:
- No events → `GRACE-skip` (just added, hasn't backfilled).
- `days_quiet > DEFAULT_QUIET_DAYS (30)` → prune.
- Otherwise keep.

`--apply` removes yaml row via `_remove_yaml_block(raw, channel_id)` (line-based, preserves surrounding rows + comments) + drops cursor from `state/slack_cursors.json`. **events.db rows untouched** — re-discovery re-adds with fresh auto-bootstrap.

### 5.22 `derive/slack_team_filter_cleanup.py` — one-shot retro purge

For channels switched to `ingest_mode: team_involved` after they had `full`-mode rows: deletes rows not satisfying `is_team_involved`. Whole-thread keep: if any reply in a thread is team-involved, retain the whole thread (incl. bot-authored roots). Loads team UIDs from `team.md` + subteam ids from `config/team_subteams.yaml`, so subteam-pinged threads kept by ingest aren't later purged. Ran 2026-05-26 against opsgenie + on-call → 16813 rows removed. Backups at `state/slack_team_cleanup_*.txt`.

### 5.23 `derive/leaves_dump.py` — leaves Phase 1 (regex prefilter)

Reads last 60d slack events authored by direct reports (owner email excluded). Matches body against `LEAVE_PATTERN` (OOO/WFH/PTO/vacation/sick/etc. word-boundary regex). Slack `events.actor` stores raw `U…` slack_id — resolve to canonical github handle at emit via `_load_team_slack_map()` (people.yaml email → slack_id → canonical). Writes `state/pending_leaves.json` + `.rules.md`.

Dedup gate: events in `team_leaves_processed` skipped (real or false-positive — both prevent re-emission). CLI: `python derive/leaves_dump.py [--days 60] [--reset]`. `--reset` clears `team_leaves_processed` and re-emits everything.

### 5.24 `derive/apply_leaves.py` — leaves Phase 3 (validate + upsert)

Reads `state/verdicts.leaves.json` (chat-emitted via `/leaves`). Per-verdict validation:
- `confidence ≥ 0.7` else REJECT (stays pending)
- `is_leave` must be boolean
- `actor` must be in `pending.team_canonical` (rejects non-team mentions)
- `date_start`/`date_end` ISO or null; `date_end ≥ date_start` if both present
- `reason` ∈ `{wfh, vacation, sick, holiday, ooo, travel, other}` (else coerced to `other`)

Accepted verdicts:
- DELETE existing `team_leaves` rows for the `event_id` (allows re-classify)
- INSERT one row per `leaves[]` entry (a multi-range plan like "5-6 May leave, 7-8 WFH, 11-15 WFH" → 3 rows)
- INSERT/REPLACE `team_leaves_processed` (sets `is_leave=0` for false positives so dump skips them)

Verdicts file archived as `verdicts.leaves.<ts>.json` post-apply.

### 5.25 `derive/render_leaves.py` — leaves Phase 4 (markdown render)

Writes `derived/team-leaves.md`. Idempotent. Sections:
- **Active today** — `date_start ≤ today AND (date_end IS NULL OR date_end ≥ today)`
- **Upcoming (next 30d)** — `date_start > today AND date_start ≤ today+30d`
- **Recent past (14d)** — `date_end < today AND date_end ≥ today-14d`
- **Ambiguous** — `date_start IS NULL` AND mentioned within last 30d

Per-row dedup CTE: `ROW_NUMBER() OVER PARTITION BY (actor, date_start, date_end, reason)` keeps shortest event_id (top-level msg beats thread-context dup). Excerpts whitespace-collapsed, pipes stripped, capped 70 chars. Days column = inclusive count. Link column wraps permalink as `[view](url)`.

### 5.26 `derive/run-leaves.sh` — leaves Phase 1 cron wrapper

Daily 04:00 IST via `com.example.leaves`. Strips `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`. Fires `leaves_dump.py` then `render_leaves.py`. Writes `state/last_leaves_success.date` on clean exit; idempotent within the day. Phase 2 chat-classify is owner-invoked via `/leaves`.

### 5.27 `bin/run-slack-discover.sh` — discover cron wrapper (auto-apply)

Wed+Fri 13:00 IST via `com.example.slack-discover`. Runs `slack_discover_channels.py --auto-mode --top 500 --apply --json-out`. Pre-apply yaml snapshot at `state/slack_channels.yaml.bak.<ts>` for rollback (last 4 retained, oldest LRU-evicted). Post-apply validator refreshes `state/last_slack_validate.json`. JSON proposals feed cron-status DISCOVERY block.

Steady state: discover auto-applies, ingest auto-bootstraps, pruner cleans stale MPIMs weekly — full closed loop, no manual touch.

### 5.28 `bin/housekeeping.sh` step 7 — MPIM pruner cron hook

Mon 03:00 IST via `com.example.housekeeping`. Step 7 runs `python -m derive.slack_prune_stale_mpims --apply` (30-day quiet threshold; events.db rows preserved). Dry-run (`--apply` omitted) is the wrapper default; pass-through `--apply` only when the launchagent fires.

### 5.29 Embedding + topic-cluster pipeline

Produces `embedding` / `topic_brief` / `topic_brief_member` tables (§6.1 + `SCHEMA.md`); feeds `/ask cluster_pulse`, `/retro`, cron-status EMBEDDING lane. Embeddings use OpenAI `text-embedding-3-*` **only**; all reasoning is chat-driven (scripts never call an LLM API).

| Module | Role |
|--------|------|
| `derive/subject_content.py` | Resolves embeddable text for any subject id (`slack:CH:ts` / `EX-NNNN` / `page:NNNN` / `owner/repo#N`). Shared content-fetcher. |
| `derive/embed_subjects.py` | **Step 1.** Embed subjects → `embedding` table. Content-hashed; skips re-embed when `content_sha` + model unchanged. OpenAI only. |
| `derive/cluster_subjects.py` | **Step 2.** Pull vectors, run HDBSCAN (cosine), print tree for eyeball validation. Does NOT persist — the inspection step. |
| `derive/cluster_diff.py` | **Step 2.5.** Match new HDBSCAN clusters to old labelled ones by member-set similarity, so labels survive non-incremental re-clusters (HDBSCAN reshuffles all `cluster_id`s). |
| `derive/label_clusters.py` | **Step 3 (Phase B).** Chat-driven cluster naming (dump → chat labels → `apply` persists to `topic_brief`). |
| `derive/enrich_clusters.py` | **Step 4 (Phase C).** Chat-driven enrichment — decisions / blockers / outcomes / root-cause per cluster. |
| `derive/finalize_refresh.py` | Single-phase orchestrator that **replaces** the separate label→enrich loop: one combined dump + one `apply` covering label + status + enrichment. Auto-invokes `link_clusters_to_projects.py apply` (§6.5). |
| `derive/auto_recurring.py` | Auto-stubs `Recurring …` clusters (channel joins, opsgenie acks, weekly oncall posts) — no real enrichment to extract. |
| `derive/refresh_embeddings.py` | Incremental orchestrator (`/refresh-embeddings`): detect drifted/new subjects → embed → cluster → relabel via diff. Steady-state loop. |
| `derive/link_clusters_to_projects.py` | Deterministic cluster → `projects.yaml` slug linker (§6.5). |
| `derive/embedding_query.py` | Analysis CLI over `embedding`: `neighbors`, etc. Cosine-nearest-neighbour lookups. |
| `derive/validate_embeddings.py` | Full sanity battery over `embedding` (drift / dim / coverage). Backs `/embed-validate`. |
| `derive/topic_brief_validate.py` | Catches the silent gap after `cluster_diff apply` — new/relabel clusters landing with NULL label/status/enrichment. |

### 5.30 Other modules

| Module | Role |
|--------|------|
| `derive/jira_metrics.py` | **Shared** Jira-interpretation primitives — ticket-credit attribution, ops-keyword detection, PR-author ownership, people resolution. Single source of truth; skills consume, never reimplement. |
| `derive/build_thread_summary.py` | Materialises `thread_summary` rows (one per Slack thread). `INSERT OR REPLACE`, idempotent. |
| `derive/build_trd_owners.py` | Materialises `trd_owners` from Confluence events (page-ownership scoring). Hooked from `run-confluence.sh`; idempotent full-table replace. |
| `derive/story_graph.py` | Walks the cross-source link graph in `event_refs` — a "story" = connected component (PR → ticket → page → thread). |
| `derive/slack_ingest_runner.py` | Legacy MCP-path runner: parses a Slack MCP `slack_read_channel`/`_thread` dump → upserts → advances cursor. Used by the `*-mcp` commands. |

---

## 6. Data layer

### 6.1 SQLite tables (in `index/events.db`)

| Table | Owner | Schema (key cols) | Purpose |
|-------|-------|-------------------|---------|
| `events` | `ingest/common.py::_ensure_schema` | `id PK, source, event_type, ts, actor, subject, title, body, url, raw_path, issue_type, story_points, sprint_id, sprint_name, sprint_state, assignee, channel_id, thread_ts, edited_ts, deleted_ts, reactions_json, reply_count, drain_attempted_at, files_json` | Primary event store. **Slack-specific cols** (`channel_id`, `thread_ts`, `edited_ts`, `deleted_ts`, `reactions_json`, `reply_count`, `drain_attempted_at`, `files_json`) populated only for `source='slack'`. `files_json` (Migration 007) = JSON list of `{id,name,mimetype,size,mode,permalink,user}`. |
| `event_refs` | same | `event_id, ref_type, ref_value, role` | One row per refs entry; enables joins by person/project/ticket/page |
| `events_fts` | same | FTS5 virtual over `title + body` | Free-text search |
| `cursors` | same | `source TEXT PK, value TEXT` | Per-source high-water mark (github/jira/confluence; slack uses `state/slack_cursors.json`) |
| `subject_summary` | `derive/llm_classifier.py::ensure_schema` | `subject, content_hash, domains_json, summary, risk_flags_json, confidence, needs_diff, created_at` | Classifier cache (`(subject, content_hash)` PK) |
| `person_narrative` | `derive/narrative.py::ensure_schema` | `actor, window_days, content_hash, body, created_at` | Narrative cache |
| `thread_summary` | `derive/build_thread_summary.py` | `thread_id PK, channel_id, parent_ts, summary, last_built_at` | Slack thread digest (1-line) per parent |
| `team_leaves` | `ingest/common.py::_ensure_schema` | `id PK, event_id, actor, mentioned_at, date_start, date_end, reason, channel_id, channel_name, body_excerpt, url, confidence, extracted_by, classified_at` | Per-leave-range row. One source event can yield N rows. |
| `team_leaves_processed` | `ingest/common.py::_ensure_schema` | `event_id PK, processed_at, is_leave (0=fp, 1=real), confidence` | Dedup gate. Dump skips rows present here regardless of is_leave. |
| `embedding` | `derive/embed_subjects.py` | `subject PK, source, vector BLOB, model, dim, content_sha, computed_at` | One vector per subject (OpenAI `text-embedding-3-*`). `content_sha` detects drift. |
| `topic_brief` | `derive/finalize_refresh.py` (label/enrich) | `cluster_id PK, label, summary, status, decisions_json, blockers_json, …, root_cause, outcomes_json, owner_distribution_json` | One row per topic cluster (cross-source). §5.29. |
| `topic_brief_member` | same | `cluster_id, subject, source, similarity, member_role` (PK `(cluster_id, subject)`) | Cluster membership + centroid distance + role. |
| `thread_enriched` | thread enrichment classifier | `subject PK, channel_id, topic_paraphrase, sentiment, urgency, intent, outcome, decisions_json, blockers_json, cross_source_refs_json, …` | LLM per-thread enrichment (sentiment/intent/outcome). |
| `trd_owners` | `derive/build_trd_owners.py` | `page_id PK, title, owner, owner_score, scores_json, contributors_json, project_slug, last_event_ts, total_events, computed_at` | Confluence page-ownership scoring. Hooked from `run-confluence.sh`. |

> `subject_summary` + `person_narrative` schemas above are abbreviated (both gained columns — ownership cols on `subject_summary`; `source`/`model`/token counts on `person_narrative`). **`SCHEMA.md` carries the authoritative current DDL for every derived table** — defer to it on column lists.

### 6.2 Filesystem state (in `state/`)

| File | Writer | Purpose |
|------|--------|---------|
| `cursors.json` | ingest scripts | Mirror of `cursors` table for shell-readable use (github/jira/confluence only) |
| `slack_cursors.json` | `ingest/slack_backfill_app::write_cursor` | Per-channel cursor (Slack-epoch float string); never-go-backwards check |
| `slack_users_cache.json` | `slack_api_client.SlackClient.build_users_cache` | Disk users.list cache (24h TTL, ~210KB for 6747 users, 24s cold → 0s warm) |
| `last_<src>_success.date` | `ingest/common.py::write_success_date` (gh/jira/confluence) / `slack_ingest_app::write_success_marker` | Daily idle gate; slack writes YYYY-MM-DD on any-channel success but no daily gate applied — cron fires hourly at :00 unconditionally |
| `last_slack_validate.json` | `ingest/run-slack.sh` (post-fire) | Cached `derive/slack_validate.py --json` consumed by `bin/cron-status.sh` |
| `last_rollup_success.date` | `derive/run-rollup.sh` | Rollup daily gate (unused — rollup is manual via `/rollup`; wrapper exists, no LaunchAgent installed) |
| `pending_classification.json` (+ `.rules.md`) | `derive/dump_pending.py` | Chat input for `/rollup` phase 2 |
| `pending_narrative.json` (+ `.rules.md`) | `derive/dump_pending_narrative.py` | Chat input for narrate phase |
| `verdicts.json` | chat session (manual) | Output of `/rollup` phase 2 |
| `verdicts.<ts>.json` | `manual-rollup.sh apply` | Archived verdicts |
| `narratives.json` / `.<ts>.json` | chat / apply | Same pattern for narratives |
| `diff_cache/<safe-subject>.json` | `derive/diff_fetcher.py` | Pass-2 diff cache |
| `cache/person_deepread/<sha1[:12]>.json` | `derive/person_deepread.py` | Bundle cache. Mtime-gated on `index/events.db`. `--no-cache` busts. |

### 6.5 `cluster_project_map` — cluster ↔ projects.yaml slug mapping

| Column | Notes |
|--------|-------|
| `cluster_id` | FK to `topic_brief.cluster_id` |
| `project_slug` | FK to `projects.yaml::slug` |
| `confidence` | 0.0–1.0; `0.95` domain-agreement, `0.90` confluence_page, `0.85` jira_epic, `0.60` keyword |
| `source` | `subject_summary_domains` / `confluence_page` / `jira_epic` / `keyword` |
| `evidence_json` | matched epics / pages / keywords for audit |
| `computed_at` | ISO timestamp |

PK `(cluster_id, project_slug)`. Many-to-many: one cluster may link multiple slugs (e.g. cluster 268 → service-c-txn-misc + db-optimisation + accounting-refactor + service-c-txn-ops + txn-correctness). Populated by `derive/link_clusters_to_projects.py` (deterministic, no LLM). Wired into `finalize_refresh.py apply` so every refresh re-links. Migration: `derive/migrations/008_cluster_project_map.sql`.

**Linker rules (highest confidence wins per slug):**
1. **Domain agreement (0.95)** — ≥50% of mappable cluster members share a slug in `subject_summary.domains`.
2. **Confluence page (0.90)** — cluster has `page:NNNNNNN` member AND `NNNNNNN` ∈ `projects.yaml::confluence_pages[slug]`.
3. **Jira epic (0.85)** — cluster has jira member whose `[Epic XXX-NNN]` title prefix (or subject) ∈ `projects.yaml::jira_epics[slug]`.
4. **Keyword (0.60)** — cluster label/summary substring-hits a keyword from `projects.yaml::keywords[slug]`. Keywords <6 chars filtered.

**Skipped:** `status=RECURRING` clusters, clusters with `member_count<3`.

Unmapped clusters surface `projects.yaml` gaps:
```bash
.venv/bin/python derive/link_clusters_to_projects.py unmapped
```
Owner extends `projects.yaml` and re-runs `link_clusters_to_projects.py apply` (or `finalize_refresh.py apply`, which auto-invokes).

### 6.3 Config (in `config/`)

| File | Schema | Purpose |
|------|--------|---------|
| `sources.yaml` | `{org, atlassian, jira, github, teams, slack, launchd}` | **Central org-identity config (gitignored; `sources.example.yaml` is the committed generic template).** Loaded by `derive/sources_config.py` → falls back to example → per-key env overrides. Single source for host / owner email / repos / project keys / slack workspace / team slugs / launchd prefix — **no org identity hardcoded in code.** |
| `people.yaml` | `{people: [{name, canonical, scope, github, github_aliases, email, jira_id, git_names, slack_id, slack_handle}]}` | Cross-source identity map. `scope` ∈ {team, org, external} — `team` counted, `org`/`external` silenced. Replaced `known_externals.yaml` (deleted 2026-05-26). Self-healed by `identity_reconcile.py` (§7). |
| `projects.yaml` | `{projects: [{slug, name, keywords, jira_epics, confluence_pages}]}` | Domain → keywords/epics/pages. **85 slugs as of 2026-05-15** after the epic-`*` rename backfill (open thread #2). `jira_prefixes` field removed 2026-05-15 (over-tagging). |
| `slack_channels.yaml` | `{channels: [{id, name, allow_mpim?, ingest_mode?, compaction_policy?}]}` | 51 rows (14 manual + 37 auto-discovered). `ingest_mode: team_involved` triggers team-membership filter; default omitted = `full`. `allow_mpim: true` unlocks MPIM channels. |
| `../management/context/team.md` | markdown roster | Source-of-truth for the 7 direct reports. Used by `derive/slack_team.py` to build `team_slack_ids` (resolved via people.yaml). **NOT** the same as the broader people.yaml identity map. |
| `team_subteams.yaml` | `{subteams: [{id: S…, handle, aliases?, notes}]}` | Slack user-group ids the team is addressed by. Loaded by `slack_team.load_team_subteam_ids()` → fed into `is_team_involved()` so `<!subteam^S…>` pings count. Owner-maintained — `usergroups:read` scope often unavailable so auto-discovery may return empty. Entries: `S0EXAMPLE` (service-c-team-devs/ex-team), `S0EXAMPLE` (service-c-oncall). |

### 6.4 Outputs (in `derived/`)

All markdown, regenerated from `events.db` — **never hand-edited**.

| Path | Generator | Cadence |
|------|-----------|---------|
| `derived/people/<handle>.md` | `build_person_profile` | Per rollup run |
| `derived/projects/<slug>.md` | `build_project_rollup` | Per rollup run |
| `derived/weekly/<YYYY-Wnn>.md` | `build_weekly` (only with `--week`) | Per rollup run when flag set |
| `derived/alerts.md` | `build_alerts` | Per rollup run |

---

## 7. Slash commands (in `.claude/commands/`)

| Command | Skill body | Phase coverage |
|---------|-----------|----------------|
| `/ask <route> <args>` | `ask.md` | Sole router for narrative/analytical questions. Routes: `person_range`, `highs_lows` (→ `/retro`), `cluster_pulse`, `signals`. Consumes `person_deepread.py` + `ask_engine.py`. Output: TL;DR-first, bulleted Signals, plain-English (no cluster IDs / metric dumps). |
| `/retro since=<iso> until=<iso>` | `retro.md` | STAKEHOLDER-FACING retro. Hard rules: team-level voice (NEVER dev names), Highs = deliveries only (code-Done ≠ delivery → Lows), measurable impact required, no PR/ticket/cluster jargon. Pulls impact numbers from slack rollout threads. |
| `/rollup [days]` | `rollup.md` | Phases 1–3 of full chat-classify cycle; phase 1.5 gates on `pending_slug_creation.json`, delegates to `/slug-epics` |
| `/slug-epics` | `slug-epics.md` | LLM-driven slug + keyword synthesis for unmapped Jira epics; emits `state/verdicts.epic_slugs.json`; applied via `manual-rollup.sh apply-slugs` |
| `/classify` | `classify.md` | Phase 2 only (when `verdicts.json` wiped or re-classify without re-dumping) |
| `/slack-ingest [channel]` | `slack-ingest.md` | Thin wrapper → `ingest/slack_ingest_app.py`. Owner-invoked or cron-fired |
| `/slack-backfill <channel> [--days N\|all]` | `slack-backfill.md` | Thin wrapper → `ingest/slack_backfill_app.py`. Owner-invoked |
| `/slack-ingest-mcp`, `/slack-backfill-mcp` | `*-mcp.md` | Verbatim MCP-era copies; fallback if API path breaks |
| `/leaves [days]` | `leaves.md` | Phases 1–4 chat-classify cycle for team leaves: refresh pending (regex) → classify → apply → render. Direct reports only. |
| `/dev-style <person>` | `dev-style.md` | Narrates a person's working style from `state/actor_behavior_report.json` (built by `derive/actor_behavior.py report`): first-responder rate, resolver rate, reply latency, domain spread. Standalone or routed from `/ask dev_style`. |
| `/refresh-embeddings` | `refresh-embeddings.md` | Incremental embedding + cluster refresh (§5.29): detect drift → embed → cluster → relabel via `cluster_diff` → re-link to projects. |
| `/embed-validate` | `embed-validate.md` | Runs `derive/validate_embeddings.py` sanity battery + cluster-quality report. |
| `/slack-compact` | `slack-compact.md` | Chat-driven thread compaction — reads `state/slack_compact_pending.json`, writes 1-line digests to `thread_summary`. |

**Removed commands:**
- 2026-05-12: `/bulk-rollup` (algorithmic path killed). See `project_chat_only_classification.md`.
- `/slack-reconcile` — trailing-window edit/delete reconcile now inline in steady-state ingest (`slack_ingest_app` Phase 2.7 / 2.7b, §5.5c).
- 2026-05-22: `/narrative` (Option K1). `/ask person_range` is the sole per-person narrative entry point.

**Owner-curated output paths** (not under `derived/`):

| Path | Generator | Cadence |
|------|-----------|---------|
| `management/narratives/per-person/acme-<handle>-<since>-to-<until>.md` | `/ask person_range` | On-demand per IC |
| `management/narratives/team/<since>-to-<until>.md` | `/ask` team route | On-demand |
| `management/narratives/em/owner-<since>-to-<until>.md` | `/ask` EM route (different shape: planning footprint, mentioners-received, design-review depth, team-level fingerprint) | On-demand |
| `management/retros/<since>-to-<until>.md` | `/retro` | Monthly stakeholder cadence |

---

## 8. Scheduler topology

LaunchAgents (sources of truth in `launchagents/*.plist`, installed by `bin/install-agents.sh`):

| Agent | Plist | Schedule (IST) | Idle gate |
|-------|-------|----------------|-----------|
| github | `com.example.github-ingest.plist` | `:00` and `:30`, 12h–22h | `state/last_github_success.date` |
| jira | `com.example.jira-ingest.plist` | `:00` and `:30`, 12h–22h | `state/last_jira_success.date` |
| confluence | `com.example.confluence-ingest.plist` | `:05` and `:35`, 12h–22h | `state/last_confluence_success.date` |
| slack | `com.example.slack-ingest.plist` | hourly `:00`, 12h–22h | none — fires every slot (volume justifies; idempotent upsert dedups); hourly so a ~30–40min sweep completes before next fire (was :00/:30, overlap-killed) |
| slack-discover | `com.example.slack-discover.plist` | **Wed + Fri 13:00** (`--auto-mode --top 500 --apply --json-out`) | — wrapper writes pre-apply yaml snapshot to `state/slack_channels.yaml.bak.<ts>` (LRU-4) before mutating; auto-applies `auto_full` + `auto_team_involved` rows |
| leaves | `com.example.leaves.plist` | **daily 04:00** | `state/last_leaves_success.date` — Phase 1 (regex dump + render of already-classified rows). Phase 2 chat-classify owner-invoked via `/leaves`. |
| housekeeping | `com.example.housekeeping.plist` | weekly **Mon 03:00** | n/a — step 7 runs MPIM pruner (`slack_prune_stale_mpims.py --apply`, 30d quiet; events.db preserved). |
| codegraph | `com.example.codegraph.plist` | **daily 18:00** (`bin/run-codegraph.sh`) | `state/last_codegraph_success.date` — git ff-if-clean + full `code-review-graph build` for service-a + service-c (~90s, no LLM). Distinct from the hook-driven `code-review-graph update` for the *work-context* graph. Feeds `/ask` code-logic queries. Per-run log: `state/codegraph_<date>.log`. |
| rollup | **(not installed)** | manual via `/rollup` | — |

All agents survive sleep/wake (replay missed fires on wake). gh/jira/confluence retry every fire until first success that day, then idle. Slack ingests every fire (cursor-bound; cheap when quiet).

**Exit-code severity** (gh/jira/confluence): ingest scripts exit `2` when *all* repos/projects/stages fail (total outage → `log.error`), `1` on partial failure (`log.warning`), `0` clean. Wrappers (`run-*.sh`) bump `last_<src>_success.date` only on exit 0 — a failed fire retries next slot. `cron-status` escalates a lane to red `✗ INGEST DOWN` when its last run logged `Cursor NOT updated`.

**Identity self-heal**: every ingest emits observed actor pairs via `derive/identity_signals.record_*` → `identity_signals` table. `derive/identity_reconcile.py` runs after each gh/jira/confluence fire (in the wrapper), back-fills missing `people.yaml` fields. Snapshot at `state/last_identity_reconcile.json`; surfaced in `cron-status` IDENTITY lane + `cron-status identity` drill-down.

**Overrun detection** (`bin/_run_health.py`, shared by cron-status + dashboard): flags a run whose duration ≥ gap to its next scheduled fire — `⚠ near-limit` at ≥80% of the interval, `✗ OVERRUN` at ≥100% (run collides with next fire → launchd SIGTERM, the failure that killed pre-widening slack sweeps). Interval = tightest intra-day gap from plist `StartCalendarInterval` (overnight gaps ignored). cron-status pairs start→Done via `parse_runs` (also returns `inflight_starts`); dashboard pairs in `get_run_health`. In-flight overrun gated on `source_running(src)` (`pgrep -f ingest/<script>`) so a `Done.` line mis-attributed during concurrent-fire log interleaving can't leave a stale open start (falls back to last completed run). Surfaced per-lane (`runtime` row) + HEALTH footer (cron-status) and as a `runtime` badge in each card's worst-state (dashboard).

**Code-graph lane** (`bin/_codegraph_status.py`, shared): reads `state/last_codegraph_success.date` + newest `state/codegraph_<date>.log` → status of the daily 18:00 rebuild (schedule/next-fire, last-run ok/fail, per-repo ✓/✗ with node·edge totals, in-flight). Mirrors the EMBEDDING lane.

**Monitoring surfaces**: `bin/cron-status.sh` reports all sources + ROLLUP / PIPELINE / HOUSEKEEPING / IDENTITY / EMBEDDING / CODE-GRAPH lanes + HEALTH footer. Drill-downs: `cron-status {slack,identity,housekeeping,embedding,pipeline,discover}` and `cron-status html`. Web dashboard: `bin/dashboard.py` (stdlib http.server, no Flask) — per-lane cards, D3 cluster circle-pack, identity-signal time-series, log-tail, expandable channel/discover tables.

---

## 9. Maintenance ops

| Task | Tool | Notes |
|------|------|-------|
| Rebuild graph after refactor | `build_or_update_graph_tool(full_rebuild=true)` | Incremental update via `base=HEAD~1` for daily work |
| Inspect a specific community | `list_communities_tool(detail_level=standard)` | Dumps all member node IDs |
| Trace a call chain | `query_graph_tool(pattern="callers_of"\|"callees_of")` | Review impact analysis |
| Find architectural hotspots | `get_hub_nodes_tool(top_n=N)` | Currently buggy — use `query_graph` as fallback |
| Detect changes pre-review | `detect_changes_tool` | Risk-scored diff analysis |

---

## 10. Open architectural threads

1. **Algorithmic classify removed 2026-05-12.** All semantic classification routes through chat. Bulk-run token cost returns; acceptable tradeoff per `project_chat_only_classification.md`.
2. **Epic-slug pipeline split (2026-05-15).** Two paths produce epic slugs: (a) `dump_pending._detect_new_epic_slugs` → `_persist_auto_slugs` for **in-window** `issue_type == Epic` (mechanical title-bigram); (b) `rollup._emit_pending_slug_creation` for unmapped epic keys referenced by children whose Epic falls **outside** the dump window (LLM via `/slug-epics`). Consolidating (a) into (b) is the next refactor.
3. **"vaccum" typo preserved verbatim** in 5+ slug/keyword fields (source typo from Jira titles). Cosmetic; doesn't affect matching.
4. **`needs_diff: true` flag is dead in chat path** but still wired in `llm_classifier._call_claude` for the now-rare script-path fallback. Consider full removal after one more chat-only rollup cycle confirms zero regression.
5. **Hub/bridge analysis tools** in `code-review-graph` MCP are broken (`'NoneType' object has no attribute 'resolve'`). Fall back to `query_graph` until upstream fix.
6. **Cluster status vs window** — `topic_brief.status` is a NOW snapshot; historical retros (>30d old) need window-scoped status. Current fix (2026-05-22): derive `window_state` per query from `first_ts`/`last_activity_ts` (§5.8c). Candidate (not landed): store `status_at_<window_end>` snapshots so historical reads don't derive on each query.
7. **Ticket lead-time as pace signal is bogus.** Workflow creates and Dones tickets the same day (recorded post-hoc), so per-ticket `lead_time_days` collapses to ~1. `person_profile.py::compute_velocity` documents the limitation. Use PR cycle time (`pr_cycle_median_days`, opened→merged) as the real pace signal (§5.8a `fate`).
8. **Graph not yet regenerated for new modules** — `person_profile.py`, `person_deepread.py`, `ask_engine.py`, `tier_expectations.yaml` not in the §1 node count. Run `build_or_update_graph_tool(full_rebuild=true)` to refresh.
9. **Late-reply blind spot — FIXED 2026-06-03.** Steady-state ingest missed replies landing on a thread *after* its parent scrolled below the channel cursor (e.g. a 12-May thread getting a 20-May reply): cursor-bound history never re-surfaces an old parent, and the 24h reconcile reply-pass (Phase 2.7b) gated on **parent age**. The stale detector couldn't fire either (parent's stored `reply_count` frozen at last drain, no drift), compounded by `fetch_threads_capped` never stamping `drain_attempted_at`. **Fix:** new `active-threads` selector (newest-reply-within-N-days, default 90) unioned into `/slack-ingest` Phase 2.5 for both `full` + `team_involved`; `fetch_threads_capped` re-clamps `reply_count` + stamps `drain_attempted_at` per drained parent (cooldown throttles to ~once/day). Self-healing over the next few fires. Residual: a thread quiet >`ACTIVE_THREAD_DAYS` then revived won't re-drain (acceptable; force via `/slack-backfill`).

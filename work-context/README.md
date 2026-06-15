# work-context

Personal engineering intelligence warehouse. Ingests GitHub PRs, Jira issues, Confluence pages, and Slack threads into a unified SQLite event index, then derives markdown rollups (per-person, per-project, weekly) plus an embedding/topic-cluster layer for AI agents and manual review.

This README = **how to run**. [`ARCHITECTURE.md`](ARCHITECTURE.md) = **how the code is wired** (code graph, communities, execution flows, per-module reference). Regenerate the graph + counts via `mcp__code-review-graph__build_or_update_graph_tool(full_rebuild=true)` after structural changes — its node/flow counts are only as fresh as the last rebuild.

---

## How it works

```
GitHub / Jira / Confluence / Slack
        │
        ▼
   ingest/*.py          normalise → unified Event schema
        │
        ├─► raw/<source>/YYYY/MM/DD.jsonl   append-only raw backup
        └─► index/events.db                 SQLite index + FTS
                │
                ▼
          derive/rollup.py       (--days 240 --week via run-rollup.sh)
                │
                ├─► derived/people/{handle}.md       per-engineer profile
                ├─► derived/projects/{slug}.md        per-domain rollup
                ├─► derived/weekly/{YYYY-Wnn}.md      team weekly summary
                └─► derived/alerts.md                 stale PRs + anti-patterns
```

Every event is normalised to the same schema at ingest time and enriched with cross-source `refs` (people, projects, Jira tickets, Confluence pages). The `derived/` tree is agent-readable markdown regenerated on demand from the index — **do not edit by hand.**

---

## Configuration (before first run)

**No org-specific values are hardcoded.** Everything resolves at runtime from `config/sources.yaml` (gitignored) via `derive/sources_config.py`, which falls back to `config/sources.example.yaml` (generic placeholders) and allows per-key env overrides. A fresh clone with neither set resolves entirely to placeholders — leaks nothing.

Copy the template and fill in your values:

```bash
cp config/sources.example.yaml config/sources.yaml
$EDITOR config/sources.yaml
```

```yaml
# config/sources.yaml  (gitignored)
org:
  email_domain: "yourco.com"
  owner_email:  "you@yourco.com"
  owner_handle: "you"                  # canonical slug for the repo owner
atlassian:
  host: "yourorg.atlassian.net"        # env JIRA_DOMAIN overrides
jira:
  project_keys: ["EX"]                 # env JIRA_PROJECT_KEYS overrides
github:
  org:   "your-org"                    # env GITHUB_ORG overrides
  repos: ["your-org/repo1", "your-org/repo2"]
  handle_prefixes: ["your-org-"]       # logins that mark a repo owner as "ours"
  matterai_bot: "matterai-yourorg[bot]"
  codegraph_repos: ["repo1", "repo2"]  # mirror short-names for the code-graph build
teams:
  home:    "your-team"                 # owner's team slug (matches an id in teams.yaml)
  coowner: "sister-team"
slack:
  workspace: "yourco"                  # → https://yourco.slack.com/... permalinks
  mom_channels: ["C0EXAMPLE"]          # weekly-sync MoM channel ids
launchd:
  prefix: "com.you"                    # reverse-DNS label prefix for your launchd agents
```

Per-run + env overrides still apply: `ingest/jira.py --project PLAT`,
`ingest/github.py --repo your-org/other-repo`; env `JIRA_DOMAIN`,
`JIRA_PROJECT_KEYS`, `GITHUB_ORG`, `GITHUB_REPOS`, `ATLASSIAN_EMAIL`, `SLACK_WORKSPACE`.

**Confluence** filters pages to team members via `jira_id` in `config/people.yaml` —
populate it before first Confluence ingest, or those pages are silently skipped.

### Slack token + channels

1. Generate a Slack User OAuth Token (`xoxp-…`) via Slack admin. Scopes in `runbook/slack-token-rotate.md`.
2. Save to `~/context/.env` as `SLACK_USER_TOKEN=xoxp-…` (gitignored).
3. Edit the channel allow-list in `config/slack_channels.yaml`. Auto-populated via `python derive/slack_discover_channels.py --auto-mode --apply` (scans team-active channels you're a member of, decides ingest_mode, appends). New channels with null cursor auto-bootstrap from `now − 365d` on first ingest fire — no manual `/slack-backfill` needed for typical adds.

(Permalink workspace prefix comes from `slack.workspace` in `config/sources.yaml` — no code edit.)

### Scheduled ingest (launchd)

Run `bin/install-agents.sh` — it materialises the generic plist templates in `launchagents/` (label prefix `com.example`, `__REPO__`/`__HOME__` paths) with your real `launchd.prefix` + machine paths and loads them. Re-run after editing schedules.

### Scheduled routines (Claude Code /schedule agents)

A separate scheduler: Claude Code **routines** (scheduled remote agents) live in `~/.claude/scheduled-tasks/<id>/SKILL.md` and are registered through the scheduled-tasks MCP, not launchd. Their templates + cron manifest are committed under `scheduled-tasks/` (`routines.yaml` holds the cron expression + enabled flag — those aren't in SKILL.md).

The Slack channel + MCP id the routines need are config, not hardcoded: set `slack.standup_channel` and `slack.mcp_server` in `config/sources.yaml` (gitignored; see `config/sources.example.yaml` for the keys). Then bootstrap a new machine with:

```bash
bin/install-routines.sh                                  # reads config/sources.yaml
STANDUP_CHANNEL=<id> SLACK_MCP_SERVER=<id> bin/install-routines.sh   # one-off env override
```

It materialises the templated SKILL.md files (substituting `__REPO__`/`__HOME__` + the config-resolved channel/MCP id; a routine whose `needs` value is unset is skipped, never blanked) and prints the `create_scheduled_task` payloads for Claude to register via MCP (a shell script can't call MCP). Both `cron-status.sh` and `dashboard.py` show a **ROUTINES** section with each routine's cadence, last run, and next fire — read from the app's `scheduled-tasks.json` registry via `bin/_routines.py`.

**Channel yaml schema (`config/slack_channels.yaml`):**

```yaml
channels:
  - id: C0EXAMPLE
    name: service-c-public
    # ingest_mode omitted → 'full' (default; store every message)

  - id: C0EXAMPLE
    name: opsgenie-prod-service-c
    ingest_mode: team_involved    # only msgs where author ∈ team OR @-mentions team UID
                                  # OR pings a team subteam in config/team_subteams.yaml

  - id: C0EXAMPLE
    name: mpdm-frank--eve908-1
    allow_mpim: true              # required to bypass MPIM hard-skip
    ingest_mode: full
```

**Mode semantics:**

- `full` (default) — every message stored.
- `team_involved` — keep only threads where the team participates. Team-involved = ANY of: author on team.md · body @-mentions a team UID (`<@U…>`) · body pings a team subteam handle (`<!subteam^S…>` from `config/team_subteams.yaml`) · OR any reply satisfies the same. **Whole-thread keep:** one team-involved reply pulls the whole thread, including non-team replies and bot-authored incident-alert roots (PagerDuty / OpsGenie / "Alert Incident Commander" templates). Logic: `derive/slack_team.py::is_team_involved`.

**Extra per-channel flag — `no_threads: true`:** skip the per-fire stale-thread reply reconcile (Phase 2.5) for that channel. Top-level messages + edit/delete reconcile still run. Use for bot alert firehoses where reply threads are acks/status noise and re-fetching them every fire dominates ingest wall-time (e.g. `service-a-alerts`, `example-tracker`, `example-recon`). The discovery alert-channel branch sets this automatically. Do NOT set it where threads carry real discussion (incident rooms, CMR-approval channels).

`config/team_subteams.yaml` lists the Slack user-group ids the team is paged via — e.g. `S0EXAMPLE` for `service-c-team-devs` / `example-team`, `S0EXAMPLE` for `service-c-oncall`. Add ids manually (the bot's `usergroups:read` scope is often unavailable, so `usergroups.list` returns empty). Without these ids, threads pinged via subteam handle silently filter out as "not team involved".

**DM/MPIM invariants:**
- 1:1 DMs (`is_im=true`) hard-skipped always — no override.
- Multi-party DMs (`is_mpim=true`) skipped unless yaml row has `allow_mpim: true` (owner consent gate).
- MPIMs auto-pruned after 30d quiet via `python derive/slack_prune_stale_mpims.py --apply` (drops yaml row + cursor; preserves events.db rows so re-discovery cleanly re-adds).
- Ad-hoc MPIM ingest without yaml: `ingest/slack_mpim_oneshot.py --confirm-mpim` (one-shot; no cursor unless `--persist-cursor`).

**Discovery + hygiene scripts:**

```bash
# Discover new team-active channels (dry-run, prints decision per candidate)
.venv/bin/python derive/slack_discover_channels.py --auto-mode --top 200

# Apply auto_full + auto_team_involved verdicts to yaml
.venv/bin/python derive/slack_discover_channels.py --auto-mode --top 200 --apply

# Emit cron-status-consumable JSON (no apply)
.venv/bin/python derive/slack_discover_channels.py --auto-mode --json-out state/last_slack_discover.json

# Prune stale MPIMs (default 30d quiet threshold)
.venv/bin/python derive/slack_prune_stale_mpims.py            # dry
.venv/bin/python derive/slack_prune_stale_mpims.py --apply
```

Discovery facts:

- Team-set source of truth = `management/context/team.md` (7 direct reports), NOT people.yaml (broader cross-team map).
- Activity floor: 5 team msgs/90d for non-MPIM, 1 for MPIM. MPIM auto_full requires ≥3 team handles in the channel name.
- A message counts toward team-activity if author ∈ team OR body @-mentions a team member OR body pings a team subteam handle (`config/team_subteams.yaml`) — subteam coverage surfaces oncall/incident channels.

**Alert-channel branch:** bot-authored alert firehoses for team-owned systems (e.g. `service-a-alerts`, `example-tracker`, `example-txn-alerts`) auto-add as `full` even when team authorship is ~0, bypassing the floor. Gate = alert-named or ≥80% bot-authored AND name carries a team-domain keyword (`accounting`, `recon`, `service-c`/`EX`, `transaction(s)`, `txn`, `service-a`, `account-freeze`, `ledger-balance`, `pending_txn`; `deposits`/`td` excluded as liabilities-domain). Token-aware match — won't mis-fire on `gl` inside `breakglass`.

---

## Event model

**Canonical event shape + SQLite DDL live in [`SCHEMA.md`](SCHEMA.md)** — go there for the full field reference. Below are only the run-guide bits: the per-source `event_type` enum and how `refs` is populated.

### Event types per source

| Source | event_type | What it represents |
|--------|------------|--------------------|
| github | `pr_opened` | PR opened |
| github | `pr_merged` | PR merged |
| github | `pr_closed` | PR closed without merge |
| github | `pr_merged_by` | Who merged the PR (separate event — list API returns `merged_by: null`, requires individual fetch) |
| github | `review` | PR review submitted (includes MatterAI bot reviews) |
| github | `comment` | PR inline or issue comment |
| github | `commit_in_pr` | Commit that landed via a PR |
| github | `commit_pushed` | Direct push commit |
| jira | `issue_created` | New issue created |
| jira | `status_change` | Issue transitioned |
| jira | `assignment` | Issue assigned/unassigned |
| jira | `comment` | Comment added |
| confluence | `page_created` | Page version 1 by a team member |
| confluence | `page_updated` | Subsequent page version by a team member |
| confluence | `comment` | Inline or footer comment |
| slack | `thread_started` | Top-level channel message (parent of a thread, or standalone) |
| slack | `thread_reply` | Reply nested under a thread parent |

### `refs` enrichment

Populated at ingest time by `ingest/common.py:enrich_refs()`:

- **people** — actor resolved against `config/people.yaml` by source-specific field → canonical GitHub handle
- **tickets** — regex `\b([A-Z]{2,10}-\d+)\b` over title + body
- **pages** — regex `/pages/(\d{8,12})\b` over title + body URLs
- **projects** — keywords matched against title+body; epic keys matched against Jira epic link; page IDs matched against extracted pages

---

## Ingest pipeline

### Cursor management

github/jira/confluence track a high-water mark in `state/cursors.json`:

```json
{
  "github:example-org/service-a": "2026-05-11T18:04:35Z",
  "jira": "2026-05-11T17:00:00Z",
  "confluence": "2026-05-11T16:00:00Z"
}
```

Slack uses **per-channel cursors** in `state/slack_cursors.json` (Slack-epoch float strings):

```json
{
  "C0EXAMPLE": "1779250675.166149",
  "C0EXAMPLE": "1779091416.463919"
}
```

- On each run: fetch only items newer than cursor.
- On clean exit: advance cursor to newest seen ts (Slack: per-channel, never-go-backwards check).
- `--reset-cursor` (gh/jira/confluence) or `--cursor-mode fresh` (Slack) ignores cursor — idempotent (duplicates skipped via `INSERT OR IGNORE` / `_event_id` PK).

### Idle guard

github/jira/confluence: each ingest script writes `state/last_<source>_success.date` (local date, `YYYY-MM-DD`) on clean exit. Wrappers (`run-*.sh`) check it: if today's date is present, exit 0 immediately.

- LaunchAgent fires every 30 min — retries until first success, then idles for the day.
- Direct invocations also write the gate file on clean exit, so `cron-status.sh` always reflects the true last success.
- **`--reset-cursor` does NOT write the gate file.** Backfill is not a "today's incremental succeeded" signal — the LaunchAgent will still run the next incremental pass.
- Force a re-run via wrapper: `echo "2000-01-01" > state/last_github_success.date`

**Slack diverges:** `run-slack.sh` has no daily gate — fires hourly at :00 (12h–22h IST) unconditionally. `slack_ingest_app.py` still writes `state/last_slack_success.date` on any-channel success (consumed by `cron-status.sh` for the freshness pill). Cursor-bound + idempotent upsert means re-running is cheap when channels are quiet.

### MatterAI signal

Every PR gets a bot (`matterai[bot]`) review (`🧪 PR Review is completed: <summary text>`). `rollup.py` extracts it and bakes it into `derived/people/` + `derived/projects/`. Instant triage on domain + risk keywords (`critical`, `panic`, `race condition`, `security`) without reading diffs.

### SQLite setup

DB at `index/events.db`. Schema auto-bootstrapped on first `get_db()` call:

- `events` — primary store
- `event_refs` — normalised refs (one row per event×ref_type×ref_value)
- `events_fts` — FTS5 virtual table over `title` + `body`
- `subject_summary` — LLM classifier cache (keyed by `subject + content_hash`); carries domain classification AND team ownership (`owned_by_primary`, `co_owners_json`, `owned_by_confidence`, `ownership_reasoning`)
- `person_narrative` — per-person narrative cache
- `topic_brief` — per-cluster brief; `owner_distribution_json` holds per-team ownership share (rolled up from `subject_summary` by `cluster_ownership_rollup.py`)

WAL mode + 30s busy timeout on every connection.

---

## Testing & pipeline validation

Two complementary layers guard the pipeline.

**1. Unit / integration tests** (`tests/`, pytest) — fast, hermetic, never touch
the real `events.db` (fixtures redirect every persistent path at a tmp tree):

```bash
bin/run-tests.sh                 # full suite (installs pytest on first use)
bin/run-tests.sh -v              # verbose
bin/run-tests.sh tests/test_common_enrich_refs.py   # one file
```

Coverage focuses on the highest-leverage, most-regressable code:

| file | what it pins |
|------|--------------|
| `test_common_enrich_refs.py` | every ref-extraction regex + project/person resolution + the 16-digit Slack-ts rule |
| `test_common_insert_event.py` | dedup-on-id, refs fan-out, FTS sync, dry-run |
| `test_common_atomic_and_raw.py` | atomic-write durability + temp cleanup; `append_raw` line numbering |
| `test_common_cursors.py` | cursor round-trip + success-date markers |
| `test_run_health.py` | ingest-overrun 80%/100%-of-interval thresholds |
| `test_jira_metrics.py` | dedup credit, attribution chain, dev-vs-reviewer, ops detection |
| `test_pipeline_validate.py` | the integrity validator's own FAIL/WARN branches |
| `test_jira_normalize.py` | Jira JSON→Event: ADF flatten, sprint pick, +0530 ts, epic prefix, changelog fan-out |
| `test_github_normalize.py` | PR opened/closed/merged collapse, review/comment, commit actor-resolution chain |
| `test_confluence_normalize.py` | page created/updated, version-author precedence, body cap, comment |
| `test_slack_parse.py` | bot block/attachment recovery, mention/subteam expand, files, reactions, thread-reply detection |
| `test_slack_upsert.py` | id-vs-subject thread split, ts/url builders, UPSERT insert/update/unchanged |

Opt-in pre-push gate: `export RUN_TESTS=1` makes `.githooks/pre-push` block a
push when the suite is red (off by default — a routine push is never gated).

**2. Data validators** (`derive/*_validate.py`) — runtime PASS/WARN/FAIL checks
on the live DB, refreshed after each ingest and rendered by `cron-status`:

- `jira_validate.py` / `github_validate.py` / `confluence_validate.py` /
  `slack_validate.py` — per-source **attribution + content** quality.
- `pipeline_validate.py` — **cross-cutting structural integrity** (schema
  NOT-NULLs, ISO-8601 ts, no future ts, source/event_type vocabulary, orphan
  `event_refs`, FTS↔events row-count sync, `raw_path` collisions from the
  `append_raw` race, and per-source **freshness** — the silent-stale guard).
  Report-only; refreshed by `ingest/refresh-pipeline-validate.sh` on every
  fire; shown as the **INTEGRITY** block in `cron-status`.

```bash
.venv/bin/python derive/pipeline_validate.py          # human-readable
.venv/bin/python derive/pipeline_validate.py --json    # cron-status cache
```

---

## Rollup pipeline

`derive/rollup.py` reads `index/events.db` and regenerates all `derived/` markdown.

- **Default window:** 30 days (bare `python rollup.py`).
- **`run-rollup.sh` (cron wrapper):** always `--days 240 --week`.

### Rollup flags

| Flag | Default | Effect |
|------|---------|--------|
| `--days N` | 30 | Lookback window in days |
| `--week` | off | Also generate `derived/weekly/YYYY-Wnn.md` |
| `--detail-summary` | off | Richer 3–5 sentence per-PR narrative in person profiles (~3× token cost) |
| `--skip-narrative` | off | Skip per-person narrative generation |

### Outputs

| File | Content |
|------|---------|
| `derived/people/{handle}.md` | Activity counts, domains as author/reviewer/owner, per-domain item list with MatterAI summaries, recent PRs, top reviewers |
| `derived/projects/{slug}.md` | PR/ticket/page counts, contributor leaderboard, recent items |
| `derived/weekly/{YYYY-Wnn}.md` | Team weekly stats: volume, cycle time, review coverage |
| `derived/alerts.md` | Stale PRs, drive-by merges, PRs with no review |
| `derived/team-leaves.md` | Team leaves (direct reports): Active today · Upcoming 30d · Recent 14d · Ambiguous. Generated daily by `derive/render_leaves.py`. |

### Auth resolution (LLM paths)

`run-rollup.sh` resolves auth in this order:

1. Claude Code OAuth — Keychain (`Claude Code-credentials`) or `~/.claude/.credentials.json`
2. `~/.secrets/anthropic_api_key` — paid API key (fallback if OAuth expired)
3. Skip — keyword fallback classification. Output still produced.

### Session-mode rollup (manual-rollup.sh)

Use inside a Claude Code session (no `anthropic_api_key`, or to avoid 429s from OAuth quota):

```bash
DAYS=90 ./derive/manual-rollup.sh           # phase 1: dump pending subjects
# → paste state/pending_classification.json into chat
# → Claude classifies; save output to state/verdicts.json
./derive/manual-rollup.sh apply             # phase 2: apply + rerun rollup
```

Verdicts persist to `subject_summary` cache; subsequent `rollup.py` runs hit cache — identical output, zero LLM cost.

**Legacy narrate-dump / narrate-apply** (`NARRATIVE=1 ./derive/manual-rollup.sh narrate-dump` + `narrate-apply`) **off by default** since 2026-05-22. Superseded by `/ask person_range` (see below). Old `derive/narrative.py` + `person_narrative` cache table remain for back-compat with `derived/people/*.md`; removed once consumers migrate.

### Domain classification

**Priority order (post-2026-05-12 chat-only policy):**

1. **Jira epic anchor** — if issue's `epic_key` matches `jira_epics` in projects.yaml → tagged deterministically, no LLM, no chat. Re-applied via `llm_classifier._apply_epic_anchor` on every verdict (defends against chat output that drops the epic slug).
2. **Auto-slug for new Epics** — only `issue_type == "Epic"` Jira tickets create new projects.yaml slugs (kebab-case from title, bigram-only keywords). Filter at `derive/dump_pending.py::_detect_new_epic_slugs`. CMRs/Tasks/Bugs link to existing slugs.
3. **Chat classifier** — pending subjects classified via `/rollup` slash command in active Claude Code session. Thin GitHub PRs: inline `gh pr diff <num> --repo <owner/repo>` fetch (the `needs_diff: true` flag is dead in chat path; `apply_verdicts.py` rejects any verdict still setting it).
4. **Keyword fallback** — cron + manual-rollup scripts always strip Anthropic auth before invoking `rollup.py`, so `llm_classifier.classify_subjects` short-circuits to `_fallback_classify`. Clean keyword hits emit verdicts directly to `subject_summary`. Misses stay pending. Cache: `subject_summary` keyed by `(subject, content_hash)`.

**Removed 2026-05-12:** `derive/algo_classify.py` (algorithmic bulk classifier with embedded `CMR_BODY_HINTS` dict) and `/bulk-rollup` slash command. Every semantic decision now flows through chat. See [`ARCHITECTURE.md`](ARCHITECTURE.md) §3.2 for full flow.

### Slash commands (in `.claude/commands/`)

**Analysis + rollup**

| Command | Purpose |
|---------|---------|
| `/ask <route> <args>` | Sole router for narrative/analytical questions. Routes: `summarize`, `person_range`, `team_range`, `attention`, `ticket_gaps`, `rootcauses`, `dev_style` (→ `/dev-style`), `highs_lows` (→ `/retro`), `feature_logic`. See "Per-person signals + retros" section. |
| `/retro since=<iso> until=<iso>` | Stakeholder-facing retrospective. Team-level voice, deliveries-only Highs, measured impact from slack. |
| `/rollup [days]` | Full chat-classify cycle: dump → classify in chat → apply. Default 240 days. |
| `/classify` | Phase 2 only — re-classify pending without re-dumping (use when `verdicts.json` got wiped). |
| `/leaves [days]` | Team-leaves chat-classify cycle (direct reports only): refresh pending → chat-classify → apply → render `derived/team-leaves.md`. Default 60-day lookback. Phase 1 (regex prefilter) + Phase 4 (render) run daily 04:00 IST via cron; Phase 2/3 (chat + apply) owner-invoked here. |
| `/dev-style <person>` | Per-developer working-style profile from the `actor_behavior` view. Read-only, owner-invoked. |
| `/slug-epics` | Create human-readable kebab-case slugs for unmapped Jira epics referenced by recent subjects. |
| `/cron-status` | Run `bin/cron-status.sh` and show the ingest/scheduler health summary. |

**Embedding + clustering**

| Command | Purpose |
|---------|---------|
| `/refresh-embeddings` | Incrementally refresh embedding + clustering pipeline after new ingestion. Detects new/changed subjects, re-embeds, re-clusters, re-links to projects. |
| `/embed-validate` | Validate the `embedding` table — sanity battery (drift, dim, coverage) + cluster-quality report. |

**Slack ops** (Web-API path is current; `*-mcp` variants are legacy)

| Command | Purpose |
|---------|---------|
| `/slack-ingest` | Steady-state Slack ingest via direct Slack Web API (current path). Includes the trailing-window edit/delete reconcile inline (no separate command). |
| `/slack-backfill` | One-time channel backfill via direct Web API. Owner-invoked. |
| `/slack-compact` | Chat-driven Slack thread compaction (reads `state/slack_compact_pending.json`). |
| `/slack-ingest-mcp` | **Legacy** — steady-state ingest via Slack MCP. Superseded by `/slack-ingest`. |
| `/slack-backfill-mcp` | **Legacy** — chunked backfill via Slack MCP. Superseded by `/slack-backfill`. |

Channel discovery is no longer a slash command — it runs on cron (`bin/run-slack-discover.sh` → `derive/slack_discover_channels.py`, Wed+Fri 13:00 IST) and can be run manually via the discovery scripts in the "Slack token + channels" section above.

Removed 2026-05-22: `/narrative` (folded into `/ask person_range`).
Removed: `/slack-discover` (now cron-only) and `/slack-reconcile` (folded into `/slack-ingest`).

---

## Per-person signals + retros (new pipeline)

Deterministic per-person + stakeholder-retro pipeline that replaced `derive/narrative.py` on 2026-05-22.

### Architecture

```
config/tier_expectations.yaml   ← reliability gates + work_hours + window
        │
        ▼
derive/person_profile.py        ← schema v3 JSON: contribution / behavioral /
        │                         throughput / quality / narrative / fate /
        │                         lookahead. Reads events.db + topic_brief +
        │                         jira_metrics. No LLM.
        ▼
derive/person_deepread.py       ← one-shot bundle (profile + 10 clusters +
        │                         tickets + PRs + Confluence + jira comments +
        │                         slack threads). Cached at
        │                         state/cache/person_deepread/.
        ▼
derive/ask_engine.py            ← lifetime-overlap filter + window_state field
        │                         (fully_in / started_in / ended_in / spans /
        │                         pre_window / post_window). Use window_state,
        │                         not topic_brief.status, for historical retros.
        ▼
.claude/commands/ask.md         ← /ask router. Renders TL;DR-first prose,
.claude/commands/retro.md       ← /retro stakeholder format (no dev names,
                                  deliveries only, measured impact).
        ▼
management/narratives/          ← per-person + team + EM outputs
management/retros/              ← monthly stakeholder retros
```

### Key files

| Path | Role |
|------|------|
| `config/tier_expectations.yaml` | Reliability gates (`sp_coverage_min=0.70`, `cmr_share_threshold=0.30`, `min_sprinted_tickets_for_verdict=5`), tier bands (SDE1/2/3), `work_hours` (12-20 IST), `window` (`lookahead_days=30, fate_max_days=90`) |
| `derive/person_profile.py` | Deterministic per-person signals. CLI: `python derive/person_profile.py --name <handle> --since <iso> --until <iso>` |
| `derive/person_deepread.py` | One-shot bundle with disk cache (`state/cache/person_deepread/<sha1>.json`). Mtime-gated on `events.db`. `--no-cache` busts. |
| `derive/ask_engine.py` | `clusters_active_in_window` / `root_causes_in_window` with `window_state` field |
| `.claude/commands/ask.md` | `/ask` skill spec — TL;DR-first, bulleted signals, plain-English (no cluster IDs / metric dumps) |
| `.claude/commands/retro.md` | `/retro` skill spec — stakeholder format locked |

### Pace signal: PR cycle time, NOT ticket lead time

Team creates+Dones tickets the same day (recorded post-hoc), so per-ticket `lead_time_days` collapses to ~1 and is bogus as a pace signal. **Use PR cycle time** (`pr_cycle_median_days`, `slow_pr_count_over_14d`, `same_day_pr_count`) from `person_profile.fate.pr_fate_summary` — sourced from real PR opened→merged timestamps.

### Cluster status vs window_state

`topic_brief.status` reflects NOW, not the asked window. For windows ≥30 days old (historical retros), render against `window_state` (derived per query from lifetime timestamps), NOT `status`. `ask_engine.py` returns both; `ask.md` + `retro.md` enforce the rule.

### Stakeholder retro rules (locked)

`/retro` and `/ask highs_lows` produce a STAKEHOLDER document:

1. **Team-level voice only.** "The team delivered X" / "instant-pay rollout reached 70% coverage". NEVER dev names.
2. **Highs = deliveries only.** Code merged / ticket-Done WITHOUT user-facing rollout is NOT a Hi — goes to Lows as "X dev complete, rollout slipping to <date>".
3. **Every High needs measurable impact.** Numbers pulled from slack rollout-update threads (RPS, latency, success rate, accounts onboarded, etc.). Don't invent.
4. **No tech-internal jargon.** No PR counts, no jira closure counts, no cluster references, no `window_state` / `lookahead` / `sp_completion`.
5. **No IC-level metrics.** Velocity tables, sp_completion per IC, after_hours_share — all internal-engineering signals. Dropped entirely.

Precedent: owner's `#example-monthly-update` Feb + March 2026 posts (slack:`C0EXAMPLE:1772082383.704359` + `1774681059.726029`). Memory at `~/.claude/projects/$HOME/memory/feedback_retro_stakeholder_format.md`.

### Output paths

| Path | Generator | Cadence |
|------|-----------|---------|
| `management/narratives/per-person/org-<handle>-<since>-to-<until>.md` | `/ask person_range` | On-demand per IC |
| `management/narratives/team/<since>-to-<until>.md` | `/ask` team route | On-demand |
| `management/narratives/em/owner-<since>-to-<until>.md` | `/ask` EM route | On-demand |
| `management/retros/<since>-to-<until>.md` | `/retro` | Monthly stakeholder cadence |

### `subject_summary` cache invariants

- Primary key: `(subject, content_hash)`.
- `content_hash` = SHA over `(title, body, matterai_summary, issue_type, diff?)`. Body edits → new hash → re-classify next run.
- Steady-state nightly run: 0 fresh classifications, 100% cache hit.
- `apply_verdicts._validate` rejects: `confidence < 0.7`, `needs_diff: true`, slugs not in `projects.yaml`, risk-flags outside enum. Rejected subjects stay pending and re-emerge in next dump.

---

## Directory structure

```
work-context/
├── bin/
│   ├── install-agents.sh              # install / reload all LaunchAgents
│   ├── install-routines.sh            # bootstrap Claude Code /schedule routines
│   ├── _routines.py                   # routine status (cadence/last/next) for cron-status + dashboard
│   ├── cron-status.sh                 # show ingest scheduler health
│   ├── backfill-confluence-titles.py  # one-time: fetch missing page titles
│   ├── backfill-jira-epics.py         # one-time: fetch epic links for issues
│   ├── discover-jira-epics.py         # discover epic hierarchy from Jira
│   └── migrate-commit-actors.py       # one-time: fix actor on commit events
├── config/
│   ├── people.yaml                    # cross-source identity map
│   ├── projects.yaml                  # domain → keywords/epics/pages
│   ├── slack_channels.yaml            # slack channel allow-list (incl. MPIM allow_mpim flag)
│   ├── team_subteams.yaml             # slack user-group ids the team is paged via (example-team, service-c-oncall…)
│   └── tier_expectations.yaml         # reliability gates + work_hours + lookahead window
├── derive/
│   ├── rollup.py                      # regenerate derived/ from events.db
│   ├── run-rollup.sh                  # wrapper: idle guard + auth + --days 240 --week
│   ├── manual-rollup.sh               # session-mode rollup: dump/apply (narrate legacy/off)
│   ├── llm_classifier.py              # LLM-based subject/domain classifier
│   ├── person_profile.py              # NEW: deterministic per-person signals (schema v3)
│   ├── person_deepread.py             # NEW: one-shot bundle + disk cache
│   ├── ask_engine.py                  # NEW: cluster lifetime-overlap + window_state
│   ├── narrative.py                   # LEGACY per-person narrative (off by default)
│   ├── diff_fetcher.py                # fetch commit diffs (pass-2 classifier)
│   ├── dump_pending.py                # extract uncached subjects for chat classification
│   ├── apply_verdicts.py              # insert verdicts into subject_summary cache
│   ├── dump_pending_narrative.py      # LEGACY narrate-dump
│   ├── apply_narratives.py            # LEGACY narrate-apply
│   ├── slack_team.py                  # team.md → slack_id resolver + team_subteams.yaml loader + is_team_involved helper
│   ├── slack_discover_channels.py     # scan members, decide ingest_mode, populate yaml
│   ├── slack_prune_stale_mpims.py     # drop MPIM yaml rows quiet >30d (events.db preserved)
│   └── slack_team_filter_cleanup.py   # one-shot retro purge for team_involved-mode flips
├── derived/                           # generated — do not edit by hand
│   ├── people/
│   ├── projects/
│   ├── weekly/
│   └── alerts.md
├── ingest/
│   ├── github.py                      # PRs, reviews, comments, commits, pr_merged_by
│   ├── jira.py                        # issues, transitions, assignments, comments
│   ├── confluence.py                  # pages, inline/footer comments (team-filtered via jira_id)
│   ├── slack_api_client.py            # Slack Web API wrapper (tier-3 rate limit, users cache w/ TTL)
│   ├── slack_backfill_app.py          # one-shot channel backfill
│   ├── slack_ingest_app.py            # steady-state cursor-bound ingest (Phase 2.7/2.7b reconcile)
│   ├── slack_mpim_oneshot.py          # explicit-consent MPIM ingest (--confirm-mpim)
│   ├── common.py                      # DB, cursor, enrichment, write_success_date
│   ├── run-github.sh                  # wrapper: idle guard + GitHub PAT injection
│   ├── run-jira.sh                    # wrapper: idle guard + Atlassian token injection
│   ├── run-confluence.sh              # wrapper: idle guard + Atlassian token injection
│   └── run-slack.sh                   # wrapper: NO idle guard (cursor-bound, every-fire)
├── launchagents/                      # source of truth for scheduler plists
│   ├── com.example.github-ingest.plist
│   ├── com.example.jira-ingest.plist
│   ├── com.example.confluence-ingest.plist
│   ├── com.example.slack-ingest.plist
│   ├── com.example.slack-discover.plist  # Wed+Fri 13:00 IST · auto-apply discover
│   ├── com.example.leaves.plist          # daily 04:00 IST · Phase 1 leaves
│   ├── com.example.housekeeping.plist    # weekly Mon 03:00 IST · step 7 = MPIM pruner
│   (no rollup plist — rollup is manual via /rollup)
├── index/
│   └── events.db                      # SQLite — primary query target
├── raw/                               # append-only JSONL backups per source/date
│   └── <source>/YYYY/MM/DD.jsonl
├── state/
│   ├── cursors.json                   # last-seen timestamp per source (gh/jira/confluence)
│   ├── slack_cursors.json             # per-channel cursor (Slack-epoch float strings)
│   ├── slack_users_cache.json         # disk-persisted users.list (24h TTL, ~210KB)
│   ├── last_github_success.date       # idle-guard markers (YYYY-MM-DD, local time)
│   ├── last_jira_success.date
│   ├── last_confluence_success.date
│   ├── last_slack_success.date        # written on any-channel success (no daily idle gate)
│   ├── last_slack_validate.json       # validator JSON refreshed each cron fire
│   ├── last_slack_discover.json       # discover proposals (consumed by cron-status DISCOVERY block)
│   ├── slack_channels.yaml.bak.*      # pre-apply discover snapshots (last 4 retained)
│   ├── last_leaves_success.date       # leaves daily idle gate
│   ├── pending_leaves.json (+ .rules.md)  # leaves Phase 1 → Phase 2 handoff
│   ├── verdicts.leaves.*.json         # archived leaves verdicts post-apply
│   ├── last_rollup_success.date
│   ├── pending_classification.json    # subjects waiting for chat classification
│   ├── pending_narrative.json         # LEGACY narrate-dump output
│   ├── cache/person_deepread/         # NEW: bundle cache for /ask person_range (mtime-gated)
│   └── verdicts.*.json                # archived classifier outputs
├── logs/
│   ├── ingest.log                     # all ingest output (LaunchAgent stdout/stderr)
│   ├── github-reset.log               # github --reset-cursor runs (separate log)
│   └── rollup.log                     # rollup output
├── SCHEMA.md                          # event schema + DB DDL reference
└── requirements.txt
```

---

## Prerequisites

### Python

```bash
cd ~/context/work-context
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Secrets

All tokens at `~/.secrets/` (never committed):

| File | Used by | How to get |
|------|---------|------------|
| `~/.secrets/github_pat` | `run-github.sh` | GitHub → Settings → Developer settings → Personal access tokens → `repo` + `read:org` scopes |
| `~/.secrets/atlassian_token` | `run-jira.sh`, `run-confluence.sh` | https://id.atlassian.com/manage-profile/security/api-tokens |
| `~/.secrets/atlassian_email` | same | Atlassian login email (defaults to `owner@example.com`) |
| `~/.secrets/anthropic_api_key` | `run-rollup.sh` (optional) | Anthropic console — used if OAuth expired |

```bash
mkdir -p ~/.secrets
echo "ghp_..." > ~/.secrets/github_pat
echo "your-api-token" > ~/.secrets/atlassian_token
echo "you@yourorg.com" > ~/.secrets/atlassian_email
chmod 600 ~/.secrets/*
```

---

## Scheduler

Ingest runs on macOS LaunchAgents — survive sleep/wake, replay missed fires on wake.

| Source | Schedule (IST) | Idle gate |
|--------|---------------|-----------|
| github | :00 and :30, 12h–22h | `state/last_github_success.date` |
| jira | :00 and :30, 12h–22h | `state/last_jira_success.date` |
| confluence | :05 and :35, 12h–22h | `state/last_confluence_success.date` |
| slack | hourly :00, 12h–22h | none — fires every slot (cursor-bound + idempotent); hourly so a full ~30–40min sweep finishes before the next fire |
| housekeeping | weekly Mon 03:00 | n/a — step 7 runs MPIM pruner (`slack_prune_stale_mpims.py --apply`, 30d quiet threshold) |
| slack-discover | Wed + Fri 13:00 IST (`bin/run-slack-discover.sh`) | — — wrapper runs `--auto-mode --top 500 --apply --json-out`; pre-apply yaml snapshot at `state/slack_channels.yaml.bak.<ts>` (LRU-4) |
| leaves | daily 04:00 IST (`derive/run-leaves.sh`) | `state/last_leaves_success.date` — Phase 1 (regex dump + render) only; chat-classify via owner-invoked `/leaves` |
| codegraph | daily 18:00 IST (`bin/run-codegraph.sh`) | `state/last_codegraph_success.date` — git ff-if-clean + full `code-review-graph` rebuild for service-a + service-c (~90s, no LLM); feeds `/ask` code-logic queries |
| rollup | **manual** (no LaunchAgent) | — invoke `/rollup [days]` in chat |

**Retry policy:** retries every fire until one success per day, then idles. Failure auto-retries at next fire.

```bash
./bin/install-agents.sh    # install / reload
./bin/cron-status.sh       # check health
```

---

## Running manually

```bash
# dry run — no DB writes, no cursor update, no gate file
.venv/bin/python ingest/github.py --dry-run

# normal incremental run (bypasses idle guard, uses cursor)
.venv/bin/python ingest/github.py

# via wrapper (respects idle guard)
./ingest/run-github.sh

# full backfill (does NOT write idle gate file)
.venv/bin/python ingest/github.py --reset-cursor

# override repos / project per-run
.venv/bin/python ingest/github.py --repo your-org/other-repo
.venv/bin/python ingest/jira.py --project PLAT

# force idle guard to allow wrapper re-run today
echo "2000-01-01" > state/last_github_success.date
```

---

## Rollup commands

```bash
# via cron wrapper (idle guard, OAuth auth, --days 240 --week)
./derive/run-rollup.sh

# direct run
.venv/bin/python derive/rollup.py --days 90
.venv/bin/python derive/rollup.py --days 240 --week
.venv/bin/python derive/rollup.py --days 90 --detail-summary   # richer PR narratives
.venv/bin/python derive/rollup.py --days 90 --skip-narrative   # skip per-person narrative

# session-mode (no API key needed — uses active Claude Code session)
DAYS=90 ./derive/manual-rollup.sh                 # dump → classify in chat → apply
DAYS=90 ./derive/manual-rollup.sh narrate-dump    # generate per-person narrative prompts
```

---

## Config

### `config/people.yaml`

Single cross-source identity map. The `scope` field replaces the deleted `known_externals.yaml` — one file, one source of truth.

```yaml
people:
  - name: Alice Example
    canonical: alice-example
    scope: team                      # team | org | external
    github: org-alice-example
    github_aliases:                  # raw git author-name variants
      - Alice Example
    email: alice.example@yourorg.com
    jira_id: "712020:abc123..."      # Atlassian accountId (jira + confluence)
    slack_id: U0EXAMPLE
    slack_handle: alice.example
    git_names: ["Alice Example"]     # unlinked-github commit author names
```

**`scope` semantics** (used by `derive/{github,jira,confluence}_validate.py`):

| scope | meaning | counted in analysis? |
|-------|---------|----------------------|
| `team` | your direct team | yes |
| `org` | broader org (cross-team, EMs, PMs) | no — silenced |
| `external` | bots, automation, vendors | no — silenced |

An actor in **no** people.yaml entry → flagged `unmapped` (WARN/FAIL) by the validators. Legacy entries with no `scope` default to `team`.

**Self-healing** — missing fields fill automatically. Each ingest emits observed identity pairs (`derive/identity_signals.py`) to the `identity_signals` table; `derive/identity_reconcile.py` runs after every fire (wired in `ingest/run-*.sh`) and back-fills `jira_id`, `slack_id`, `github_aliases`, etc. onto matching entries. Generic, eventual, no batch. See `cron-status identity`.

### `config/projects.yaml`

```yaml
projects:
  - slug: cash-withholding
    name: Cash Withholding
    keywords: [withholding, TdsEntry, TDS_DEDUCTION]  # LLM guidance + keyword fallback
    jira_epics: [EX-2238]                     # deterministic — matched first
    confluence_pages: [EXAMPLE_PAGE_ID]        # page IDs from URLs
```

### Ownership + team configs

Team-attribution layer. Ownership is **content-first** — resolved from a subject's classified `domains`, not who posted it.

| File | Purpose | Consumed by |
|------|---------|-------------|
| `config/teams.yaml` | Team enumeration (`id` + description). LLM reads descriptions to attribute each subject to an owning team — descriptions are the primary classifier signal. | `derive/dump_pending.py`, `derive/apply_verdicts.py` |
| `config/domain_team_map.yaml` | Domain-slug → owning-team map. `default_team` + `overrides` for sister-owned slugs; `review:` lists ambiguous slugs the owner must confirm. | `derive/ownership_resolve.py` |
| `config/team_subteams.yaml` | Slack user-group (subteam) IDs that represent this team — so `ingest_mode=team_involved` keeps threads that page the team via `<!subteam^S…>` instead of individual `@UID` mentions. | `derive/slack_team.load_team_subteam_ids()`, `ingest/slack_*_app.py` |
| `config/tier_expectations.yaml` | Per-tier throughput/quality ranges + sprint cadence + work-window (for `after_hours_share`). Surfaces deviation, not a verdict. | `/ask person_range` |

Cluster-level ownership is **derived** by aggregating per-subject ownership — see `derive/cluster_ownership_rollup.py`. Unmapped domain slugs surface as yaml gaps in the census reconciliation each run.

```yaml
# config/domain_team_map.yaml
default_team: home-team                # any slug not overridden = home team
overrides:
  payments:      { primary: payments-domain-team, co: [home-team] }
  subscriptions: { primary: payments-domain-team }
review: [ ... ]                        # ambiguous slugs — owner confirms
```

---

## Monitoring

```bash
./bin/cron-status.sh
```

Per-lane dashboard: github / jira / confluence / slack (last run, cursor age,
next fire, validate findings, 24h counts) + ROLLUP, PIPELINE, HOUSEKEEPING,
IDENTITY, EMBEDDING, CODE-GRAPH + DB snapshot + recent runs + HEALTH footer.

**CODE-GRAPH lane:** status of the daily 18:00 `com.example.codegraph` rebuild (`bin/run-codegraph.sh`) — schedule/next-fire, last-run ok/fail, per-repo ✓/✗ with node/edge totals. Reads `state/last_codegraph_success.date` + `state/codegraph_<date>.log`; parser shared via `bin/_codegraph_status.py`.

**Severity:** a lane shows red `✗ INGEST DOWN` (not yellow WARN) when its last run logged `Cursor NOT updated` — distinguishes a total auth/network outage from a transient flake. Ingest scripts exit `2` on 100%-source failure, `1` partial, `0` clean.

**Overrun detection:** each lane shows a `runtime` flag when a run takes (or is taking) longer than the gap to its next scheduled fire — `⚠ near-limit` at ≥80% of the interval, `✗ OVERRUN` at ≥100% (run collides with the next fire and gets SIGTERMed by launchd). Interval read from the plist; in-flight flags gated on `pgrep` so a mis-attributed open start can't false-alarm. Surfaced per-lane + in the HEALTH footer (cron-status) and as a `runtime` badge on each card (web dashboard). Logic in `bin/_run_health.py`, shared by both.

### Drill-downs

```bash
cron-status slack        # per-channel: cursor age, events, last activity
cron-status identity     # reconcile fills + signals by source + pair types
cron-status housekeeping # per-run action log w/ file lists
cron-status embedding    # per-source coverage + gap counts
cron-status pipeline     # topic_brief cluster status + v2 field gaps
cron-status discover     # full proposed slack-channel list (ranked)
cron-status html [path]  # self-contained HTML report (default /tmp/cron-status.html)
cron-status help
```

### Web dashboard

```bash
bin/dashboard.py [--port 8765]
open http://127.0.0.1:8765
```

Stdlib-only HTTP server (no Flask/FastAPI). Auto-refresh 30min. Per-lane cards, D3 circle-pack of top clusters (area ∝ member_count, color by status, click for detail), identity-signals 7d time-series (Chart.js), log-tail picker, expandable slack per-channel + discovered-channel tables. Routes: `/api/snapshot`, `/api/slack-channels`, `/api/discover`, `/api/clusters`, `/api/identity-timeseries`, `/api/log-tail`.

---

## Debugging

### DB locked

```bash
lsof index/events.db
kill -9 <pid> [<pid2> ...]
sqlite3 index/events.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

Cause: zombie Python processes from prior background runs.

### Auth failures

- GitHub: confirm `~/.secrets/github_pat` has `repo` + `read:org` scopes
- Atlassian: regenerate at https://id.atlassian.com/manage-profile/security/api-tokens

### Rollup 429s

OAuth quota is shared between interactive Claude sessions and rollup. If rollup fails with 429:

```bash
DAYS=90 ./derive/manual-rollup.sh    # skips LLM entirely
```

### Verify event counts

```bash
sqlite3 index/events.db "SELECT source, event_type, count(*) FROM events GROUP BY source, event_type ORDER BY source, event_type;"
```

---

## Resetting a source

```bash
.venv/bin/python ingest/github.py --reset-cursor --dry-run   # preview
.venv/bin/python ingest/github.py --reset-cursor              # apply
echo "2000-01-01" > state/last_github_success.date            # reset idle guard only
```

---

## Schema + Architecture

- **[`SCHEMA.md`](SCHEMA.md)** — full event shape + SQLite DDL.
- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — full code graph: communities, execution flows, per-module function reference, data layer, cross-community coupling. Regenerate via `mcp__code-review-graph__build_or_update_graph_tool(full_rebuild=true)` after structural changes.

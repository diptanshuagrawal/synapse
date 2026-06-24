# context

Personal engineering-management copilot. Pulls real activity from GitHub, Jira, Confluence, and Slack into a local warehouse, derives markdown rollups, and feeds them to a Claude Code agent that answers EM questions grounded in what the team actually shipped.

---

## Structure

```
context/
├── work-context/          data pipeline — ingest (GitHub/Jira/Confluence/Slack), SQLite, derived rollups
│   ├── README.md          how to run
│   └── ARCHITECTURE.md    code graph: module reference, execution flows, community map
└── management/            agent working directory — CLAUDE.md, sessions, drafts
    ├── context/
    │   └── activity/  →   symlink to work-context/derived/
    ├── sessions/
    ├── drafts/
    ├── audit/
    └── build-notes/       design decisions, build log, original handoff doc
```

**Full code-graph tour** → [`work-context/ARCHITECTURE.md`](work-context/ARCHITECTURE.md): community map, execution flows, per-module function reference, cross-community coupling. Regenerate via `mcp__code-review-graph__build_or_update_graph_tool(full_rebuild=true)` after structural changes.

---

## How the pieces connect

```
GitHub / Jira / Confluence / Slack
        │
        ▼
  work-context/ingest/        normalise → unified Event schema
        │
        ├─► raw/<source>/YYYY/MM/DD.jsonl    append-only backup
        └─► index/events.db                  SQLite index + FTS
                │
                ├─► derive/rollup.py             classify → derived/ markdown
                └─► derive/embed_subjects.py     embed → cluster → topic_brief
                │
                ▼
        work-context/derived/               agent-readable output
                │  (symlink)
                ▼
        management/context/activity/        read by Claude Code agent
                │
                ▼
        Claude Code (management/)           answers EM questions, drafts docs
```

The unified `Event` schema, event-type tables, classification mechanics, and identity unification are documented in `work-context/ARCHITECTURE.md` + `work-context/README.md` → SCHEMA. Setup is below.

---

## Fresh machine setup

### Step 1 — Python env

```bash
cd ~/context/work-context
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Step 2 — Secrets

All tokens at `~/.secrets/` (mode 600, never committed):

| File | Used by | How to get |
|------|---------|------------|
| `github_pat` | GitHub ingest | GitHub → Settings → Developer settings → Personal access tokens → `repo` + `read:org` scopes |
| `atlassian_token` | Jira + Confluence ingest | https://id.atlassian.com/manage-profile/security/api-tokens |
| `atlassian_email` | same | Atlassian login email (optional — defaults to `owner@example.com`) |
| `anthropic_api_key` | rollup LLM classifier | optional — see Rollup section |
| `openai_api_key` | embedding pipeline (`embed_subjects.py`) | OpenAI key. **Embeddings only** (`text-embedding-3-*`) — never chat/LLM |

```bash
mkdir -p ~/.secrets
echo "ghp_..." > ~/.secrets/github_pat
echo "your-atlassian-token" > ~/.secrets/atlassian_token
echo "you@yourorg.com" > ~/.secrets/atlassian_email
echo "sk-..." > ~/.secrets/openai_api_key       # optional — only if running embeddings
chmod 600 ~/.secrets/*
```

**Slack token is separate.** Slack ingest reads `SLACK_USER_TOKEN` (a `xoxp-…` user token) from `~/context/.env` (gitignored), **not** `~/.secrets/`. Slack scripts fail-loud if `ANTHROPIC_API_KEY` is also in env (chat-only-classification policy). See Step 5 + `runbook/slack-token-rotate.md`.

### Step 3 — Populate people.yaml (before any ingest)

`config/people.yaml` is the cross-source identity map.

**Must be populated before Confluence ingest** — Confluence filters pages to team members by `jira_id`. No jira_ids → no Confluence events.

```yaml
people:
  - name: Alice Example
    canonical: org-alice01     # GitHub handle — join key everywhere
    github: org-alice01
    email: alice.example@yourorg.com
    jira_id: "EXAMPLE_ACCOUNT_ID"      # Atlassian account ID — needed for Confluence filter
    git_name: "Alice Example"   # matches git commit author field
```

- Required fields: `canonical`, `github`, `jira_id`. Others improve attribution.
- **Find jira_id:** Jira → open any issue the person created/commented on → avatar → "View Profile" → accountId is in the URL (format `NNNNNN:xxxx-xxxx-...`). Or Atlassian MCP `lookupJiraAccountId`.

### Step 4 — Configure your org (one file, no code edits)

All org-specific values live in `work-context/config/sources.yaml` (gitignored). Copy the template and fill it in:

```bash
cp work-context/config/sources.example.yaml work-context/config/sources.yaml
$EDITOR work-context/config/sources.yaml
```

- Set `github.repos` + `github.org`, `jira.project_keys`, `atlassian.host`, `slack.workspace`, `teams.home`/`coowner`, `launchd.prefix`, etc.
- `derive/sources_config.py` loads it (falls back to `sources.example.yaml` placeholders; per-key env overrides: `GITHUB_ORG`, `JIRA_DOMAIN`, `JIRA_PROJECT_KEYS`, `SLACK_WORKSPACE`, `ATLASSIAN_EMAIL`).
- Per-run overrides still work: `ingest/github.py --repo your-org/other`, `ingest/jira.py --project PLAT`. Confluence shares `atlassian.host`.
- Full field reference: `work-context/README.md` → "Configuration".

### Step 5 — Configure Slack workspace + channels

Slack is the 4th ingest source (direct Web API; the older MCP path is legacy). Full detail: `work-context/README.md` → "Slack workspace + channels". Minimum:

1. Generate a Slack **User OAuth token** (`xoxp-…`) — scopes in `runbook/slack-token-rotate.md`.
2. Save to `~/context/.env` (gitignored): `SLACK_USER_TOKEN=xoxp-…`
3. Auto-populate the channel allow-list (don't hand-edit):

```bash
cd ~/context/work-context
# scan team-active channels you're a member of, decide ingest_mode, append to yaml
.venv/bin/python derive/slack_discover_channels.py --auto-mode --top 200 --apply
```

Per-channel `ingest_mode`:
- `full` — store every message (default)
- `team_involved` — keep only threads the team participates in (by author, `@UID` mention, or `<!subteam^S…>` ping from `config/team_subteams.yaml`)

1:1 DMs are always skipped; MPIMs need `allow_mpim: true`. New channels auto-bootstrap from `now − 365d` on first ingest — no manual backfill for typical adds.

### Step 6 — Configure projects.yaml (domain taxonomy)

`config/projects.yaml` defines the domains events get tagged to. Drives all per-project and per-person rollup output.

```yaml
projects:
  - slug: cash-withholding     # used as filename + identifier everywhere
    name: Cash Withholding     # human-readable label
    keywords:                  # case-insensitive substring match on title+body
      - withholding
      - WithholdingEntry
      - WITHHOLDING_DEDUCTION
    jira_epics:                # Jira epic keys — deterministic, matched first
      - EX-2238
      - EX-2389
    confluence_pages:          # Confluence page IDs — match against URLs in events
      - EXAMPLE_PAGE_ID
```

- Start with the epics your team owns; refine keywords after first rollup. The LLM classifier handles the rest.
- **Classification priority:** jira_epics match → LLM classifier (two-pass) → keyword fallback.

### Step 7 — First ingest

Run each source with `--reset-cursor` to pull full history. GitHub = all PRs/commits/reviews since repo start; Jira = all issues ever updated; Confluence = all pages by team members. Expect **5–30 min** by repo/project size.

```bash
cd ~/context/work-context

# GitHub — full history, all repos from config (sources.yaml github.repos)
.venv/bin/python ingest/github.py --reset-cursor

# Jira — full history, all projects from config (sources.yaml jira.project_keys)
.venv/bin/python ingest/jira.py --reset-cursor

# Confluence — full history, team members only (requires jira_id in people.yaml)
.venv/bin/python ingest/confluence.py --reset-cursor

# Slack — channels in slack_channels.yaml (must NOT have ANTHROPIC_API_KEY in env).
# No --reset-cursor flag: null-cursor channels auto-bootstrap from now−365d.
env -u ANTHROPIC_API_KEY .venv/bin/python ingest/slack_ingest_app.py
```

- Explicit historical backfill of one channel: `ingest/slack_backfill_app.py` (or `/slack-backfill`) — see `work-context/README.md`.
- Dry run (preview, no writes): add `--dry-run`, e.g. `.venv/bin/python ingest/github.py --reset-cursor --dry-run`.
- Verify counts: `sqlite3 index/events.db "SELECT source, event_type, count(*) FROM events GROUP BY source, event_type ORDER BY source, event_type;"`

**Note:** `--reset-cursor` does NOT write the idle gate file (`state/last_*_success.date`). Intentional — backfill doesn't count as "today's incremental succeeded", so the LaunchAgent can still run today's incremental.

### Step 8 — First rollup

Rollup reads `index/events.db` and regenerates all `derived/` markdown.

**Policy (2026-05-12): all semantic classification flows through chat.** Scripts strip Anthropic auth before invoking `rollup.py` — they only run keyword fallback against `config/projects.yaml`. Any subject without a clean keyword hit lands in pending and gets chat-classified.

**Default window: 30 days.** Use `--days 90` (or `/rollup 90`) for a richer initial view.

#### Primary workflow — `/rollup` (chat-driven)

In a Claude Code session at `~/context/work-context/`:

```
/rollup           # 240 days (default)
/rollup 90        # 90 days
/classify         # phase 2 only — when verdicts.json got wiped
```

`/rollup` runs three phases:

1. **Dump** — `manual-rollup.sh dump` → keyword pass → unclassified subjects → `state/pending_classification.json` + sibling `.rules.md`.
2. **Classify** — chat reads `.rules.md` first (objectivity lock), then classifies the JSON. Thin GitHub PRs fetched inline via `gh pr diff <num> --repo <owner/repo>`. Output → `state/verdicts.json`.
3. **Apply** — `manual-rollup.sh apply` validates (slug enum, conf threshold, risk-flag enum, epic anchor re-apply), inserts into `subject_summary` cache, archives verdicts, reruns rollup (full cache hit).

Phase 3b — narratives (LEGACY, off by default since 2026-05-22): `# NARRATIVE=1 ./derive/manual-rollup.sh narrate-dump`. Superseded by `/ask person_range` + `/retro` (Step 8b).

#### Background workflow — daily cron

**Rollup is currently MANUAL** — no LaunchAgent as of 2026-05-12. `derive/run-rollup.sh` exists as a wrapper but has no plist + no install-script entry. To re-enable daily: create `launchagents/com.example.rollup.plist` + add to `bin/install-agents.sh::SERVICES`. Until then EM invokes `/rollup` interactively (weekly in practice).

#### Removed (2026-05-12)

- `derive/algo_classify.py` — algorithmic bulk classifier with embedded `CMR_BODY_HINTS` dict
- `.claude/commands/bulk-rollup.md` — `/bulk-rollup` slash command

Replaced by the chat-only flow. Rationale: single source of truth, no embedded heuristics drifting from chat logic. New patterns go in `config/projects.yaml` keywords, not code.

#### Verify output

```bash
ls derived/people/
ls derived/projects/
cat derived/alerts.md
```

#### Rollup flags

| Flag | Default | Effect |
|------|---------|--------|
| `--days N` | 30 | Lookback window in days |
| `--week` | off | Also generate `derived/weekly/YYYY-Wnn.md` |
| `--detail-summary` | off | Richer 3–5 sentence per-PR narrative (3× token cost) |
| `--skip-narrative` | off | LEGACY — narrative.py path off by default since 2026-05-22 |

### Step 8b — Per-person signals + retros

Replaced `derive/narrative.py` on 2026-05-22. Full architecture: `work-context/README.md` → "Per-person signals + retros".

**Per-IC narrative (`/ask person_range`):**

```
/ask what <person> worked on between <since> and <until>
```

Routes to `derive/person_deepread.py` (one-shot bundle, disk-cached) → `derive/person_profile.py` (deterministic signals: contribution / behavioral / throughput / quality / fate / lookahead) → renders TL;DR-first prose. Saved to `management/narratives/per-person/<handle>-<since>-to-<until>.md`.

**Stakeholder retro (`/retro` and `/ask highs_lows`):**

```
/retro since=2026-04-01 until=2026-04-30
/ask highs and lows for April
```

Stakeholder-facing: team-level voice (NEVER dev names), Highs = deliveries only (code-Done ≠ delivery), measurable impact from slack threads, no PR/ticket/cluster jargon. Saved to `management/retros/<since>-to-<until>.md`.

- **Config source of truth:** `work-context/config/tier_expectations.yaml` (reliability gates, work_hours 12-20 IST, lookahead 30d, fate_max 90d).
- **Pace signal:** PR cycle time only. Ticket lead-time is bogus here (same-day create+Done flips).
- **Cluster status vs window:** for windows ≥30 days old, render against `window_state` (lifetime-overlap, derived per query in `derive/ask_engine.py`), NOT `topic_brief.status` (NOW-snapshot).

**Embedding + topic clusters** (feeds `cluster_pulse` / retro): subjects embedded (`derive/embed_subjects.py`, OpenAI `text-embedding-3-*` only) → clustered (`derive/cluster_subjects.py`) → LLM-enriched + named (`derive/enrich_clusters.py`, `derive/label_clusters.py`) → linked to `projects.yaml` slugs (`derive/link_clusters_to_projects.py`). Output → `embedding` / `topic_brief` / `topic_brief_member` / `cluster_project_map` tables (see `SCHEMA.md`). Refresh incrementally with `/refresh-embeddings`; sanity-check with `/embed-validate`. Requires `~/.secrets/openai_api_key`.

### Step 9 — Install LaunchAgents (scheduler)

```bash
./bin/install-agents.sh
```

Installs macOS LaunchAgents (see `bin/install-agents.sh::SERVICES`). Survive sleep/wake. (Rollup is currently manual — no LaunchAgent.)

| Agent | Schedule (IST) | Idle gate |
|-------|---------------|-----------|
| `github-ingest` | :00 and :30, 12h–22h | `state/last_github_success.date` |
| `jira-ingest` | :00 and :30, 12h–22h | `state/last_jira_success.date` |
| `confluence-ingest` | :05 and :35, 12h–22h | `state/last_confluence_success.date` |
| `slack-ingest` | :00 and :30, 12h–22h | **none** — ingests every fire (volume) |
| `slack-discover` | Wed + Fri 13:00 | auto-discovers new team channels |
| `leaves` | daily 04:00 | regex prefilter + render (chat steps manual) |
| `codegraph` | daily 18:00 | git fetch + full code-graph rebuild (feeds `/ask` code-logic) |
| `housekeeping-review` | weekly Mon (Claude routine) | deterministic prune **+** a classification layer that scans for further cleanup candidates and posts Approve/Reject cards to #rollup (suggest-only; git-safe apply) |
| rollup | **manual** (no LaunchAgent) | invoke `/rollup` in chat |

**Retry policy (idle-gated agents):** fires every 30 min; checks gate file (YYYY-MM-DD local time). If today's date present → exits immediately. First success writes today's date → idles rest of day. Auth/network failure → auto-retries next fire. `slack-ingest` has no gate and ingests on every fire.

Check health: `./bin/cron-status.sh`

### Step 10 — Wire management copilot

```bash
# verify the symlink exists — should point to: $HOME/context/work-context/derived
ls -la ~/context/management/context/activity

# if missing:
ln -s ~/context/work-context/derived ~/context/management/context/activity
```

Open `~/context/management/` in Claude Code. `CLAUDE.md` auto-loads and reads `context/activity/` for live team data.

---

## Running ingest manually (day-to-day)

```bash
# dry run — no writes
.venv/bin/python ingest/github.py --dry-run

# normal incremental run (bypasses idle guard, uses cursor)
.venv/bin/python ingest/github.py

# via wrapper (respects idle guard — skips if today already succeeded)
./ingest/run-github.sh

# full backfill (does NOT write idle gate file)
.venv/bin/python ingest/github.py --reset-cursor

# force idle guard to allow wrapper re-run today
echo "2000-01-01" > state/last_github_success.date

# ingest specific repos only (per-run override of config sources.yaml github.repos)
.venv/bin/python ingest/github.py --repo your-org/other-repo

# ingest specific Jira project
.venv/bin/python ingest/jira.py --project PLAT
```

Same pattern for `jira.py` and `confluence.py`.

---

## Core logic

Canonical detail (full `Event` shape, event-type tables, classification internals) lives in `work-context/ARCHITECTURE.md` + `work-context/README.md` → SCHEMA. Key behaviours:

**Unified event model** — every source normalises to the same `Event` (id, source, event_type, ts, actor, subject, title, body, url, refs `{people,projects,tickets,pages}`, raw_path) before storage. `pr_merged_by`: GitHub list API returns `merged_by: null`, so ingest fetches each merged PR individually to get the actual merger (idempotent — skips if event already in DB).

**Identity unification** — at ingest, `actor` = source-native ID. Rollup builds an `alias_map` from every people.yaml field (github, email, jira_id, git_name) → canonical GitHub handle; all cross-source attribution collapses to it. Confluence uses `jira_id` as the actor field — pages by anyone whose `jira_id` isn't in people.yaml are silently skipped.

**Domain classification — five mechanisms, in priority order:**

1. **Jira epic anchor (deterministic)** — if an issue's epic key matches `jira_epics` in projects.yaml, tag to that slug immediately (no LLM). Auto-applied via `llm_classifier._apply_epic_anchor`, even on chat-emitted verdicts.
2. **Auto-slug from new in-window Epics** — `derive/dump_pending.py::_detect_new_epic_slugs` filters `issue_type == "Epic"` (added by `ingest/backfill-jira-issue-type.py` one-shot, maintained by `ingest/jira.py::normalize_issue_created`). For each unmapped Epic in the window: kebab-case slug from title + bigram-only keywords, appended via `_persist_auto_slugs`. **Only Epics** create new slugs; CMRs/Tasks/Bugs link to existing via keywords.
3. **LLM slug synthesis for unmapped epics referenced by children (`/slug-epics`)** — when a child's `epic_key` is missing from projects.yaml AND the Epic is outside the dump window, `derive/rollup.py::_emit_pending_slug_creation` bundles the epic title+body + recent child titles/bodies into `state/pending_slug_creation.json`; `manual-rollup.sh dump` halts and prompts `/slug-epics`, which synthesises a slug + bigram keywords (optional `merge_into`). `apply-slugs` folds verdicts into projects.yaml and invalidates affected `subject_summary` rows. Replaces prior `epic-<key>` fabrication.
4. **Chat classifier (`/rollup`)** — events with no epic/keyword match land in `state/pending_classification.json`; chat reads rules, writes `verdicts.json`. Thin GitHub PRs: inline `gh pr diff` (the `needs_diff: true` flag is dead in chat path; `apply_verdicts.py` rejects verdicts still setting it).
5. **Keyword fallback (cron + script path)** — with auth stripped (always, per policy), `llm_classifier._fallback_classify` does case-insensitive substring match against projects.yaml keywords. Clean hits → `subject_summary` directly; misses stay pending. Cache keyed by `(subject, content_hash)`.

Removed: `derive/algo_classify.py` (algorithmic bulk classifier w/ embedded `CMR_BODY_HINTS`). See `work-context/ARCHITECTURE.md` §3.2.

**MatterAI signal** — every PR gets a `matterai[bot]` review (`🧪 PR Review is completed: <one-line summary>`). Rollup extracts it into person + project files for instant risk triage (`critical`, `panic`, `race condition`, `security`) without reading diffs.

**Auth resolution (LLM paths) — superseded.** As of 2026-05-12 scripts never call Anthropic: both `derive/run-rollup.sh` (cron) and `derive/manual-rollup.sh` export empty `ANTHROPIC_API_KEY` + `ANTHROPIC_AUTH_TOKEN` before invoking `rollup.py`, which short-circuits to `_fallback_classify`. All semantic classification happens in the live Claude Code session via `/rollup` or `/classify`. Reason: scripts previously raced the chat session for OAuth quota → 429s; `_call_claude` now raises `RuntimeError` on retry exhaustion (fail-loud) so accidental auth presence surfaces. Historical resolution order (still coded, never reached): (1) `~/.secrets/anthropic_api_key`, (2) Claude Code OAuth (Keychain `Claude Code-credentials` or `~/.claude/.credentials.json`), (3) skip → keyword fallback (the only path that runs today).

---

## Config reference

Full schemas: `work-context/README.md`.

`config/people.yaml` and `config/projects.yaml` — see Steps 3 and 6 above.

| File | Purpose |
|------|---------|
| `config/slack_channels.yaml` | Slack channel allow-list + per-channel `ingest_mode` (`full` / `team_involved`). Auto-populated by `derive/slack_discover_channels.py`. |
| `config/teams.yaml` | Team enumeration for content-first ownership classification (LLM reads descriptions). |
| `config/domain_team_map.yaml` | Domain-slug → owning-team map (`default_team` + `overrides` + `review`). Consumed by `derive/ownership_resolve.py`. |
| `config/team_subteams.yaml` | Slack user-group IDs representing the team — keeps `team_involved` threads paged via `<!subteam^S…>`. |
| `config/tier_expectations.yaml` | Per-tier throughput/quality ranges + work-window (12–20 IST). Used by `/ask person_range`. |

---

## Key design decisions

- **Cron + direct API tokens, not Claude Code routines.** Routines need interactive auth refresh. Cron + PAT/API token = headless, zero AI cost, deterministic.
- **JSONL + SQLite only.** No Postgres/Elasticsearch/vector DB. JSONL = append-only audit trail; SQLite = sufficient for all query patterns here.
- **Separate management/ and work-context/.** Different edit patterns, sizes, threat models. Backups: management/ → private git remote; work-context/ → local encrypted disk only.
- **Epic anchor first.** Jira epic link is deterministic; LLM only runs on items without an epic match.
- **Cache by content hash.** `subject_summary` keyed by `(subject, content_hash)`. ~3–6 LLM calls per steady-state nightly run.
- **WAL mode on SQLite.** Concurrent ingest processes OK; 30s busy timeout. If locked: `lsof index/events.db` → `kill -9 <pid>` → `PRAGMA wal_checkpoint(TRUNCATE)`.
- **`--reset-cursor` does not write idle gate.** Backfill isn't a "today's incremental succeeded" signal; the gate is written only on normal incremental runs.

---

## Debugging

See `work-context/README.md` for: DB locked / zombie process recovery; auth failure diagnosis per source; rollup 429s (OAuth quota exhaustion); verifying event counts; resetting a source cursor.

---

## Management copilot (`management/`)

Open `~/context/management/` in Claude Code. On session start it:

1. Reads `context/activity/alerts.md` — stale PRs, drive-by merges
2. Runs `tail -n 30 audit/log.jsonl` — recent agent actions
3. Reads most recent `sessions/*.md` — prior session context
4. Summarises open threads before taking new actions

Hard rules in `management/CLAUDE.md`:
- Always pass Confluence cloudId explicitly: `YOUR_CONFLUENCE_CLOUD_ID`
- Never paraphrase TRD/PRD from memory — fetch the page first
- State intent before any mutation outside `drafts/`
- Counter/charge config belongs in service-layer (YAML/Go), not DB
- Flag race conditions on "first row of month" with `SELECT FOR UPDATE`

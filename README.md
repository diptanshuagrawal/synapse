# context

Personal engineering management copilot. Pulls real activity from GitHub, Jira, and Confluence into a local warehouse, derives markdown rollups, and feeds them to a Claude Code agent that answers EM questions grounded in what the team actually shipped.

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

**See [`work-context/ARCHITECTURE.md`](work-context/ARCHITECTURE.md) for the full code-graph tour** — community map, execution flows, per-module function reference, cross-community coupling. Regenerated via `mcp__code-review-graph__build_or_update_graph_tool(full_rebuild=true)` after structural changes.

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

---

## Fresh machine setup — step by step

### Step 1 — Python env

```bash
cd ~/context/work-context
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

### Step 2 — Secrets

All tokens at `~/.secrets/` (mode 600, never committed):

| File | Used by | How to get |
|------|---------|------------|
| `github_pat` | GitHub ingest | GitHub → Settings → Developer settings → Personal access tokens → `repo` + `read:org` scopes |
| `atlassian_token` | Jira + Confluence ingest | https://id.atlassian.com/manage-profile/security/api-tokens |
| `atlassian_email` | same | your Atlassian login email (optional — defaults to `owner@example.com`) |
| `anthropic_api_key` | rollup LLM classifier | optional — see Rollup section |
| `openai_api_key` | embedding pipeline (`embed_subjects.py`) | OpenAI platform key. **Embeddings only** (`text-embedding-3-*`) — never used for chat/LLM work |

```bash
mkdir -p ~/.secrets
echo "ghp_..." > ~/.secrets/github_pat
echo "your-atlassian-token" > ~/.secrets/atlassian_token
echo "you@yourorg.com" > ~/.secrets/atlassian_email
echo "sk-..." > ~/.secrets/openai_api_key       # optional — only if running embeddings
chmod 600 ~/.secrets/*
```

**Slack token is separate** — Slack ingest reads `SLACK_USER_TOKEN` (a `xoxp-…`
user token) from `~/context/.env` (gitignored), **not** `~/.secrets/`. Slack
scripts fail-loud if `ANTHROPIC_API_KEY` is also in env (chat-only-classification
policy). See Step 5 + `runbook/slack-token-rotate.md`.

---

### Step 3 — Populate people.yaml (do this before any ingest)

`config/people.yaml` is the cross-source identity map. **Must be populated before running Confluence ingest** — Confluence filters pages to team members only using `jira_id`. If no jira_ids are present, no Confluence events will be ingested.

Add every team member:

```yaml
people:
  - name: Alice Example
    canonical: org-alice01     # GitHub handle — join key everywhere
    github: org-alice01
    email: alice.example@yourorg.com
    jira_id: "EXAMPLE_ACCOUNT_ID"      # Atlassian account ID — needed for Confluence filter
    git_name: "Alice Example"   # matches git commit author field
```

**How to find jira_id:** In Jira, open any issue the person created or commented on → click their avatar → "View Profile" → the URL contains their accountId (format: `NNNNNN:xxxx-xxxx-...`). Or use the Atlassian MCP tool `lookupJiraAccountId`.

Required fields: `canonical`, `github`, `jira_id`. Others improve attribution.

---

### Step 4 — Configure your org (one file, no code edits)

All org-specific values live in `work-context/config/sources.yaml` (gitignored —
real values never tracked). Copy the template and fill it in:

```bash
cp work-context/config/sources.example.yaml work-context/config/sources.yaml
$EDITOR work-context/config/sources.yaml
```

Set `github.repos` + `github.org`, `jira.project_keys`, `atlassian.host`,
`slack.workspace`, `teams.home`/`coowner`, `launchd.prefix`, etc.
`derive/sources_config.py` loads it (falling back to `sources.example.yaml`
generic placeholders, with per-key env overrides: `GITHUB_ORG`, `JIRA_DOMAIN`,
`JIRA_PROJECT_KEYS`, `SLACK_WORKSPACE`, `ATLASSIAN_EMAIL`).

Per-run overrides still work: `ingest/github.py --repo your-org/other`,
`ingest/jira.py --project PLAT`. Confluence shares `atlassian.host`.

See `work-context/README.md` → "Configuration" for the full field reference.

---

### Step 5 — Configure Slack workspace + channels

Slack is the 4th ingest source (direct Web API; the older MCP path is legacy).
Full setup detail lives in `work-context/README.md` → "Slack workspace + channels".
Minimum:

1. Generate a Slack **User OAuth token** (`xoxp-…`) — scopes in `runbook/slack-token-rotate.md`.
2. Save to `~/context/.env` (gitignored): `SLACK_USER_TOKEN=xoxp-…`
3. Set the channel allow-list — auto-populate it instead of hand-editing:

```bash
cd ~/context/work-context
# scan team-active channels you're a member of, decide ingest_mode, append to yaml
.venv/bin/python derive/slack_discover_channels.py --auto-mode --top 200 --apply
```

Per-channel `ingest_mode`: `full` (store every message, default) or
`team_involved` (keep only threads the team participates in — by author, `@UID`
mention, or `<!subteam^S…>` ping from `config/team_subteams.yaml`). 1:1 DMs are
always skipped; MPIMs need `allow_mpim: true`. New channels auto-bootstrap from
`now − 365d` on first ingest, so no manual backfill is needed for typical adds.

---

### Step 6 — Configure projects.yaml (domain taxonomy)

`config/projects.yaml` defines the domains that events get tagged to. This drives all per-project and per-person rollup output.

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

Start with the epics your team owns. Keywords can be refined after first rollup. You don't need to cover everything — the LLM classifier handles the rest.

**Classification priority:** jira_epics match → LLM classifier (two-pass) → keyword fallback.

---

### Step 7 — First ingest

Run each source with `--reset-cursor` to pull full history. GitHub fetches all PRs/commits/reviews since the repo's beginning. Jira fetches all issues ever updated in the project. Confluence fetches all pages authored by team members.

Expect this to take **5–30 minutes** depending on repo/project size.

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

For an explicit historical backfill of a specific channel, use
`ingest/slack_backfill_app.py` (or the `/slack-backfill` command) — see
`work-context/README.md`.

Do a dry run first if you want to preview without writing:

```bash
.venv/bin/python ingest/github.py --reset-cursor --dry-run
```

After each run, verify event counts:

```bash
sqlite3 index/events.db "SELECT source, event_type, count(*) FROM events GROUP BY source, event_type ORDER BY source, event_type;"
```

**Note:** `--reset-cursor` does NOT write the idle gate file (`state/last_*_success.date`). This is intentional — backfill runs don't count as "today's incremental succeeded", so the LaunchAgent can still run today's incremental pass.

---

### Step 8 — First rollup

Rollup reads `index/events.db` and regenerates all `derived/` markdown. **Policy as of 2026-05-12: all semantic classification flows through chat.** Scripts strip Anthropic auth before invoking `rollup.py` — they only run keyword fallback against `config/projects.yaml`. Any subject without a clean keyword hit lands in pending and gets chat-classified.

**Default window: 30 days.** Use `--days 90` (or `/rollup 90`) for a richer initial view.

#### Primary workflow — `/rollup` slash command (chat-driven)

In a Claude Code session at `~/context/work-context/`:

```
/rollup           # 240 days (default)
/rollup 90        # 90 days
/classify         # phase 2 only — when verdicts.json got wiped
```

`/rollup` runs three phases end-to-end:

1. **Dump** — `manual-rollup.sh dump` → keyword pass → unclassified subjects land in `state/pending_classification.json` + sibling `.rules.md`.
2. **Classify** — chat reads `.rules.md` first (objectivity lock), then classifies the JSON. Thin GitHub PRs fetched inline via `gh pr diff <num> --repo <owner/repo>`. Output → `state/verdicts.json`.
3. **Apply** — `manual-rollup.sh apply` validates (slug enum, conf threshold, risk-flag enum, epic anchor re-apply), inserts into `subject_summary` cache, archives verdicts, reruns rollup (full cache hit).

Phase 3b — narratives (LEGACY, off by default since 2026-05-22):

```bash
# NARRATIVE=1 ./derive/manual-rollup.sh narrate-dump   # legacy path
```

Superseded by `/ask person_range` + `/retro` (see "Per-person signals + retros" section below).

#### Background workflow — daily cron

**Rollup is currently MANUAL** — no background LaunchAgent installed as of 2026-05-12. `derive/run-rollup.sh` exists as a wrapper but no plist + no install-script entry. To re-enable daily rollup: create `launchagents/com.example.rollup.plist` + add to `bin/install-agents.sh::SERVICES`. Until then, EM invokes `/rollup` interactively (weekly cadence in practice).

#### Removed (2026-05-12)

- `derive/algo_classify.py` — algorithmic bulk classifier with embedded `CMR_BODY_HINTS` dict
- `.claude/commands/bulk-rollup.md` — `/bulk-rollup` slash command

Replaced by chat-only flow above. Rationale: single source of truth, no embedded heuristics drifting from chat logic. New patterns get added to `config/projects.yaml` keywords, not code.

#### Verify output

```bash
ls derived/people/
ls derived/projects/
cat derived/alerts.md
```

#### Rollup flags reference

| Flag | Default | Effect |
|------|---------|--------|
| `--days N` | 30 | Lookback window in days |
| `--week` | off | Also generate `derived/weekly/YYYY-Wnn.md` |
| `--detail-summary` | off | Richer 3–5 sentence per-PR narrative (3× token cost) |
| `--skip-narrative` | off | LEGACY — narrative.py path is off by default since 2026-05-22 |

---

### Step 8b — Per-person signals + retros (new pipeline)

Replaced `derive/narrative.py` on 2026-05-22. See `work-context/README.md` "Per-person signals + retros" section for full architecture.

**Per-IC narrative (`/ask person_range`):**

```
/ask what <person> worked on between <since> and <until>
```

Routes to `derive/person_deepread.py` (one-shot bundle, disk-cached) → `derive/person_profile.py` (deterministic signals: contribution / behavioral / throughput / quality / fate / lookahead) → renders TL;DR-first prose. Output saved to `management/narratives/per-person/<handle>-<since>-to-<until>.md`.

**Stakeholder retro (`/retro` and `/ask highs_lows`):**

```
/retro since=2026-04-01 until=2026-04-30
/ask highs and lows for April
```

Stakeholder-facing: team-level voice (NEVER dev names), Highs = deliveries only (code-Done ≠ delivery), measurable impact from slack threads, no PR/ticket/cluster jargon. Output saved to `management/retros/<since>-to-<until>.md`.

**Config source of truth:** `work-context/config/tier_expectations.yaml` (reliability gates, work_hours 12-20 IST, lookahead 30d, fate_max 90d).

**Pace signal:** PR cycle time only. Ticket lead-time is bogus for this team (same-day create+Done flips).

**Cluster status vs window:** For windows ≥30 days old, render against `window_state` (lifetime-overlap derived per query in `derive/ask_engine.py`), NOT `topic_brief.status` (NOW-snapshot).

**Embedding + topic clusters (feeds `cluster_pulse` / retro):** subjects are
embedded (`derive/embed_subjects.py`, OpenAI `text-embedding-3-*` only) →
clustered (`derive/cluster_subjects.py`) → LLM-enriched + named
(`derive/enrich_clusters.py`, `derive/label_clusters.py`) → linked to
`projects.yaml` slugs (`derive/link_clusters_to_projects.py`). Output lands in
the `embedding` / `topic_brief` / `topic_brief_member` / `cluster_project_map`
tables (see `SCHEMA.md`). Refresh incrementally after new ingest with
`/refresh-embeddings`; sanity-check with `/embed-validate`. Requires
`~/.secrets/openai_api_key`.

---

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
| `slack-discover` | Wed + Fri 13:00 | — auto-discovers new team channels |
| `leaves` | daily 04:00 | — regex prefilter + render (chat steps manual) |
| `codegraph` | daily 18:00 | — git fetch + full code-graph rebuild (feeds `/ask` code-logic) |
| `housekeeping` | Sun 03:00 | — log rotation / cache cleanup |
| rollup | **manual** (no LaunchAgent) | — invoke `/rollup` in chat |

**Retry policy (idle-gated agents):** fires every 30 min; checks gate file (YYYY-MM-DD local time). If today's date is present, exits immediately. First success writes today's date → idles rest of day. Auth/network failure → auto-retries at next fire. `slack-ingest` has no gate and ingests on every fire.

Check health:
```bash
./bin/cron-status.sh
```

---

### Step 10 — Wire management copilot

Verify the symlink exists:
```bash
ls -la ~/context/management/context/activity
# should point to: $HOME/context/work-context/derived
```

If missing:
```bash
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

### Unified event model

Every source normalises to the same `Event` shape before storage:

```
id          globally unique: github:example-org/service-a:pr:847:pr_merged
source      github | jira | confluence
event_type  see table below
ts          ISO8601 UTC
actor       source-native ID (GitHub login, Jira accountId)
subject     human reference (example-org/service-a#847)
title       one-line summary
body        full text
url         canonical URL
refs        {people, projects, tickets, pages} — enriched at ingest
raw_path    raw/github/2026/05/05.jsonl#12
```

| Source | event_type |
|--------|------------|
| github | `pr_opened`, `pr_merged`, `pr_closed`, `pr_merged_by`, `review`, `comment`, `commit_in_pr`, `commit_pushed` |
| jira | `issue_created`, `status_change`, `assignment`, `comment` |
| confluence | `page_created`, `page_updated`, `comment` |

**`pr_merged_by` note:** GitHub list API always returns `merged_by: null`. Ingest fetches each merged PR individually to get the actual merger. Only fetches if event doesn't already exist in DB (idempotent).

### Identity unification

At ingest: `actor` = source-native ID. Rollup builds an `alias_map` from every field in people.yaml (github, email, jira_id, git_name) → canonical handle. All cross-source attribution collapses to canonical GitHub handle in derived output.

**Confluence specifically:** uses `jira_id` as actor field. Must be present in people.yaml for person-level attribution and team filtering. Pages authored by anyone whose `jira_id` is not in people.yaml are silently skipped.

### Domain classification — epic to slug

Four mechanisms, in priority order:

**1. Jira epic anchor (deterministic)**
Every Jira issue carries an epic link. If the epic key matches `jira_epics` in projects.yaml, the event is tagged to that slug immediately — no LLM, no ambiguity. Auto-applied via `llm_classifier._apply_epic_anchor` even on chat-emitted verdicts.

**2. Auto-slug from new in-window Epics**
`derive/dump_pending.py::_detect_new_epic_slugs` filters `issue_type == "Epic"` (added via `ingest/backfill-jira-issue-type.py` one-shot, then maintained by `ingest/jira.py::normalize_issue_created`). For each unmapped Epic in the dump window: generate kebab-case slug from title, bigram-only keywords, append to `projects.yaml` via `_persist_auto_slugs`. **Only Epics** create new slugs — CMRs/Tasks/Bugs link to existing ones via keywords.

**3. LLM slug synthesis for unmapped epics referenced by children (`/slug-epics`)**
When a child subject's `epic_key` is missing from `projects.yaml` AND the Epic itself is outside the dump window, `derive/rollup.py::_emit_pending_slug_creation` bundles the epic's title + body + most recent child-ticket titles/bodies into `state/pending_slug_creation.json`. `manual-rollup.sh dump` halts and prompts the chat to run `/slug-epics`, which synthesises a human-readable slug + bigram keywords (with optional `merge_into` for existing slugs). `apply-slugs` folds verdicts into `projects.yaml` and invalidates affected `subject_summary` rows. Replaces the prior fabrication of `epic-<key>` slugs.

**4. Chat classifier (`/rollup`)**
For events without an epic match and no keyword hit: subject lands in `state/pending_classification.json`. Chat reads the rules file, classifies, writes `verdicts.json`. Thin GitHub PRs: inline `gh pr diff` (the `needs_diff: true` flag is dead in chat path; `apply_verdicts.py` rejects any verdict still setting it).

**5. Keyword fallback (cron + manual-rollup script path)**
When auth stripped (always, per chat-only policy): `llm_classifier._fallback_classify` does case-insensitive substring match against `projects.yaml` keywords. Clean hits emit verdicts directly to `subject_summary`. Misses stay pending for chat to handle. Cache: `subject_summary` table, keyed by `(subject, content_hash)`.

Removed: `derive/algo_classify.py` (algorithmic bulk classifier with embedded `CMR_BODY_HINTS`). See `work-context/ARCHITECTURE.md` §3.2.

### MatterAI signal

Every PR gets a `matterai[bot]` review:
```
🧪 PR Review is completed: <one-line summary>
```
Rollup extracts this and bakes it into person + project files — instant risk triage (`critical`, `panic`, `race condition`, `security`) without reading diffs.

### Auth resolution (LLM paths) — superseded

**As of 2026-05-12, scripts never call Anthropic.** Both `derive/run-rollup.sh` (cron) and `derive/manual-rollup.sh` (slash-command-backed) explicitly export empty `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` before invoking `rollup.py`. The rollup script short-circuits to `_fallback_classify` keyword path. All semantic classification happens in the active Claude Code session via `/rollup` or `/classify`.

Reason: scripts previously raced the chat session for OAuth quota, producing 429s. Defensive `_call_claude` now raises `RuntimeError` on retry exhaustion (fail-loud) so any accidental auth presence surfaces visibly.

Historical resolution order (still implemented in code, never reached in practice):

1. `~/.secrets/anthropic_api_key` — paid API
2. Claude Code OAuth — Keychain (`Claude Code-credentials`) or `~/.claude/.credentials.json`
3. Skip → keyword fallback (the only path that runs today)

---

## Config reference

### `config/people.yaml`

```yaml
people:
  - name: Eve Example
    canonical: org-eve03     # GitHub handle = canonical everywhere
    github: org-eve03
    email: eve.e@yourorg.com
    jira_id: "EXAMPLE_ACCOUNT_ID"      # required for Confluence
    git_name: "Eve Example"       # matches git commit author field
```

### `config/projects.yaml`

```yaml
projects:
  - slug: instant-pay-atm
    name: Instant-Pay ATM Charges
    keywords: [instantpay_atm, InstantPayAtm, purpose_code, atm_txn_counter]
    jira_epics: [EX-2238, EX-185]    # matched first — authoritative
    confluence_pages: [EXAMPLE_PAGE_ID]        # page IDs from URLs
```

### Other config files

Full schemas in `work-context/README.md`.

| File | Purpose |
|------|---------|
| `config/slack_channels.yaml` | Slack channel allow-list + per-channel `ingest_mode` (`full` / `team_involved`). Auto-populated by `derive/slack_discover_channels.py`. |
| `config/teams.yaml` | Team enumeration for content-first ownership classification (LLM reads descriptions). |
| `config/domain_team_map.yaml` | Domain-slug → owning-team map (`default_team` + `overrides` + `review`). Consumed by `derive/ownership_resolve.py`. |
| `config/team_subteams.yaml` | Slack user-group IDs representing the team — keeps `team_involved` threads paged via `<!subteam^S…>`. |
| `config/tier_expectations.yaml` | Per-tier throughput/quality ranges + work-window (12–20 IST). Used by `/ask person_range`. |

---

## Key design decisions

**Cron + direct API tokens, not Claude Code routines.** Routines need interactive auth refresh. Cron + PAT/API token = headless, zero AI cost, deterministic.

**JSONL + SQLite only.** No Postgres, no Elasticsearch, no vector DB. JSONL = append-only audit trail. SQLite = sufficient for all query patterns here.

**Separate management/ and work-context/.** Different edit patterns, sizes, threat models. Backups: management/ → private git remote; work-context/ → local encrypted disk only.

**Epic anchor first.** Jira epic link is deterministic. LLM only runs on items without an epic match.

**Cache by content hash.** `subject_summary` keyed by `(subject, content_hash)`. ~3–6 LLM calls per steady-state nightly run.

**WAL mode on SQLite.** Multiple ingest processes can run concurrently. 30s busy timeout. If locked: `lsof index/events.db` → `kill -9 <pid>` → `PRAGMA wal_checkpoint(TRUNCATE)`.

**`--reset-cursor` does not write idle gate.** Backfill is not a "today's incremental succeeded" signal. Gate is only written on normal incremental runs.

---

## Debugging

See `work-context/README.md` for:
- DB locked / zombie process recovery
- Auth failure diagnosis per source
- Rollup 429s (OAuth quota exhaustion)
- Verifying event counts
- Resetting a source cursor

---

## Management copilot (`management/`)

Open `~/context/management/` in Claude Code. On session start:

1. Reads `context/activity/alerts.md` — stale PRs, drive-by merges
2. Runs `tail -n 30 audit/log.jsonl` — recent agent actions
3. Reads most recent `sessions/*.md` — prior session context
4. Summarises open threads before taking new actions

Hard rules baked into `management/CLAUDE.md`:
- Always pass Confluence cloudId explicitly: `YOUR_CONFLUENCE_CLOUD_ID`
- Never paraphrase TRD/PRD from memory — fetch the page first
- State intent before any mutation outside `drafts/`
- Counter/charge config belongs in service-layer (YAML/Go), not DB
- Flag race conditions on "first row of month" with `SELECT FOR UPDATE`

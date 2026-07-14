# work-context event schema

## Unified event shape

Every source normalizes to this before storage. Enables cross-source chronological queries.

```json
{
  "id": "github:example-org/service-a:pr:847",
  "source": "github",
  "event_type": "pr_merged",
  "ts": "2026-05-05T11:02:00Z",
  "actor": "eve-example",
  "subject": "example-org/service-a#847",
  "title": "feat: charge_rules table migration",
  "body": "Adds the charge_rules table per TRD section 4.2…",
  "url": "https://github.com/example-org/service-a/pull/847",
  "refs": {
    "people": ["eve-example"],
    "projects": ["counter-charge-engine"],
    "tickets": ["EX-1284"],
    "pages": []
  },
  "raw_path": "raw/github/2026/05/05.jsonl#12"
}
```

### Field definitions

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | `<source>:<subject>:<type>:<seq>` — globally unique, stable |
| `source` | string | `github` \| `jira` \| `confluence` \| `slack` |
| `event_type` | string | See per-source list below |
| `ts` | string | ISO8601 UTC |
| `actor` | string | Unified identity key (GitHub login, Jira accountId, etc.) |
| `subject` | string | Human-readable subject reference |
| `title` | string | One-line summary |
| `body` | string | Full text (may be long) |
| `url` | string | Canonical URL |
| `refs` | object | Enriched cross-references (see below) |
| `raw_path` | string | `raw/<source>/YYYY/MM/DD.jsonl#<line>` |

### Event types per source

- **github**: `pr_opened`, `pr_merged`, `pr_closed`, `review`, `commit_pushed`, `comment`
- **jira**: `issue_created`, `status_change`, `comment`, `assignment`
- **confluence**: `page_created`, `page_updated`, `comment`
- **slack**: `message`, `thread_reply`, `mention`, `dm`, `message_edited`

### `refs` enrichment

Populated at ingest time using `config/people.yaml` and `config/projects.yaml`.
Email is the join key across sources.

```json
{
  "people":   ["github-login or slack-handle unified via email"],
  "projects": ["project slug from projects.yaml"],
  "tickets":  ["PROJ-1234 style Jira keys extracted from text"],
  "pages":    ["confluence page IDs extracted from URLs"]
}
```

---

## SQLite schema (`index/events.db`)

```sql
CREATE TABLE events (
  id         TEXT PRIMARY KEY,
  source     TEXT NOT NULL,
  event_type TEXT NOT NULL,
  ts         TEXT NOT NULL,   -- ISO8601 UTC
  actor      TEXT,
  subject    TEXT,
  title      TEXT,
  body       TEXT,
  url        TEXT,
  raw_path   TEXT NOT NULL,

  -- Jira enrichment (NULL for other sources). Added by ingest migrations.
  issue_type   TEXT,    -- Epic | Story | Task | Bug | CMR ...
  story_points REAL,
  sprint_id    INTEGER,
  sprint_name  TEXT,
  sprint_state TEXT,    -- active | closed | future
  assignee     TEXT,    -- assignee at event time (dev-vs-reviewer credit)
  to_status    TEXT,    -- status_change events: destination status

  -- Slack enrichment (NULL for other sources).
  channel_id         TEXT,
  thread_ts          TEXT,    -- Slack epoch ts of thread parent
  edited_ts          TEXT,
  deleted_ts         TEXT,    -- tombstone marker; row kept, content cleared
  reactions_json     TEXT,    -- [{name, count, users}]
  reply_count        INTEGER, -- thread parents only
  drain_attempted_at TEXT,    -- stale-thread drain cooldown
  files_json         TEXT     -- [{id, name, mimetype, size, mode, permalink, user}]
);
CREATE INDEX idx_events_ts          ON events(ts);
CREATE INDEX idx_events_actor_ts    ON events(actor, ts);
CREATE INDEX idx_events_source_ts   ON events(source, ts);
CREATE INDEX idx_events_channel_ts  ON events(channel_id, ts);
CREATE INDEX idx_events_thread_ts   ON events(thread_ts);
CREATE INDEX idx_events_subject     ON events(subject, event_type);
CREATE INDEX idx_events_deleted_ts_partial ON events(source, deleted_ts)
    WHERE source = 'slack' AND deleted_ts IS NOT NULL;
CREATE INDEX idx_events_subject_to_status  ON events(subject, ts)
    WHERE to_status IS NOT NULL;

CREATE TABLE event_refs (
  event_id  TEXT NOT NULL,
  ref_type  TEXT NOT NULL,   -- person | project | ticket | page
  ref_value TEXT NOT NULL,
  role      TEXT,            -- WHY the ref appears (LLM enrich pass, migration 005):
                             -- ASKING_QUESTION | REQUESTING_ACTION | FIXING | BLOCKED_BY |
                             -- DUPLICATE | REFERENCING | PASSING_MENTION | UPDATE_ON | RESOLVED_BY.
                             -- Nullable; ingest inserts NULL — treat NULL as "role unknown".
  PRIMARY KEY (event_id, ref_type, ref_value)
);
CREATE INDEX idx_refs_value ON event_refs(ref_type, ref_value);

CREATE VIRTUAL TABLE events_fts USING fts5(
  title, body,
  content='events',
  content_rowid='rowid'
);
-- (FTS5 also creates internal shadow tables events_fts_data/idx/docsize/config.)

-- Slack ingest retry queue: bot-thread roots whose replies weren't drained yet.
-- Producers: ingest/slack_ingest_app.py, derive/slack_backfill_helper.py.
CREATE TABLE slack_pending_reply_check (
  channel_id  TEXT NOT NULL,
  parent_ts   TEXT NOT NULL,               -- Slack epoch ts of the (bot) root
  reply_count INTEGER,                     -- declared reply_count at enqueue time
  first_seen  TEXT NOT NULL,               -- ISO ts first enqueued
  attempts    INTEGER NOT NULL DEFAULT 0,  -- drain attempts so far (retry/abandon ceiling)
  PRIMARY KEY (channel_id, parent_ts)
);
CREATE INDEX idx_pending_reply_check_chan ON slack_pending_reply_check(channel_id, parent_ts);

-- Identity self-heal: observed actor pairs captured at ingest time.
-- Written by derive/identity_signals.record_signal/record_user_dict;
-- consumed by derive/identity_reconcile.py to back-fill config/people.yaml.
CREATE TABLE identity_signals (
  observed_at TEXT NOT NULL,
  source      TEXT NOT NULL,   -- github | jira | confluence | slack
  key_a_type  TEXT NOT NULL,   -- email | jira_id | slack_id | slack_handle | github | git_name | name
  key_a_value TEXT NOT NULL,
  key_b_type  TEXT NOT NULL,
  key_b_value TEXT NOT NULL,
  n_obs       INTEGER NOT NULL DEFAULT 1,   -- confidence: repeat-sighting counter
  PRIMARY KEY (key_a_type, key_a_value, key_b_type, key_b_value)  -- canonical-ordered pair
);
CREATE INDEX idx_signals_a ON identity_signals(key_a_type, key_a_value);
CREATE INDEX idx_signals_b ON identity_signals(key_b_type, key_b_value);
```

---

## Derived tables (`index/events.db`)

`events` / `event_refs` / `events_fts` are the ingest-time tables above. Everything
below is **machine-generated** by the rollup, embedding, and per-person pipelines.
Re-derivable from `events` + raw JSONL — safe to drop and rebuild.

### Classification cache

```sql
-- Per-subject domain classification + summary. Keyed by (subject, content_hash)
-- so a verdict survives until the underlying content changes.
-- Producers: derive/llm_classifier.py (chat/keyword), derive/apply_verdicts.py.
-- Ownership columns added by derive/ownership_resolve.py.
CREATE TABLE subject_summary (
  subject             TEXT NOT NULL,
  content_hash        TEXT NOT NULL,
  domains             TEXT NOT NULL,   -- json array of project slugs
  summary             TEXT NOT NULL,
  risk_flags          TEXT,            -- json array
  confidence          REAL,
  source              TEXT NOT NULL,   -- 'claude-api' | 'claude-session' | 'fallback'
  model               TEXT,
  classified_at       TEXT NOT NULL,
  input_tokens        INTEGER,
  output_tokens       INTEGER,
  detail              TEXT,
  owned_by_primary    TEXT,            -- canonical owner handle
  co_owners_json      TEXT,
  owned_by_confidence REAL,
  ownership_reasoning TEXT,
  PRIMARY KEY (subject, content_hash)
);
CREATE INDEX idx_subject_summary_subject ON subject_summary(subject);

-- Trigger: a 'fallback' (keyword) verdict must never overwrite an existing
-- 'claude%' (LLM) verdict for the same (subject, content_hash).
CREATE TRIGGER protect_session_rows
BEFORE INSERT ON subject_summary
WHEN NEW.source = 'fallback'
  AND EXISTS (SELECT 1 FROM subject_summary
              WHERE subject = NEW.subject AND content_hash = NEW.content_hash
                AND source LIKE 'claude%')
BEGIN
    SELECT RAISE(IGNORE);
END;
```

### Embedding + topic clustering

```sql
-- One vector per subject. OpenAI text-embedding-3-* only (LLM work stays Claude).
-- Producer: derive/embed_subjects.py. content_sha detects drift for re-embed.
CREATE TABLE embedding (
  subject     TEXT PRIMARY KEY,
  source      TEXT NOT NULL,   -- slack | jira | confluence | github
  vector      BLOB NOT NULL,   -- float32 array, dim = model output
  model       TEXT NOT NULL,   -- e.g. text-embedding-3-small
  dim         INTEGER NOT NULL,
  content_sha TEXT,            -- hash of embedded content
  computed_at TEXT NOT NULL
);
CREATE INDEX idx_embedding_source ON embedding(source);

-- Embed-recipe cache: per-subject content fingerprint so incremental refresh
-- (derive/refresh_embeddings.py, content built by derive/subject_content.py)
-- can skip subjects whose rendered content hasn't changed.
CREATE TABLE embed_content_cache (
  subject        TEXT PRIMARY KEY,
  recipe_version INTEGER NOT NULL,   -- bump invalidates the whole cache
  n_events       INTEGER NOT NULL,
  last_event_ts  TEXT,
  content_sha    TEXT NOT NULL,
  computed_at    TEXT NOT NULL
);

-- One row per topic cluster (cross-source). Producers: derive/cluster_subjects.py
-- (clustering) → derive/enrich_clusters.py / label_clusters.py (LLM enrich + name).
CREATE TABLE topic_brief (
  cluster_id              INTEGER PRIMARY KEY AUTOINCREMENT,
  label                   TEXT,      -- LLM-named topic
  summary                 TEXT,      -- 3-5 sentence "what is this"
  status                  TEXT,      -- ACTIVE | RESOLVED | STALE | RECURRING
  decisions_json          TEXT,      -- [{text, evidence_subject, evidence_phrase}]
  blockers_json           TEXT,
  participants_json       TEXT,      -- [{person, role, contribution_count}]
  source_breakdown_json   TEXT,      -- {slack:N, jira:N, page:N, github:N}
  member_count            INTEGER,
  first_ts                TEXT,
  last_activity_ts        TEXT,
  classifier_version      TEXT,
  computed_at             TEXT,
  confidence              REAL,
  root_cause              TEXT,
  outcomes_json           TEXT,
  followups_json          TEXT,
  risk_areas_json         TEXT,
  stakeholders_json       TEXT,
  artifacts_json          TEXT,
  owner_distribution_json TEXT
);

-- Cluster membership (subject ↔ cluster), with centroid distance + role.
CREATE TABLE topic_brief_member (
  cluster_id  INTEGER NOT NULL,
  subject     TEXT NOT NULL,
  source      TEXT NOT NULL,
  similarity  REAL,         -- distance from cluster centroid, 0-1
  member_role TEXT,         -- KEY_DECISION_THREAD | REFERENCE_DOC | RELATED_TICKET | PASSING_MENTION
  PRIMARY KEY (cluster_id, subject),
  FOREIGN KEY (cluster_id) REFERENCES topic_brief(cluster_id) ON DELETE CASCADE
);
CREATE INDEX idx_topic_brief_member_subject ON topic_brief_member(subject);

-- Cluster → projects.yaml slug mapping. Deterministic linker (jira_epic /
-- confluence_page / domain / keyword rules). Producer: derive/link_clusters_to_projects.py,
-- auto-run after every finalize_refresh apply. Unmapped clusters surface yaml gaps.
CREATE TABLE cluster_project_map (
  cluster_id    INTEGER NOT NULL,
  project_slug  TEXT    NOT NULL,
  confidence    REAL    NOT NULL,
  source        TEXT    NOT NULL,   -- jira_epic | confluence_page | domain | keyword
  evidence_json TEXT,
  computed_at   TEXT    NOT NULL,
  PRIMARY KEY (cluster_id, project_slug)
);
CREATE INDEX idx_cpm_cluster    ON cluster_project_map(cluster_id);
CREATE INDEX idx_cpm_project    ON cluster_project_map(project_slug);
CREATE INDEX idx_cpm_confidence ON cluster_project_map(confidence DESC);

-- Noise-filter decisions: channels/subjects excluded from clustering
-- (alert/bot spam). Producer: derive/cluster_noise_filter.py.
CREATE TABLE cluster_excluded_channel (
  channel_id TEXT PRIMARY KEY,
  name       TEXT,
  reason     TEXT,      -- force_exclude | ratio | name-bootstrap
  noise      INTEGER,   -- sampled noise-thread count
  real       INTEGER,   -- sampled real-thread count
  ratio      REAL,
  decided_at TEXT
);

CREATE TABLE cluster_excluded_subject (
  subject    TEXT PRIMARY KEY,
  channel_id TEXT,
  reason     TEXT,      -- automation-no-reply | force_exclude
  decided_at TEXT
);
```

### Slack thread rollups

```sql
-- One row per Slack thread (subject = 'slack:<channel>:<parent_ts>').
-- Producer: derive/build_thread_summary.py. Denorms channel meta + cross-refs.
CREATE TABLE thread_summary (
  subject              TEXT PRIMARY KEY,
  channel_id           TEXT NOT NULL,
  channel_name         TEXT,            -- denorm from slack_channel_meta cache
  channel_class        TEXT,            -- denorm from slack_channels.yaml
  started_by_canonical TEXT,            -- people.yaml canonical of parent author; NULL if unresolved
  participants_json    TEXT NOT NULL,   -- canonicals; raw U-ids dropped if unresolved
  first_ts             TEXT NOT NULL,
  last_ts              TEXT NOT NULL,   -- latest non-tombstone message
  msg_count            INTEGER NOT NULL,-- parent + replies (excludes deleted)
  reply_count          INTEGER NOT NULL,
  referenced_tickets   TEXT,            -- json array, denormed from event_refs
  referenced_pages     TEXT,            -- json array of page IDs
  referenced_prs       TEXT,            -- json array of 'owner/repo#N'
  referenced_threads   TEXT,            -- json array of cross-referenced slack subjects
  ops_pattern_match    TEXT,            -- incident | drill | rca | year_end | NULL
  digest               TEXT,            -- 1-line LLM summary, lazy at compaction
  computed_at          TEXT NOT NULL
);
CREATE INDEX idx_thread_summary_channel_last ON thread_summary(channel_id, last_ts);
CREATE INDEX idx_thread_summary_started_by   ON thread_summary(started_by_canonical);
CREATE INDEX idx_thread_summary_ops          ON thread_summary(ops_pattern_match)
    WHERE ops_pattern_match IS NOT NULL;

-- LLM enrichment per thread: sentiment/intent/outcome + decisions/blockers.
-- Classifier sees thread + linked tickets/pages/PRs + top-k embedding neighbours.
CREATE TABLE thread_enriched (
  subject                TEXT PRIMARY KEY,
  channel_id             TEXT,
  topic_paraphrase       TEXT,
  sentiment              TEXT,     -- NEUTRAL|REQUEST|FRUSTRATION|CELEBRATION|URGENT
  urgency                INTEGER,  -- 0-3 ordinal
  intent                 TEXT,     -- ASKING|ANNOUNCING|REQUESTING_ACTION|RESOLVING|DEBUGGING|REPORTING
  outcome                TEXT,     -- RESOLVED|DEFERRED|ESCALATED|UNRESOLVED|FYI|REQUEST_DONE
  outcome_summary        TEXT,
  decisions_json         TEXT,     -- [{text, made_by, evidence_phrase}]
  blockers_json          TEXT,     -- [{text, on_whom, ticket_ref, evidence_phrase}]
  participants_json      TEXT,     -- [{person, role}]
  implicit_refs_json     TEXT,     -- [{phrase, resolved_subject?, confidence}]
  cross_source_refs_json TEXT,     -- [{subject, source, ref_role, evidence_phrase, similarity}]
  reply_count_seen       INTEGER,  -- idempotency: skip if no new replies
  classifier_version     TEXT,
  computed_at            TEXT
);
```

### Ownership + per-person + leaves

```sql
-- TRD/confluence-page ownership scoring. Producer: derive/build_trd_owners.py.
CREATE TABLE trd_owners (
  page_id           TEXT PRIMARY KEY,
  title             TEXT NOT NULL,
  owner             TEXT NOT NULL,   -- canonical handle (people.yaml)
  owner_score       REAL NOT NULL,
  scores_json       TEXT NOT NULL,   -- {canonical: score, ...} every scorer
  contributors_json TEXT NOT NULL,   -- [canonical, ...] score >= 30% of owner
  project_slug      TEXT,            -- projects.yaml confluence_pages match (NULL if title-only)
  last_event_ts     TEXT NOT NULL,
  total_events      INTEGER NOT NULL,
  computed_at       TEXT NOT NULL
);
CREATE INDEX idx_trd_owners_owner   ON trd_owners(owner);
CREATE INDEX idx_trd_owners_project ON trd_owners(project_slug);
CREATE INDEX idx_trd_owners_last_ts ON trd_owners(last_event_ts);

-- Cached per-person narrative. Keyed by (actor, window_days, content_hash).
-- Producer: derive/narrative.py / apply_narratives.py.
CREATE TABLE person_narrative (
  actor         TEXT NOT NULL,
  window_days   INTEGER NOT NULL,
  content_hash  TEXT NOT NULL,
  body          TEXT NOT NULL,
  source        TEXT NOT NULL,   -- 'claude-api' | 'claude-session' | 'fallback'
  model         TEXT,
  generated_at  TEXT NOT NULL,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  PRIMARY KEY (actor, window_days, content_hash)
);
CREATE INDEX idx_person_narrative_actor ON person_narrative(actor, window_days);

-- Team leave mentions extracted from Slack. Producers: derive/leaves_dump.py (candidates)
-- → chat verdict → derive/apply_leaves.py (insert). Regex never inserts directly.
CREATE TABLE team_leaves (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id      TEXT NOT NULL,   -- source slack events.id
  actor         TEXT NOT NULL,   -- canonical github handle
  mentioned_at  TEXT NOT NULL,
  date_start    TEXT,            -- YYYY-MM-DD or NULL if ambiguous
  date_end      TEXT,            -- YYYY-MM-DD or NULL if open/single-day
  reason        TEXT,            -- wfh | vacation | sick | holiday | ooo | other
  channel_id    TEXT,
  channel_name  TEXT,
  body_excerpt  TEXT,            -- <= 300 chars
  url           TEXT,
  confidence    REAL,            -- chat verdict 0..1
  extracted_by  TEXT,            -- 'chat'
  classified_at TEXT
);
CREATE INDEX idx_team_leaves_event ON team_leaves(event_id);
CREATE INDEX idx_team_leaves_actor ON team_leaves(actor);
CREATE INDEX idx_team_leaves_dates ON team_leaves(date_start, date_end);

-- Idempotency ledger: which slack events already screened for leave mentions.
CREATE TABLE team_leaves_processed (
  event_id     TEXT PRIMARY KEY,
  processed_at TEXT NOT NULL,
  is_leave     INTEGER NOT NULL,   -- 1 = real leave, 0 = false positive
  confidence   REAL
);
```

### PR quality + friction

```sql
-- Per-PR metadata (size, checks, labels). Producers: ingest/github.py,
-- ingest/github_backfill_pr_meta.py (one-time backfill).
CREATE TABLE pr_meta (
  subject            TEXT PRIMARY KEY,   -- owner/repo#N
  repo               TEXT NOT NULL,
  number             INTEGER NOT NULL,
  state              TEXT,               -- open | closed | merged
  additions          INTEGER,
  deletions          INTEGER,
  files_changed      INTEGER,
  is_draft           INTEGER,            -- 0 | 1
  labels_json        TEXT,               -- JSON list of label names
  head_sha           TEXT,
  checks_status      TEXT,               -- success | failure | pending | none | unknown
  checks_failed_json TEXT,               -- JSON list of failing check-run names
  created_at         TEXT,
  merged_at          TEXT,
  updated_at         TEXT,
  fetched_at         TEXT
);
CREATE INDEX idx_pr_meta_repo  ON pr_meta(repo);
CREATE INDEX idx_pr_meta_state ON pr_meta(state);

-- Root-cause classification of each PR review comment (chat-classified via
-- /pr-quality). Producers: derive/pr_quality_dump.py → derive/apply_pr_classes.py.
CREATE TABLE pr_comment_class (
  event_id      TEXT PRIMARY KEY,   -- events.id of the review / comment
  subject       TEXT NOT NULL,      -- owner/repo#N
  source        TEXT NOT NULL,      -- human | <ai-review-bot> (agentic reviewer tag)
  category      TEXT NOT NULL,      -- root-cause taxonomy (pr_review_rules.md)
  confidence    REAL,
  classified_at TEXT
);
CREATE INDEX idx_pr_comment_class_subject  ON pr_comment_class(subject);
CREATE INDEX idx_pr_comment_class_category ON pr_comment_class(category);

-- Per-PR friction score rolled up from comment classes + mechanical signals.
-- Producer: derive/pr_quality_report.py (rendered by /pr-report).
CREATE TABLE pr_friction (
  subject              TEXT PRIMARY KEY,  -- owner/repo#N
  score                REAL,              -- 0-100
  dominant_category    TEXT,
  mechanical_json      TEXT,              -- size / checks / churn signals
  category_counts_json TEXT,
  computed_at          TEXT
);
```

### Feature / release tracking

```sql
-- CMR tickets as the rollout record (real Released ts, approver, PR links).
-- Producer: derive/cmr_releases.py (CMR release-signal pipeline);
-- slug linkage via derive/feature_resolve.py.
CREATE TABLE feature_release (
  cmr_subject           TEXT NOT NULL,    -- CMR ticket key
  slug                  TEXT NOT NULL,    -- projects.yaml slug, or ''
  linked_via            TEXT,             -- project_ref | impacted_areas | none
  service               TEXT,
  impacted_areas        TEXT,
  pr_urls_json          TEXT,             -- JSON list of owner/repo#N
  release_owner         TEXT,
  created_at            TEXT,             -- CMR issue_created ts
  approval_requested_at TEXT,
  approved_at           TEXT,
  approved_by           TEXT,
  released_at           TEXT,
  outcome               TEXT,             -- released|emergency|rolled_back|cancelled|pending
  is_feature_release    INTEGER,          -- 0 | 1 (feature rollout vs ops CMR)
  title                 TEXT,
  url                   TEXT,
  computed_at           TEXT,
  PRIMARY KEY (cmr_subject, slug)
);
CREATE INDEX idx_feature_release_slug    ON feature_release(slug);
CREATE INDEX idx_feature_release_outcome ON feature_release(outcome);
CREATE INDEX idx_feature_release_relts   ON feature_release(released_at);

-- Lifecycle stage detection per feature slug (planning → trd → code_dev → rollout).
-- Producer: derive/feature_stages.py; consumed by the feature-narrative pipeline.
CREATE TABLE feature_stage (
  slug             TEXT NOT NULL,
  scope            TEXT NOT NULL DEFAULT '',  -- '' = domain rollup; else the anchor epic key
  stage            TEXT NOT NULL,   -- planning | trd | code_dev | rollout
  entered_at       TEXT,
  detection_source TEXT,
  confidence       TEXT,            -- high | medium | low
  artefact_count   INTEGER,
  detail_json      TEXT,
  computed_at      TEXT,
  PRIMARY KEY (slug, scope, stage)
);
CREATE INDEX idx_feature_stage_slug ON feature_stage(slug);
```

> Backup tables (`topic_brief_bak_*`, `topic_brief_member_bak_*`) are transient
> snapshots written by `derive/finalize_refresh.py` before a cluster re-derive.
> Not part of the stable schema.

---

## Second database: `state/doc_sync.db`

Doc-sync automation state — one row per drift-finding comment the sweep left on a
Confluence page. Owner script: `derive/doc_sync_state.py` (written by the doc-sync
sweep/digest routines; drives the pending-review digest + Relay approve/reject flow).

```sql
CREATE TABLE doc_sync_comments (
  comment_id        TEXT PRIMARY KEY,
  page_id           TEXT NOT NULL,
  page_title        TEXT NOT NULL,
  page_url          TEXT NOT NULL,
  comment_url       TEXT NOT NULL,
  owner_account     TEXT,            -- Confluence/Jira account id (people.yaml jira_id)
  severity          TEXT,            -- major | medium | minor
  check_type        TEXT,            -- schema | behavior | decision | dependency | lld | sequence
  finding_title     TEXT NOT NULL,   -- short headline shown in the digest
  anchor            TEXT,            -- inline text the comment is anchored to
  created_ts        TEXT,
  resolution_status TEXT DEFAULT 'open',  -- open | resolved | dangling | reopened
  last_checked_ts   TEXT,
  sweep_run_id      TEXT,
  finding_key       TEXT            -- stable dedup key for re-found findings
);
CREATE INDEX idx_docsync_status  ON doc_sync_comments(resolution_status);
CREATE INDEX idx_docsync_owner   ON doc_sync_comments(owner_account);
CREATE INDEX idx_docsync_finding ON doc_sync_comments(finding_key);
```

---

## Storage layout

```
raw/<source>/YYYY/MM/DD.jsonl   — append-only raw events, one JSON per line
index/events.db                 — SQLite unified index
state/cursors.json              — last-seen IDs/timestamps per source
state/doc_sync.db               — doc-sync automation state (see above)
derived/                        — machine-generated rollups (agent-readable, do not write)
```

## Cadences

LaunchAgents use `StartCalendarInterval` (not `StartInterval`) — they fire on a
fixed clock every 30 min inside the work window, not on a rolling timer. See
`launchagents/*.plist`, installed by `bin/install-agents.sh`.

| Agent | Schedule (IST) | Idle guard |
|-------|----------------|------------|
| `slack-ingest`      | every 30 min, 12:00–23:00 | **none** — ingests every fire (volume justifies it) |
| `github-ingest`     | every 30 min, 12:00–22:30 | gated → one success/day (`last_github_success.date`) |
| `jira-ingest`       | every 30 min, 12:00–22:30 | gated → one success/day |
| `confluence-ingest` | every 30 min, 12:05–22:35 | gated → one success/day |
| `leaves`            | daily 04:00 | — |
| `slack-discover`    | Wed + Fri 13:00 | — |
| `housekeeping-review` | weekly Mon (routine) | gated → one success/week (prune + classify→#rollup) |

Idle-gated agents still fire every 30 min but exit early once the day's success
file is stamped — so each source lands at most one full ingest per day.

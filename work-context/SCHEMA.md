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
  raw_path   TEXT NOT NULL
);
CREATE INDEX idx_events_ts          ON events(ts);
CREATE INDEX idx_events_actor_ts    ON events(actor, ts);
CREATE INDEX idx_events_source_ts   ON events(source, ts);

CREATE TABLE event_refs (
  event_id  TEXT NOT NULL,
  ref_type  TEXT NOT NULL,   -- person | project | ticket | page
  ref_value TEXT NOT NULL,
  PRIMARY KEY (event_id, ref_type, ref_value)
);
CREATE INDEX idx_refs_value ON event_refs(ref_type, ref_value);

CREATE VIRTUAL TABLE events_fts USING fts5(
  title, body,
  content='events',
  content_rowid='rowid'
);

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

> Backup tables (`topic_brief_bak_*`, `topic_brief_member_bak_*`) are transient
> snapshots written by `derive/finalize_refresh.py` before a cluster re-derive.
> Not part of the stable schema.

---

## Storage layout

```
raw/<source>/YYYY/MM/DD.jsonl   — append-only raw events, one JSON per line
index/events.db                 — SQLite unified index
state/cursors.json              — last-seen IDs/timestamps per source
derived/                        — machine-generated rollups (agent-readable, do not write)
```

## Cadences

LaunchAgents use `StartCalendarInterval` (not `StartInterval`) — they fire on a
fixed clock every 30 min inside the work window, not on a rolling timer. See
`launchagents/*.plist`, installed by `bin/install-agents.sh`.

| Agent | Schedule (IST) | Idle guard |
|-------|----------------|------------|
| `slack-ingest`      | every 30 min, 12:00–22:30 | **none** — ingests every fire (volume justifies it) |
| `github-ingest`     | every 30 min, 12:00–22:30 | gated → one success/day (`last_github_success.date`) |
| `jira-ingest`       | every 30 min, 12:00–22:30 | gated → one success/day |
| `confluence-ingest` | every 30 min, 12:05–22:35 | gated → one success/day |
| `leaves`            | daily 04:00 | — |
| `slack-discover`    | Wed + Fri 13:00 | — |
| `housekeeping-review` | weekly Mon (routine) | gated → one success/week (prune + classify→#rollup) |

Idle-gated agents still fire every 30 min but exit early once the day's success
file is stamped — so each source lands at most one full ingest per day.

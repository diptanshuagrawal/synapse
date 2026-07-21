# events.db — schema reference

Reference for sessions that query the context engine directly (e.g. `/manager`).
Read-only consumers ONLY — all writes go through the ingest/derive pipeline.
Values below use generic placeholders; real identifiers live in the DB and configs.

## Location & open pattern

Canonical DB: `work-context/index/events.db` (WAL mode). Other `events.db` paths in the
repo are 0-byte stubs — do not use them.

Always open read-only with a busy timeout (concurrent ingest writers exist):

```bash
sqlite3 "file:work-context/index/events.db?mode=ro" "PRAGMA busy_timeout=30000; <query>"
```

```python
con = sqlite3.connect("file:work-context/index/events.db?mode=ro", uri=True)
con.execute("PRAGMA busy_timeout=30000")
```

## Core tables

### events — central fact table (~225k rows)
`id` PK · `source` · `event_type` · `ts` (ISO) · `actor` · `subject` · `title` · `body`
· `url` · `raw_path` · plus source-specific columns: `issue_type`, `story_points`,
`sprint_id/name/state`, `assignee`, `to_status` (Jira transitions), `channel_id`,
`thread_ts`, `edited_ts`, `deleted_ts` (tombstone — filter `deleted_ts IS NULL`),
`reactions_json`, `reply_count`, `files_json`.
Indexed: `(ts)`, `(actor,ts)`, `(source,ts)`, `(channel_id,ts)`, `(thread_ts)`,
`(subject,event_type)`, partial `(subject,ts) WHERE to_status IS NOT NULL`.

`source` values (desc. volume): `slack`, `github`, `jira`, `confluence`, `service`, `meeting`.

`event_type` values (main): `thread_reply`, `thread_started`, `comment`, `commit_in_pr`,
`status_change`, `review`, `commit_pushed`, `issue_created`, `assignment`,
`page_updated`, `page_created`, `pr_merged`, `pr_merged_by`, `sprint_change`,
`pr_opened`, `story_points_change`, `pr_closed`, `service_brief`,
`transcript_segment`, `meeting_recorded`.

### event_refs — event→entity edges (~158k rows)
`event_id` · `ref_type` · `ref_value` · `role`; PK all three.
`ref_type`: `project`, `person`, `ticket`, `slack_thread`, `page`, `pull_request`.
Indexed `(ref_type, ref_value)` — cheap reverse lookups.

### events_fts — FTS5 over `title`+`body`
External-content table mirroring `events` (`content_rowid = rowid`).

### embedding (~43k rows)
`subject` PK · `source` · `vector` BLOB · `model` · `dim` · `content_sha` · `computed_at`.
Vectors are raw little-endian float32, `dim=1536`, model `text-embedding-3-small`.
Decode in bulk: `np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(N, dim)`,
then L2-normalize so cosine = dot. See `derive/embedding_query.py`.

### topic_brief / topic_brief_member — topic clusters (~460 clusters / ~5k members)
brief: `cluster_id` PK · `label` · `summary` · `status` (ACTIVE|RESOLVED|STALE|RECURRING)
· `decisions_json` · `blockers_json` · `participants_json` · `risk_areas_json`
· `outcomes_json` · `followups_json` · `member_count` · `first_ts` · `last_activity_ts`
· `confidence` · `root_cause`.
member: `(cluster_id, subject)` PK · `similarity` · `member_role`
(KEY_DECISION_THREAD|REFERENCE_DOC|RELATED_TICKET|PASSING_MENTION).
`cluster_project_map` links `cluster_id` → `projects.yaml` slug with confidence.

### subject_summary (~9k rows) — per-subject classification
`(subject, content_hash)` PK · `domains` · `summary` · `risk_flags` · `confidence`
· `owned_by_primary` · `co_owners_json` · `ownership_reasoning`.

### thread_summary (~64k rows) — deterministic Slack-thread rollup
`subject` PK · `channel_id/name/class` · `started_by_canonical` · `participants_json`
· `first_ts` · `last_ts` · `msg_count` · `reply_count` · `referenced_tickets/pages/prs/threads`
· `ops_pattern_match` (incident|drill|rca|year_end|NULL) · `digest`.

### PR tables
`pr_meta` (`subject` = `<org>/<repo>#N` PK): `state` (open|closed|merged), `additions`,
`deletions`, `files_changed`, `is_draft`, `labels_json`, `checks_status`,
`checks_failed_json`, `created_at`, `merged_at`, `updated_at`.
`pr_comment_class` (`event_id` PK): review-comment root-cause category, `source` human|bot.
`pr_friction` (`subject` PK): friction `score` 0-100, `dominant_category`.

### Other
`identity_signals` (~38k) — actor-identity co-occurrence pairs for reconciliation.
`team_leaves` / `team_leaves_processed` — leave records from the leaves pipeline.
`trd_owners` — TRD page ownership. `feature_release`, `feature_stage` — feature narrative.
`thread_enriched`, `person_narrative` — schemas exist, currently unpopulated; check
row count before relying on them.
`work-context/state/doc_sync.db` — separate DB for doc-drift comments.

## Subject-format conventions

- slack: `slack:<channel_id>:<parent_ts>` — one subject per THREAD, shared by all replies
  (thread root = MIN(ts) within subject)
- jira: bare issue key, e.g. `PROJ-123`
- github PR: `<org>/<repo>#<n>` (matches `pr_meta.subject`); commit: `<org>/<repo>@<sha12>`
- confluence: `page:<numeric_id>`
- service: `service:<svc>#<facet>`
- meeting: `meeting:<YYYY-MM-DD>:<slug>-<HHMM>`

## Canonical query patterns

```sql
-- everything on a subject, chronological
SELECT ts, event_type, actor, title FROM events WHERE subject=? ORDER BY ts;

-- tickets referenced by a thread / events referencing a ticket
SELECT ref_value FROM event_refs r JOIN events e ON r.event_id=e.id
 WHERE e.subject=? AND r.ref_type='ticket';
SELECT event_id FROM event_refs WHERE ref_type='ticket' AND ref_value=?;

-- full-text search
SELECT e.* FROM events_fts f JOIN events e ON e.rowid=f.rowid
 WHERE events_fts MATCH ? ORDER BY rank LIMIT 50;

-- a person's recent activity (actor = canonical slug from people.yaml)
SELECT ts, source, event_type, subject, title FROM events
 WHERE actor=? AND ts>=? AND deleted_ts IS NULL ORDER BY ts DESC;

-- expand a topic cluster, then map to project slug
SELECT tb.label, tbm.subject, tbm.member_role
  FROM topic_brief tb JOIN topic_brief_member tbm USING(cluster_id)
 WHERE tb.cluster_id=?;

-- live PR state + friction
SELECT m.state, m.checks_status, f.score, f.dominant_category
  FROM pr_meta m LEFT JOIN pr_friction f USING(subject) WHERE m.subject=?;
```

Gotchas: filter Slack tombstones (`deleted_ts IS NULL`); Jira dev-credit goes to the
assignee during In Progress, not the reviewer (see `derive/jira_metrics.py` — consume
it rather than reimplementing Jira interpretation); working hours are 12:00–20:00 IST
for any after-hours computation.

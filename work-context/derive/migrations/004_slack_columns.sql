-- Migration 004: Slack ingest columns on events table.
--
-- Adds five additive columns to support Slack message ingestion.
-- Existing rows (github/jira/confluence) get NULL for all five — safe.
--
-- See prd/slack-ingest.md §7 "Schema delta".
-- Helper: work-context/derive/slack_upsert.py owns the UPSERT logic.
--
-- Columns:
--   channel_id      Slack channel ID (C0…). NULL for non-slack rows.
--                   Always set together with subject for slack rows.
--   thread_ts       Parent message ts (Slack float-seconds string) for replies.
--                   NULL for top-level messages and non-slack rows.
--   edited_ts       ISO ts of latest edit (when slack message edited after first
--                   ingest). NULL if never edited.
--   deleted_ts      ISO ts when we noticed Slack hard-delete. Tombstone — row is
--                   preserved with body intact; downstream queries filter
--                   deleted_ts IS NOT NULL to exclude.
--   reactions_json  {":+1:": 5, ":eyes:": 2} or NULL. Overwritten on each fetch
--                   (most recent state wins). Used for thread-importance signal.
--
-- Indexes:
--   idx_events_channel_ts        — per-channel cursor advance + reconcile window
--   idx_events_thread_ts         — thread reply grouping
--   idx_events_deleted_ts_partial — fast tombstone filter

ALTER TABLE events ADD COLUMN channel_id     TEXT;
ALTER TABLE events ADD COLUMN thread_ts      TEXT;
ALTER TABLE events ADD COLUMN edited_ts      TEXT;
ALTER TABLE events ADD COLUMN deleted_ts     TEXT;
ALTER TABLE events ADD COLUMN reactions_json TEXT;

CREATE INDEX IF NOT EXISTS idx_events_channel_ts ON events(channel_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_thread_ts  ON events(thread_ts);
CREATE INDEX IF NOT EXISTS idx_events_deleted_ts_partial
    ON events(source, deleted_ts) WHERE source = 'slack' AND deleted_ts IS NOT NULL;

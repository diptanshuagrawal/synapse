-- Migration 005: thread_summary materialised view for Slack threads.
--
-- One row per Slack thread (= one subject = slack:<channel>:<parent_ts>).
-- Built by derive/build_thread_summary.py from events + event_refs.
-- Refreshed post-ingest (called from /slack-ingest skill after writes).
--
-- See prd/slack-ingest.md §7 (storage) — this is the per-thread fast-lookup
-- view that backs "what happened on this thread" queries used by /narrative,
-- /retro, future /story.
--
-- Late-add safety: on people.yaml change, re-run
--   .venv/bin/python derive/build_thread_summary.py --rebuild-all
-- to refresh started_by_canonical + participants_json snapshots.

CREATE TABLE IF NOT EXISTS thread_summary (
    subject              TEXT PRIMARY KEY,      -- 'slack:<channel>:<parent_ts>'
    channel_id           TEXT NOT NULL,
    channel_name         TEXT,                  -- denorm from slack_channel_meta cache
    channel_class        TEXT,                  -- denorm from slack_channels.yaml (team / oncall / alerts / etc.)
    started_by_canonical TEXT,                  -- people.yaml canonical of parent-msg author; NULL if unresolved
    participants_json    TEXT NOT NULL,         -- '["alice-example", ...]' canonicals; raw U-ids dropped on unresolved
    first_ts             TEXT NOT NULL,         -- ISO ts of thread parent
    last_ts              TEXT NOT NULL,         -- ISO ts of latest message in thread (excluding tombstones)
    msg_count            INTEGER NOT NULL,      -- parent + replies (excludes deleted_ts NOT NULL)
    reply_count          INTEGER NOT NULL,      -- msg_count - 1 (or 0)
    referenced_tickets   TEXT,                  -- json array, denormed from event_refs (every Jira key cited anywhere in thread)
    referenced_pages     TEXT,                  -- json array of page IDs (numeric, no prefix)
    referenced_prs       TEXT,                  -- json array of 'owner/repo#N'
    referenced_threads   TEXT,                  -- json array of other slack:<ch>:<ts> subjects cross-referenced
    ops_pattern_match    TEXT,                  -- 'incident' | 'drill' | 'rca' | 'year_end' | NULL (from OPS_PATTERNS regex on title/first body)
    digest               TEXT,                  -- 1-line LLM summary; lazy-populated at compaction (NULL pre-compaction)
    computed_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_thread_summary_channel_last ON thread_summary(channel_id, last_ts);
CREATE INDEX IF NOT EXISTS idx_thread_summary_started_by  ON thread_summary(started_by_canonical);
CREATE INDEX IF NOT EXISTS idx_thread_summary_ops          ON thread_summary(ops_pattern_match)
    WHERE ops_pattern_match IS NOT NULL;

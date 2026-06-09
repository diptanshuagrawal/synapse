-- 007_jira_to_status.sql — capture the new-status string on jira events.
--
-- Background
-- ----------
-- Before this migration, `status_change` events stored "status: From → To"
-- only in the title and left body empty. Downstream consumers (e.g.
-- /ask throughput compute, completion-rate proxies) had no indexed way
-- to determine current status, forcing brittle "Reviewed" / "DAM alert"
-- comment-text heuristics.
--
-- After this migration
-- --------------------
-- New `to_status` column on `events`:
--   - On status_change events:    item.toString from the Jira changelog
--   - On issue_created events:    f.status.name at creation time
--                                 (usually "Backlog" / "To Do")
--   - All other events:           NULL
--
-- The latest non-null to_status for a subject ORDER BY ts DESC LIMIT 1
-- gives the current status. Existing rows (pre-migration) stay NULL —
-- /ask falls back to the comment-text heuristic for them, and warns.

ALTER TABLE events ADD COLUMN to_status TEXT;

-- Optional: index for current-status lookups per subject.
CREATE INDEX IF NOT EXISTS idx_events_subject_to_status
    ON events(subject, ts)
    WHERE to_status IS NOT NULL;

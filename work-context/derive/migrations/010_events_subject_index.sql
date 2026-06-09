-- 010_events_subject_index.sql — index events by subject for content assembly.
--
-- Background
-- ----------
-- `derive/subject_content.get_content()` fetches a subject's embeddable text
-- with per-subject queries shaped `WHERE subject = ? AND event_type = ?`
-- (and `subject = ? AND source = ?`). The only pre-existing subject index was
-- the PARTIAL `idx_events_subject_to_status` (WHERE to_status IS NOT NULL),
-- which the planner cannot use for these content queries.
--
-- Result: every get_content() call ran `SCAN events` over all ~166k rows.
-- `refresh_embeddings.py status` (detect_delta) calls get_content once per
-- corpus subject — ~38k subjects × 2-4 full scans each = billions of row reads.
-- The status pre-flight took 15+ minutes and was effectively unusable.
--
-- After this migration
-- --------------------
-- Composite (subject, event_type) index. Query plan flips from
-- `SCAN events` to `SEARCH events USING INDEX idx_events_subject`.
-- The status pre-flight drops from 15+ min to ~25s on a 38k-subject corpus.
-- The leading `subject` column also serves the `subject = ? AND source = ?`
-- queries (seek on subject, filter the handful of matched rows in-memory).

CREATE INDEX IF NOT EXISTS idx_events_subject
    ON events(subject, event_type);

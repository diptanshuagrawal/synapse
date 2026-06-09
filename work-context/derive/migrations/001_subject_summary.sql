-- 001_subject_summary.sql
-- Cache for Claude-generated per-subject classifications (domains + summary).
-- Keyed by (subject, content_hash) so re-classify only fires when source changes.

CREATE TABLE IF NOT EXISTS subject_summary (
    subject       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    domains       TEXT NOT NULL,
    summary       TEXT NOT NULL,
    risk_flags    TEXT,
    confidence    REAL,
    source        TEXT NOT NULL,
    model         TEXT,
    classified_at TEXT NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    PRIMARY KEY (subject, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_subject_summary_subject ON subject_summary(subject);

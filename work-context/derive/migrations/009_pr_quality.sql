-- Migration 009 — PR quality scorer (post-merge friction analysis)
--
-- Canonical DDL reference. The applied copy lives in
-- ingest/common.py::_ensure_schema (no external migration runner).
--
-- Feeds the /pr-quality skill (PRD: prd/pr-quality-scorer.md). Three tables:
--
--   pr_meta           — PR-level facts that don't fit the append-only event
--                       log: diff size, CI status, labels, draft flag.
--                       Populated by ingest/github.py (Phase 1). Keyed by the
--                       canonical subject 'owner/repo#N'.
--   pr_comment_class  — one row per classified review/comment. Category is the
--                       root-cause taxonomy from config/pr_review_rules.md.
--                       Written by /pr-quality apply (Phase 4). Empty until then.
--   pr_friction       — computed friction score per PR (mechanical +
--                       category-weighted). Written by derive/github_metrics.py
--                       (Phase 3+). Empty until then.

CREATE TABLE IF NOT EXISTS pr_meta (
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
    fetched_at         TEXT                -- when this row was last refreshed
);
CREATE INDEX IF NOT EXISTS idx_pr_meta_repo  ON pr_meta(repo);
CREATE INDEX IF NOT EXISTS idx_pr_meta_state ON pr_meta(state);

CREATE TABLE IF NOT EXISTS pr_comment_class (
    event_id      TEXT PRIMARY KEY,   -- events.id of the review / comment
    subject       TEXT NOT NULL,      -- owner/repo#N
    source        TEXT NOT NULL,      -- human | matterai | claude
    category      TEXT NOT NULL,      -- business-logic | correctness | test-gap |
                                      -- design | security | naming | nit |
                                      -- question | praise
    confidence    REAL,
    classified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pr_comment_class_subject  ON pr_comment_class(subject);
CREATE INDEX IF NOT EXISTS idx_pr_comment_class_category ON pr_comment_class(category);

CREATE TABLE IF NOT EXISTS pr_friction (
    subject              TEXT PRIMARY KEY,  -- owner/repo#N
    score                REAL,              -- 0-100 composite friction score
    dominant_category    TEXT,              -- the friction reason driving the score
    mechanical_json      TEXT,              -- {review_rounds, changes_requested,
                                            --  rework_commits, ttm_hours, ...}
    category_counts_json TEXT,              -- {category: {human: N, matterai: N}}
    computed_at          TEXT
);

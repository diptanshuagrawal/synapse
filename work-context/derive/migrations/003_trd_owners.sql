-- Migration 003: trd_owners materialised view.
--
-- Populated by derive/build_trd_owners.py after every successful Confluence
-- ingest (hooked from ingest/run-confluence.sh). Read by /narrative and /retro
-- slash commands to assign DESIGNED / OWNS / CONTRIBUTED labels to TRD pages
-- without per-call inference.
--
-- Scoring (per (page, actor)):
--   page_created  → 10
--   page_updated  → 3 per update
--   comment       → 1 per comment
-- Top scorer = OWNER. Anyone with score ≥ 30% of owner = CONTRIBUTOR.
--
-- TRD detection: title regex (?i)(TRD|Tech Spec|Technical Design|Technical Redesign)
-- OR page_id listed in any config/projects.yaml::confluence_pages.

CREATE TABLE IF NOT EXISTS trd_owners (
    page_id            TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    owner              TEXT NOT NULL,        -- canonical handle (people.yaml)
    owner_score        REAL NOT NULL,
    scores_json        TEXT NOT NULL,        -- {canonical: score, ...} every scorer
    contributors_json  TEXT NOT NULL,        -- [canonical, ...] score >= 30% of owner
    project_slug       TEXT,                 -- from projects.yaml confluence_pages match (NULL if title-match only)
    last_event_ts      TEXT NOT NULL,        -- max ts across all events on this page
    total_events       INTEGER NOT NULL,
    computed_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trd_owners_owner ON trd_owners(owner);
CREATE INDEX IF NOT EXISTS idx_trd_owners_project ON trd_owners(project_slug);
CREATE INDEX IF NOT EXISTS idx_trd_owners_last_ts ON trd_owners(last_event_ts);

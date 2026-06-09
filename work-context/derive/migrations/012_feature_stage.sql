-- Migration 012 — feature lifecycle stages
--
-- Canonical DDL reference. The applied copy lives in
-- ingest/common.py::_ensure_schema (no external migration runner).
--
-- Feeds the feature-narrative + Feature Score work (PRD:
-- prd/feature-narrative-scorer.md). One table:
--
--   feature_stage — one row per (slug, stage) for the four lifecycle stages
--                   planning / trd / code_dev / rollout. entered_at is the
--                   first detected timestamp for that stage; detection_source
--                   records which signal fired; a missing stage is simply
--                   absent (the narrative renders "not detected"). Written by
--                   derive/feature_stages.py.

-- scope distinguishes the two units of "a feature": '' is the whole-slug domain
-- rollup; a non-empty scope is an anchor epic key (epic-bounded journey).

CREATE TABLE IF NOT EXISTS feature_stage (
    slug             TEXT NOT NULL,
    scope            TEXT NOT NULL DEFAULT '',  -- '' = domain rollup; else anchor epic key
    stage            TEXT NOT NULL,     -- planning | trd | code_dev | rollout
    entered_at       TEXT,              -- first detected ts for the stage
    detection_source TEXT,              -- jira_epic | confluence_declared | github | cmr_release | …
    confidence       TEXT,              -- high | medium | low
    artefact_count   INTEGER,           -- # distinct subjects backing the stage
    detail_json      TEXT,              -- stage-specific extras (counts, sample subjects)
    computed_at      TEXT,
    PRIMARY KEY (slug, scope, stage)
);
CREATE INDEX IF NOT EXISTS idx_feature_stage_slug ON feature_stage(slug);

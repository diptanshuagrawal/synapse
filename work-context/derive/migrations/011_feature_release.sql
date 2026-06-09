-- Migration 011 — feature release records (CMR → rollout signal)
--
-- Canonical DDL reference. The applied copy lives in
-- ingest/common.py::_ensure_schema (no external migration runner).
--
-- Feeds the feature-narrative + Feature Score work (PRD:
-- prd/feature-narrative-scorer.md). One table:
--
--   feature_release — one row per (CMR, slug). A CMR (issue_type='CMR') is the
--                     org's release record: structured body
--                     (Service / PR / Impacted Areas / Owner of release) plus a
--                     status lifecycle (Approval Requested → Change Approved →
--                     Released / Released with Emergency / Rolled Back /
--                     Cancelled). Written by derive/cmr_releases.py. A release
--                     touching N features yields N rows (one per slug);
--                     unattributed CMRs get a single slug='' row.
--
-- is_feature_release = 0 marks ops CMRs (DB changes, balance fixes, config)
-- that carry no PR link and no project ref — excluded from feature scoring.

CREATE TABLE IF NOT EXISTS feature_release (
    cmr_subject           TEXT NOT NULL,    -- EX-NNNN
    slug                  TEXT NOT NULL,    -- projects.yaml slug, or '' if unattributed
    linked_via            TEXT,             -- project_ref | impacted_areas | none
    service               TEXT,             -- parsed "Service:" field
    impacted_areas        TEXT,             -- parsed "Impacted Areas:" field
    pr_urls_json          TEXT,             -- JSON list of owner/repo#N parsed from body
    release_owner         TEXT,             -- parsed "Owner of release:" field
    created_at            TEXT,             -- CMR issue_created ts
    approval_requested_at TEXT,             -- first Approval-Requested transition ts
    approved_at           TEXT,             -- Change-Approved transition ts
    approved_by           TEXT,             -- human who commented "Approved" (not the bot)
    released_at           TEXT,             -- first Released-family transition ts
    outcome               TEXT,             -- released | emergency | rolled_back | cancelled | pending
    is_feature_release    INTEGER,          -- 0 | 1
    title                 TEXT,             -- CMR title
    url                   TEXT,             -- CMR url
    computed_at           TEXT,
    PRIMARY KEY (cmr_subject, slug)
);
CREATE INDEX IF NOT EXISTS idx_feature_release_slug    ON feature_release(slug);
CREATE INDEX IF NOT EXISTS idx_feature_release_outcome ON feature_release(outcome);
CREATE INDEX IF NOT EXISTS idx_feature_release_relts   ON feature_release(released_at);

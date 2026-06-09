-- 006_topic_brief_v2.sql — extend topic_brief with v2 enrichment fields.
--
-- Adds 5 JSON-stored fields for richer cluster summarisation:
--   outcomes      — what actually shipped vs deferred (distinct from decisions)
--   followups     — open TODOs at last activity (lighter than blockers)
--   risk_areas    — known unknowns / things that broke during the work
--   stakeholders  — non-contributor people who decide / are affected
--   artifacts     — links to TRD pages, demo recordings, dashboards
--
-- All fields are optional; chat may leave them as empty arrays `[]`.
-- Existing rows get NULL on first read — topic_brief_validate v2 will surface
-- the gap and the next finalize_refresh apply backfills.

ALTER TABLE topic_brief ADD COLUMN outcomes_json     TEXT;
ALTER TABLE topic_brief ADD COLUMN followups_json    TEXT;
ALTER TABLE topic_brief ADD COLUMN risk_areas_json   TEXT;
ALTER TABLE topic_brief ADD COLUMN stakeholders_json TEXT;
ALTER TABLE topic_brief ADD COLUMN artifacts_json    TEXT;

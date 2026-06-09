-- Migration 008 — cluster_project_map
--
-- Maps topic_brief clusters to projects.yaml slugs (many-to-many).
-- Populated by derive/link_clusters_to_projects.py after each
-- finalize_refresh apply. Consumed by:
--   - derive/ask_engine.py::clusters_by_project, projects_active_in_window
--   - derive/person_profile.py::project_footprint block
--   - .claude/commands/ask.md  (renders project-level voice)
--   - .claude/commands/retro.md (aggregates outcomes per project)
--
-- Source values:
--   'jira_epic'      — cluster has ≥1 member whose epic_key matches projects.yaml::jira_epics
--   'confluence_page'— cluster has ≥1 member whose page_id matches projects.yaml::confluence_pages
--   'keyword'        — cluster label/summary substring-hits projects.yaml::keywords
--   'manual'         — owner-asserted via verdict file (future)
--
-- One cluster may link to N projects; conflict resolved by `confidence`.

CREATE TABLE IF NOT EXISTS cluster_project_map (
  cluster_id    INTEGER NOT NULL,
  project_slug  TEXT    NOT NULL,
  confidence    REAL    NOT NULL,
  source        TEXT    NOT NULL,
  evidence_json TEXT,
  computed_at   TEXT    NOT NULL,
  PRIMARY KEY (cluster_id, project_slug)
);

CREATE INDEX IF NOT EXISTS idx_cpm_cluster ON cluster_project_map(cluster_id);
CREATE INDEX IF NOT EXISTS idx_cpm_project ON cluster_project_map(project_slug);
CREATE INDEX IF NOT EXISTS idx_cpm_confidence ON cluster_project_map(confidence DESC);

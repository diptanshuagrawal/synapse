-- 002_person_narrative.sql
-- Cached per-person engineering narrative. Key includes window_days so 7d
-- and 240d narratives coexist. content_hash captures the person's activity
-- shape so we re-narrate only when their activity changes.

CREATE TABLE IF NOT EXISTS person_narrative (
    actor         TEXT NOT NULL,
    window_days   INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    body          TEXT NOT NULL,
    source        TEXT NOT NULL,    -- 'claude-api', 'claude-session', 'fallback'
    model         TEXT,
    generated_at  TEXT NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    PRIMARY KEY (actor, window_days, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_person_narrative_actor ON person_narrative(actor, window_days);

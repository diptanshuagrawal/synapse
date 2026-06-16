"""
Shared event schema, SQLite writer, JSONL appender, cursor management,
and refs enrichment for work-context ingest scripts.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import yaml


# ---------------------------------------------------------------------------
# Atomic write helper — shared across ingest + derive layers
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write *content* to *path* atomically.

    Writes to a sibling tempfile, fsync, then os.replace. The replace is
    atomic on POSIX, so readers either see the old file or the new file —
    never a truncated one. Use this for any state file that another process
    may read concurrently (cursors, success-date markers, validation JSON).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup of the tempfile on failure.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, data, *, indent: int = 2, sort_keys: bool = False) -> None:
    """Convenience wrapper around atomic_write_text for JSON payloads."""
    atomic_write_text(path, json.dumps(data, indent=indent, sort_keys=sort_keys))

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "index" / "events.db"
STATE_PATH = ROOT / "state" / "cursors.json"
RAW_ROOT = ROOT / "raw"
CONFIG_DIR = ROOT / "config"


# ---------------------------------------------------------------------------
# Event dataclass
# ---------------------------------------------------------------------------

@dataclass
class Refs:
    people: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    tickets: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    pull_requests: list[str] = field(default_factory=list)  # 'owner/repo#N'
    slack_threads: list[str] = field(default_factory=list)  # 'slack:<channel>:<ts>'


@dataclass
class Event:
    id: str
    source: str
    event_type: str
    ts: str                    # ISO8601 UTC
    actor: Optional[str]
    subject: Optional[str]
    title: Optional[str]
    body: Optional[str]
    url: Optional[str]
    refs: Refs = field(default_factory=Refs)
    raw_path: str = ""         # filled in by append_raw()
    issue_type: Optional[str] = None  # jira: 'Epic' | 'Story' | 'Task' | 'Bug' | etc.; other sources: None
    story_points: Optional[float] = None   # jira: customfield_10051 scalar
    sprint_id: Optional[int] = None        # jira: customfield_10010[*].id — most recent
    sprint_name: Optional[str] = None      # jira: customfield_10010[*].name
    sprint_state: Optional[str] = None     # jira: 'active' | 'closed' | 'future'
    assignee: Optional[str] = None         # jira issue_created only: assignee email at creation (fallback when no assignment changelog event)
    to_status: Optional[str] = None        # jira: new status string. On status_change events
                                           # this is the toString from the changelog. On
                                           # issue_created this is the initial status name
                                           # (typically "Backlog" / "To Do"). Other events: None.


# ---------------------------------------------------------------------------
# SQLite connection + schema bootstrap
# ---------------------------------------------------------------------------

def get_db(path: Path = DB_PATH, timeout: float = 30.0) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")  # 30 seconds in milliseconds
    conn.execute("PRAGMA journal_mode = WAL")     # Use WAL for better concurrency
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id           TEXT PRIMARY KEY,
            source       TEXT NOT NULL,
            event_type   TEXT NOT NULL,
            ts           TEXT NOT NULL,
            actor        TEXT,
            subject      TEXT,
            title        TEXT,
            body         TEXT,
            url          TEXT,
            raw_path     TEXT NOT NULL,
            issue_type   TEXT,
            story_points REAL,
            sprint_id    INTEGER,
            sprint_name  TEXT,
            sprint_state TEXT,
            assignee     TEXT,
            to_status    TEXT             -- jira: new status name on status_change /
                                          -- initial status on issue_created. Other events: NULL.
        );
        CREATE INDEX IF NOT EXISTS idx_events_subject_to_status
            ON events(subject, ts)
            WHERE to_status IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_events_ts        ON events(ts);
        CREATE INDEX IF NOT EXISTS idx_events_actor_ts  ON events(actor, ts);
        CREATE INDEX IF NOT EXISTS idx_events_source_ts ON events(source, ts);

        CREATE TABLE IF NOT EXISTS event_refs (
            event_id  TEXT NOT NULL,
            ref_type  TEXT NOT NULL,
            ref_value TEXT NOT NULL,
            PRIMARY KEY (event_id, ref_type, ref_value)
        );
        CREATE INDEX IF NOT EXISTS idx_refs_value ON event_refs(ref_type, ref_value);

        CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
            title, body,
            content='events',
            content_rowid='rowid'
        );
    """)
    # Migration: add columns if events table predates them.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    if "issue_type" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN issue_type TEXT")
    if "story_points" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN story_points REAL")
    if "sprint_id" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN sprint_id INTEGER")
    if "sprint_name" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN sprint_name TEXT")
    if "sprint_state" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN sprint_state TEXT")
    if "assignee" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN assignee TEXT")
    # Migration 004 — Slack ingest columns (additive; existing rows get NULL).
    if "channel_id" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN channel_id TEXT")
    if "thread_ts" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN thread_ts TEXT")
    if "edited_ts" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN edited_ts TEXT")
    if "deleted_ts" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN deleted_ts TEXT")
    if "reactions_json" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN reactions_json TEXT")
    if "reply_count" not in cols:
        # Slack thread parent's `Thread: N replies` count, captured at ingest
        # time so /slack-ingest can identify which parents need a follow-up
        # slack_read_thread call without re-paging the whole channel.
        conn.execute("ALTER TABLE events ADD COLUMN reply_count INTEGER")
    if "drain_attempted_at" not in cols:
        # Slack thread parent: ISO-ts of last drain attempt via
        # slack_backfill_helper.drain-threads. Used to:
        #   (1) clamp reply_count drift caused by MCP thread pagination cap
        #       (~100 replies/page even with limit=1000 — long threads under-fetch),
        #   (2) cool down stale-threads detector so we don't re-drain same
        #       parents every fire when MCP can't return more.
        conn.execute("ALTER TABLE events ADD COLUMN drain_attempted_at TEXT")
    if "files_json" not in cols:
        # Slack only: structured file attachments captured at ingest time.
        # JSON list of {id, name, mimetype, size, mode, permalink, user}.
        # Body still carries `[files: name1, name2]` suffix for narrative
        # readability; this column enables file-aware queries (dedup, type
        # filters, permalink resolution) without re-parsing the suffix.
        conn.execute("ALTER TABLE events ADD COLUMN files_json TEXT")

    # Migration 005 — semantic-enrichment column on event_refs.
    # Lets enrich-2 (LLM classifier) annotate WHY a ref appears in an event:
    # ASKING_QUESTION | REQUESTING_ACTION | FIXING | BLOCKED_BY | DUPLICATE |
    # REFERENCING | PASSING_MENTION | UPDATE_ON | RESOLVED_BY. Nullable so all
    # legacy rows continue to work; consumers must treat NULL as "role unknown".
    refs_cols = {row[1] for row in conn.execute("PRAGMA table_info(event_refs)").fetchall()}
    if "role" not in refs_cols:
        conn.execute("ALTER TABLE event_refs ADD COLUMN role TEXT")

    # Migration 006 — enrichment tables (embeddings + LLM-derived semantic views).
    # These are derived state; safe to wipe + rebuild any time.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS embedding (
            subject     TEXT PRIMARY KEY,
            source      TEXT NOT NULL,        -- slack | jira | confluence | github
            vector      BLOB NOT NULL,        -- float32 array, dim = model output
            model       TEXT NOT NULL,        -- e.g. text-embedding-3-small
            dim         INTEGER NOT NULL,
            content_sha TEXT,                 -- hash of embedded content; lets us detect drift
            computed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_embedding_source ON embedding(source);

        CREATE TABLE IF NOT EXISTS thread_enriched (
            -- one row per slack thread (subject = slack:CH:ts). Multi-source
            -- informed: classifier sees thread + linked tickets/pages/PRs +
            -- top-k embedding neighbours when producing this row.
            subject               TEXT PRIMARY KEY,
            channel_id            TEXT,
            topic_paraphrase      TEXT,            -- one-line plain English summary
            sentiment             TEXT,            -- NEUTRAL|REQUEST|FRUSTRATION|CELEBRATION|URGENT
            urgency               INTEGER,         -- 0-3 ordinal
            intent                TEXT,            -- ASKING|ANNOUNCING|REQUESTING_ACTION|RESOLVING|DEBUGGING|REPORTING
            outcome               TEXT,            -- RESOLVED|DEFERRED|ESCALATED|UNRESOLVED|FYI|REQUEST_DONE
            outcome_summary       TEXT,
            decisions_json        TEXT,            -- [{text, made_by, evidence_phrase}]
            blockers_json         TEXT,            -- [{text, on_whom, ticket_ref, evidence_phrase}]
            participants_json     TEXT,            -- [{person, role}]
            implicit_refs_json    TEXT,            -- [{phrase, resolved_subject?, confidence}]
            cross_source_refs_json TEXT,           -- [{subject, source, ref_role, evidence_phrase, similarity}]
            reply_count_seen      INTEGER,         -- idempotency: skip if no new replies
            classifier_version    TEXT,
            computed_at           TEXT
        );

        CREATE TABLE IF NOT EXISTS topic_brief (
            -- one row per topic cluster (across all sources).
            cluster_id            INTEGER PRIMARY KEY AUTOINCREMENT,
            label                 TEXT,            -- LLM-named topic
            summary               TEXT,            -- 3-5 sentence "what is this"
            status                TEXT,            -- ACTIVE|RESOLVED|STALE|RECURRING
            root_cause            TEXT,            -- one-liner root cause for ACTIVE issues
            decisions_json        TEXT,            -- [{text, evidence_subject, evidence_phrase}]
            blockers_json         TEXT,
            participants_json     TEXT,            -- [{person, role, contribution_count}]
            source_breakdown_json TEXT,            -- {slack: N, jira: N, page: N, github: N}
            member_count          INTEGER,
            first_ts              TEXT,
            last_activity_ts      TEXT,
            classifier_version    TEXT,
            computed_at           TEXT,
            confidence            REAL             -- 0-1 from chat-labeling pass
        );

        CREATE TABLE IF NOT EXISTS topic_brief_member (
            cluster_id  INTEGER NOT NULL,
            subject     TEXT NOT NULL,
            source      TEXT NOT NULL,
            similarity  REAL,                       -- distance from cluster centroid, 0-1
            member_role TEXT,                       -- KEY_DECISION_THREAD|REFERENCE_DOC|RELATED_TICKET|PASSING_MENTION
            PRIMARY KEY (cluster_id, subject),
            FOREIGN KEY (cluster_id) REFERENCES topic_brief(cluster_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_topic_brief_member_subject ON topic_brief_member(subject);

        -- ── team_leaves ──────────────────────────────────────────────────
        -- Owner's direct-reports leave plans extracted from Slack mentions.
        -- Pipeline mirrors subject_summary: regex prefilter (derive/leaves_dump.py)
        -- → chat classify (.claude/commands/leaves.md) → apply (derive/apply_leaves.py)
        -- → render (derive/render_leaves.py → derived/team-leaves.md).
        --
        -- One source slack event can yield N leave rows (one event mentioning
        -- multiple people, or multiple disjoint date ranges). De-dup gate is
        -- team_leaves_processed below, keyed on the source event_id.
        CREATE TABLE IF NOT EXISTS team_leaves (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id      TEXT NOT NULL,             -- source slack events.id
            actor         TEXT NOT NULL,             -- canonical github handle
            mentioned_at  TEXT NOT NULL,             -- when message posted (ISO)
            date_start    TEXT,                      -- YYYY-MM-DD or NULL if ambiguous
            date_end      TEXT,                      -- YYYY-MM-DD or NULL if open/single-day
            reason        TEXT,                      -- short tag: wfh|vacation|sick|holiday|ooo|other
            channel_id    TEXT,
            channel_name  TEXT,
            body_excerpt  TEXT,                      -- ≤ 300 chars
            url           TEXT,                      -- permalink
            confidence    REAL,                      -- chat verdict 0..1
            extracted_by  TEXT,                      -- 'chat' (regex never inserts here directly)
            classified_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_team_leaves_event  ON team_leaves(event_id);
        CREATE INDEX IF NOT EXISTS idx_team_leaves_actor  ON team_leaves(actor);
        CREATE INDEX IF NOT EXISTS idx_team_leaves_dates  ON team_leaves(date_start, date_end);

        -- Dedup gate: an event_id appears here once classification ran,
        -- regardless of whether it was confirmed leave or rejected false-positive.
        -- leaves_dump.py uses NOT IN (SELECT event_id FROM ...) to skip these.
        CREATE TABLE IF NOT EXISTS team_leaves_processed (
            event_id      TEXT PRIMARY KEY,
            processed_at  TEXT NOT NULL,
            is_leave      INTEGER NOT NULL,          -- 1 = real leave, 0 = false positive
            confidence    REAL
        );

        -- ── slack_pending_reply_check ────────────────────────────────────
        -- Reply-walk backlog for team_involved-mode channels.
        --
        -- In team_involved mode a bot-rooted thread is only KEPT if a reply
        -- walk confirms a team member participated. That walk is budget-capped
        -- per fire (TEAM_REPLY_CHECK_CAP). On a high-noise bot channel
        -- (e.g. a release-notification room) the budget is consumed by earlier release-bot
        -- threads, so a later bot root gets DROPPED without ever walking its
        -- replies — and the channel cursor still advances past it, so no future
        -- fire re-examines it. A genuine team reply buried under that root
        -- (e.g. an owner CMR-approval ask) is then lost forever.
        --
        -- This queue decouples the reply-walk from the cursor: when a bot root
        -- with replies is starved of budget, we enqueue (channel_id, parent_ts)
        -- here instead of silently dropping it. A bounded drain pass on a later
        -- fire walks the queued parents' replies regardless of cursor position;
        -- team-involved threads get the root + replies upserted, the rest are
        -- dequeued. Mirrors the stale/active reconcile pattern, but for parents
        -- that were never stored in events at all.
        CREATE TABLE IF NOT EXISTS slack_pending_reply_check (
            channel_id   TEXT NOT NULL,
            parent_ts    TEXT NOT NULL,            -- Slack epoch ts of the (bot) root
            reply_count  INTEGER,                  -- declared reply_count at enqueue time
            first_seen   TEXT NOT NULL,            -- ISO ts first enqueued
            attempts     INTEGER NOT NULL DEFAULT 0,  -- drain attempts so far (retry/abandon ceiling)
            PRIMARY KEY (channel_id, parent_ts)
        );
        CREATE INDEX IF NOT EXISTS idx_pending_reply_check_chan
            ON slack_pending_reply_check(channel_id, parent_ts);
    """)

    # Indexes for Slack workloads.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_channel_ts ON events(channel_id, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_thread_ts ON events(thread_ts)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_deleted_ts_partial "
        "ON events(source, deleted_ts) WHERE source = 'slack' AND deleted_ts IS NOT NULL"
    )

    # Migration 009 — PR quality scorer (see derive/migrations/009_pr_quality.sql).
    # pr_meta is populated by ingest/github.py; pr_comment_class + pr_friction are
    # written later by the /pr-quality skill + derive/github_metrics.py and stay
    # empty until then. All additive — pre-existing DBs just gain empty tables.
    conn.executescript("""
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
            fetched_at         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pr_meta_repo  ON pr_meta(repo);
        CREATE INDEX IF NOT EXISTS idx_pr_meta_state ON pr_meta(state);

        CREATE TABLE IF NOT EXISTS pr_comment_class (
            event_id      TEXT PRIMARY KEY,   -- events.id of the review / comment
            subject       TEXT NOT NULL,      -- owner/repo#N
            source        TEXT NOT NULL,      -- human | matterai
            category      TEXT NOT NULL,      -- root-cause taxonomy (pr_review_rules.md)
            confidence    REAL,
            classified_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pr_comment_class_subject  ON pr_comment_class(subject);
        CREATE INDEX IF NOT EXISTS idx_pr_comment_class_category ON pr_comment_class(category);

        CREATE TABLE IF NOT EXISTS pr_friction (
            subject              TEXT PRIMARY KEY,  -- owner/repo#N
            score                REAL,              -- 0-100
            dominant_category    TEXT,
            mechanical_json      TEXT,
            category_counts_json TEXT,
            computed_at          TEXT
        );

        -- feature_release — CMR rollout records (migration 011).
        -- One row per (CMR, slug). Written by derive/cmr_releases.py; empty
        -- until then. Feeds the feature-narrative + Feature Score work
        -- (prd/feature-narrative-scorer.md).
        CREATE TABLE IF NOT EXISTS feature_release (
            cmr_subject           TEXT NOT NULL,    -- EX-NNNN
            slug                  TEXT NOT NULL,    -- projects.yaml slug, or ''
            linked_via            TEXT,             -- project_ref | impacted_areas | none
            service               TEXT,
            impacted_areas        TEXT,
            pr_urls_json          TEXT,             -- JSON list of owner/repo#N
            release_owner         TEXT,
            created_at            TEXT,             -- CMR issue_created ts
            approval_requested_at TEXT,
            approved_at           TEXT,
            approved_by           TEXT,
            released_at           TEXT,
            outcome               TEXT,             -- released|emergency|rolled_back|cancelled|pending
            is_feature_release    INTEGER,          -- 0 | 1
            title                 TEXT,
            url                   TEXT,
            computed_at           TEXT,
            PRIMARY KEY (cmr_subject, slug)
        );
        CREATE INDEX IF NOT EXISTS idx_feature_release_slug    ON feature_release(slug);
        CREATE INDEX IF NOT EXISTS idx_feature_release_outcome ON feature_release(outcome);
        CREATE INDEX IF NOT EXISTS idx_feature_release_relts   ON feature_release(released_at);

        -- feature_stage — feature lifecycle stages (migration 012).
        -- One row per (slug, stage). Written by derive/feature_stages.py.
        CREATE TABLE IF NOT EXISTS feature_stage (
            slug             TEXT NOT NULL,
            scope            TEXT NOT NULL DEFAULT '',  -- '' = domain rollup; else the anchor epic key
            stage            TEXT NOT NULL,   -- planning | trd | code_dev | rollout
            entered_at       TEXT,
            detection_source TEXT,
            confidence       TEXT,            -- high | medium | low
            artefact_count   INTEGER,
            detail_json      TEXT,
            computed_at      TEXT,
            PRIMARY KEY (slug, scope, stage)
        );
        CREATE INDEX IF NOT EXISTS idx_feature_stage_slug ON feature_stage(slug);
    """)

    # ── Column-level backfills for pre-existing DBs ─────────────────────────
    # CREATE TABLE IF NOT EXISTS is a no-op when the table already exists, so
    # columns added to the canonical schema after initial deployment must be
    # ALTERed in. Historically this drift was repaired ad-hoc inside
    # `derive/label_clusters._ensure_tables`, which meant the canonical
    # ingest schema lied about reality. Doing it here unifies the source of
    # truth and eliminates the order-of-execution hazard where the derive
    # script ran before the ingest schema was up-to-date.
    _add_column_if_missing(conn, "topic_brief", "root_cause", "TEXT")
    _add_column_if_missing(conn, "topic_brief", "confidence", "REAL")

    conn.commit()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    """ALTER TABLE ADD COLUMN if `col` not already present.

    SQLite has no IF NOT EXISTS for ADD COLUMN, so we probe `PRAGMA
    table_info` first. Cheap; runs once per connection bootstrap.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


# ---------------------------------------------------------------------------
# JSONL append + raw_path tracking
# ---------------------------------------------------------------------------

def append_raw(event: Event, dry_run: bool = False) -> str:
    """Write event JSON to raw/<source>/YYYY/MM/DD.jsonl. Returns raw_path.

    Count-then-append is protected by an advisory ``fcntl.flock`` on the
    target day-file so concurrent ingest fires can't race on ``line_num``
    (CRITICAL bug if two writers see the same count and stamp identical
    ``raw_path`` values).
    """
    ts = datetime.fromisoformat(event.ts.replace("Z", "+00:00"))
    day_path = RAW_ROOT / event.source / ts.strftime("%Y/%m/%d.jsonl")

    if dry_run:
        # Even in dry-run we compute a representative raw_path. Skip locking
        # because no writes happen — the line_num is approximate by design.
        line_num = 1
        if day_path.exists():
            with open(day_path, "rb") as f:
                line_num = sum(1 for _ in f) + 1
        event.raw_path = f"raw/{event.source}/{ts.strftime('%Y/%m/%d')}.jsonl#{line_num}"
        return event.raw_path

    day_path.parent.mkdir(parents=True, exist_ok=True)
    # Open with O_APPEND + O_CREAT so the file is auto-created and all writes
    # are atomic-at-the-kernel append (POSIX guarantees no interleaving for a
    # single write() <= PIPE_BUF, but JSON lines can be larger — flock makes
    # this safe regardless of payload size).
    fd = os.open(day_path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Count lines while holding the lock so no other writer can race.
        try:
            with os.fdopen(os.dup(fd), "rb") as rf:
                rf.seek(0)
                line_num = sum(1 for _ in rf) + 1
        except Exception:
            line_num = 1

        raw_path = f"raw/{event.source}/{ts.strftime('%Y/%m/%d')}.jsonl#{line_num}"
        event.raw_path = raw_path

        record = asdict(event)
        payload = (json.dumps(record) + "\n").encode("utf-8")
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    return raw_path


# ---------------------------------------------------------------------------
# SQLite insert with dedup (idempotent on event.id)
# ---------------------------------------------------------------------------

def insert_event(conn: sqlite3.Connection, event: Event, dry_run: bool = False) -> bool:
    """Insert event into SQLite. Returns True if inserted, False if duplicate."""
    if dry_run:
        return True

    try:
        conn.execute(
            """
            INSERT INTO events (id, source, event_type, ts, actor, subject, title, body, url, raw_path,
                                issue_type, story_points, sprint_id, sprint_name, sprint_state, assignee,
                                to_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id, event.source, event.event_type, event.ts,
                event.actor, event.subject, event.title, event.body,
                event.url, event.raw_path,
                event.issue_type, event.story_points, event.sprint_id,
                event.sprint_name, event.sprint_state, event.assignee,
                event.to_status,
            ),
        )

        ref_rows = (
            [(event.id, "person",       v) for v in event.refs.people]
            + [(event.id, "project",      v) for v in event.refs.projects]
            + [(event.id, "ticket",       v) for v in event.refs.tickets]
            + [(event.id, "page",         v) for v in event.refs.pages]
            + [(event.id, "pull_request", v) for v in event.refs.pull_requests]
            + [(event.id, "slack_thread", v) for v in event.refs.slack_threads]
        )
        if ref_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO event_refs (event_id, ref_type, ref_value) VALUES (?, ?, ?)",
                ref_rows,
            )

        # Keep FTS index in sync
        conn.execute(
            "INSERT INTO events_fts(rowid, title, body) "
            "SELECT rowid, title, body FROM events WHERE id = ?",
            (event.id,),
        )

        conn.commit()
        return True

    except sqlite3.IntegrityError:
        return False  # duplicate


def delete_events(
    conn: sqlite3.Connection,
    event_ids,
    *,
    commit: bool = True,
) -> int:
    """Hard-delete events by id, cascading to event_refs + events_fts. Returns
    the number of `events` rows removed.

    The single delete counterpart to insert_event(). insert_event is the only
    writer of the (events, event_refs, events_fts) trio; this is the only
    deleter, so the three can never drift. A bare ``DELETE FROM events`` that
    forgot the paired event_refs delete is exactly what leaked 16 orphan refs
    behind a re-ingested Slack thread — route every hard delete through here so
    that leak (and stale full-text postings) can't recur.

    Order matters: the events_fts row is an external-content FTS5 index keyed on
    the events rowid, so its `'delete'` command must run *before* the parent
    row vanishes (it reads rowid/title/body from `events`). For rows whose body
    drifted from what was indexed (Slack edits never re-sync FTS incrementally),
    the periodic ``events_fts('rebuild')`` heals any residue.

    Idempotent and chunked (SQLite caps host parameters per statement). Pass
    ``commit=False`` when the caller drives its own ``with conn:`` transaction.
    """
    ids = [e for e in dict.fromkeys(event_ids) if e]  # dedup, drop falsy, keep order
    if not ids:
        return 0

    BATCH = 500
    deleted = 0
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        ph = ",".join("?" * len(chunk))
        # 1. Drop full-text postings while the parent rows still exist.
        conn.execute(
            f"INSERT INTO events_fts(events_fts, rowid, title, body) "
            f"SELECT 'delete', rowid, title, body FROM events WHERE id IN ({ph})",
            chunk,
        )
        # 2. Refs — same id set as the events delete below, so nothing orphans.
        conn.execute(f"DELETE FROM event_refs WHERE event_id IN ({ph})", chunk)
        # 3. The events rows themselves.
        cur = conn.execute(f"DELETE FROM events WHERE id IN ({ph})", chunk)
        deleted += cur.rowcount

    if commit:
        conn.commit()
    return deleted


# ---------------------------------------------------------------------------
# Cursor management
# ---------------------------------------------------------------------------

def read_cursor(source: str) -> Optional[str]:
    """Return last cursor for source, or None."""
    if not STATE_PATH.exists():
        return None
    with open(STATE_PATH) as f:
        return json.load(f).get(source)


def write_cursor(source: str, value: str) -> None:
    """Persist cursor for source.

    Uses ``atomic_write_json`` so concurrent readers never see a half-written
    file (e.g. cron-status reading mid-write). Cursors file is read-modify-
    write under an exclusive lock on the file itself so two ingest fires
    can't lose each other's updates.
    """
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".lock")
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            cursors: dict = {}
            if STATE_PATH.exists():
                with open(STATE_PATH) as f:
                    cursors = json.load(f)
            cursors[source] = value
            atomic_write_json(STATE_PATH, cursors)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def write_success_date(source: str) -> None:
    """Write today's date to state/last_{source}_success.date.

    Called by ingest scripts on clean exit so that both manual runs and
    cron-launched runs update the cron-status gate file.
    """
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    date_file = STATE_PATH.parent / f"last_{source}_success.date"
    today = datetime.now().strftime("%Y-%m-%d")  # local time — matches shell gate
    atomic_write_text(date_file, today)


# ---------------------------------------------------------------------------
# Refs enrichment
# ---------------------------------------------------------------------------

_people_config: Optional[list[dict]] = None
_projects_config: Optional[list[dict]] = None

TICKET_RE = re.compile(r"\b([A-Z]{2,10}-\d+)\b")
CONFLUENCE_PAGE_RE = re.compile(r"/pages/(\d{8,12})\b")
# Matches title-prefix written by ingest/jira.py::_prefix_epic for jira issues
# in a tracked epic (e.g. "[Epic EX-2238] withholding Handling…"). Source-of-truth
# duplicated from derive/rollup.py + derive/llm_classifier.py — keep in sync.
EPIC_PREFIX_RE = re.compile(r"\[Epic ([A-Z]+-\d+)\]")
# GitHub PR / issue URL: https://github.com/owner/repo/pull/123 → store as 'owner/repo#123'
PR_URL_RE = re.compile(
    r"github\.com/([\w.\-]+/[\w.\-]+)/(?:pull|issues)/(\d+)\b",
    re.IGNORECASE,
)
# Shorthand 'owner/repo#123' when explicitly written in text.
PR_SHORTHAND_RE = re.compile(r"\b([\w.\-]+/[\w.\-]+)#(\d+)\b")
# Slack permalink: https://<workspace>.slack.com/archives/<C…>/p<ts-no-dot>
# Slack ts is always 10-digit unix-secs + 6-digit microsecs = exactly 16 digits.
# Strict match prevents false positives from truncated text (title vs body).
SLACK_THREAD_URL_RE = re.compile(
    r"slack\.com/archives/([CGD][A-Z0-9]+)/p(\d{16})\b",
    re.IGNORECASE,
)
# Slack mention <@U0EXAMPLE> or <@U0EXAMPLE|name> — captures U-id only.
SLACK_MENTION_RE = re.compile(r"<@([UB][A-Z0-9]+)(?:\|[^>]*)?>")


def _load_people() -> list[dict]:
    global _people_config
    if _people_config is None:
        with open(CONFIG_DIR / "people.yaml") as f:
            _people_config = yaml.safe_load(f).get("people", [])
    return _people_config


def _load_projects() -> list[dict]:
    global _projects_config
    if _projects_config is None:
        with open(CONFIG_DIR / "projects.yaml") as f:
            _projects_config = yaml.safe_load(f).get("projects", [])
    return _projects_config


def _resolve_person(handle: str, field: str) -> Optional[str]:
    """Return canonical key for a handle looked up by field name.

    Stubs restored from Atlassian/Confluence externals carry jira_id only
    (no canonical). Treat them as known-but-unmapped → return None so the
    downstream ref is skipped instead of raising KeyError mid-fire.
    """
    for p in _load_people():
        if p.get(field, "").lower() == handle.lower():
            return p.get("canonical")
    return None


def enrich_refs(
    event: Event,
    actor_field: str = "github",
    extra_handles: Optional[list[tuple[str, str]]] = None,
    slack_users_cache: Optional[dict[str, str]] = None,
) -> None:
    """
    Populate event.refs in-place.
    - actor_field: which people.yaml field to resolve event.actor against
    - extra_handles: list of (handle, field) pairs for additional people
    - slack_users_cache: optional {U-id: canonical} map for resolving <@U…> mentions.
      Pass when ingesting Slack content; pre-warmed from state/slack_users_cache.json.
      Unknown U-ids → person ref skipped (resolve later via backfill utility).

    Ref types populated (also see schema_init.sql for full vocabulary):
      person         canonical from people.yaml
      project        slug from projects.yaml
      ticket         Jira key, e.g. 'EX-2629'
      page           Confluence numeric page id, e.g. 'EXAMPLE_PAGE_ID'
      pull_request   github canonical 'owner/repo#N' (NEW)
      slack_thread   subject form 'slack:<channel>:<ts>' (NEW)
    """
    text = f"{event.title or ''} {event.body or ''}"
    people_set: set[str] = set()
    projects_set: set[str] = set()
    tickets_set: set[str] = set()
    pages_set: set[str] = set()
    prs_set: set[str] = set()
    slack_threads_set: set[str] = set()

    # Resolve actor
    if event.actor:
        canonical = _resolve_person(event.actor, actor_field)
        if canonical:
            people_set.add(canonical)

    # Resolve extra handles (e.g. PR author, reviewer)
    for handle, f in (extra_handles or []):
        canonical = _resolve_person(handle, f)
        if canonical:
            people_set.add(canonical)

    # Resolve Slack <@U…> mentions via cache.
    if slack_users_cache:
        for m in SLACK_MENTION_RE.finditer(text):
            uid = m.group(1)
            canonical = slack_users_cache.get(uid)
            if canonical:
                people_set.add(canonical)
            # Unknown U-ids: no person ref. backfill utility re-runs once cache
            # warms (people.yaml updated with the slack_id, or U-id observed
            # in a later /slack-discover refresh).

    # Extract Jira ticket references
    for m in TICKET_RE.finditer(text):
        tickets_set.add(m.group(1))

    # Extract Confluence page IDs
    for m in CONFLUENCE_PAGE_RE.finditer(text):
        pages_set.add(m.group(1))

    # Extract GitHub PR / issue URLs → canonical 'owner/repo#N'.
    for m in PR_URL_RE.finditer(text):
        prs_set.add(f"{m.group(1)}#{m.group(2)}")
    # Also accept owner/repo#N shorthand inline.
    from derive.sources_config import github_handle_prefixes
    _gh_prefixes = github_handle_prefixes()
    for m in PR_SHORTHAND_RE.finditer(text):
        # Filter to known org owner prefixes (from config) to avoid noise.
        owner = m.group(1).split("/")[0]
        if any(owner.startswith(p) for p in _gh_prefixes):
            prs_set.add(f"{m.group(1)}#{m.group(2)}")

    # Extract Slack thread permalinks → canonical 'slack:<channel>:<ts>'.
    # Regex enforces exactly 16-digit ts (10-sec + 6-microsec packed).
    for m in SLACK_THREAD_URL_RE.finditer(text):
        channel_id = m.group(1)
        ts_no_dot = m.group(2)
        ts = ts_no_dot[:10] + "." + ts_no_dot[10:]
        slack_threads_set.add(f"slack:{channel_id}:{ts}")

    # Extract event's own epic anchor from title prefix (only set on jira events
    # in tracked epics — github / confluence / slack titles won't carry this).
    em = EPIC_PREFIX_RE.search(text)
    event_epic_key = em.group(1) if em else ""

    # Match projects: precise signals only.
    # Removed jira_prefixes matching 2026-05-13 — was over-tagging (35+ projects
    # share `EX` prefix, every EX event got all 35 as refs). See slack-ingest
    # PRD §18 entry. Use jira_epics for precise per-project anchor.
    text_lower = text.lower()
    for proj in _load_projects():
        matched = False
        # 1. Keyword in title/body (precise — projects.yaml keywords are scoped).
        for kw in proj.get("keywords", []):
            if kw.lower() in text_lower:
                projects_set.add(proj["slug"])
                matched = True
                break
        if matched:
            continue
        # 2. Event's own epic in project's jira_epics list (precise per-project anchor).
        if event_epic_key and event_epic_key in (proj.get("jira_epics") or []):
            projects_set.add(proj["slug"])
            continue
        # 3. Confluence page ID listed in project's confluence_pages.
        for page_id in proj.get("confluence_pages", []):
            if str(page_id) in pages_set:
                projects_set.add(proj["slug"])
                break

    event.refs = Refs(
        people=sorted(people_set),
        projects=sorted(projects_set),
        tickets=sorted(tickets_set),
        pages=sorted(pages_set),
        pull_requests=sorted(prs_set),
        slack_threads=sorted(slack_threads_set),
    )


# ---------------------------------------------------------------------------
# Convenience: write + insert in one call
# ---------------------------------------------------------------------------

def store_event(conn: sqlite3.Connection, event: Event, dry_run: bool = False) -> bool:
    """Append to JSONL and insert into SQLite. Returns True if new."""
    append_raw(event, dry_run=dry_run)
    return insert_event(conn, event, dry_run=dry_run)

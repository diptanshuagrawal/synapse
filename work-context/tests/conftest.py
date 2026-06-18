"""Shared pytest fixtures for the ingest / derive test suite.

Design rules
------------
- **Never touch the real events.db, raw/ tree, or state/ dir.** Every fixture
  that needs persistence redirects ``ingest.common`` module globals
  (``DB_PATH`` / ``RAW_ROOT`` / ``STATE_PATH``) at ``tmp_path`` for the test.
- **No network, no live config.** Tests that exercise ``enrich_refs`` inject a
  tiny synthetic people.yaml / projects.yaml via the ``patch_config`` fixture
  rather than reading ``config/*.yaml`` — so a roster edit can't break a test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make `import ingest.common` / `import derive.*` resolve regardless of CWD.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ingest import common  # noqa: E402


# ---------------------------------------------------------------------------
# Filesystem isolation — redirect every persistent path at tmp_path.
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_paths(tmp_path, monkeypatch):
    """Point common.DB_PATH / RAW_ROOT / STATE_PATH at a throwaway tree.

    Returns a small namespace with the three paths so tests can assert on the
    raw JSONL / cursor files directly.
    """
    db_path = tmp_path / "index" / "events.db"
    raw_root = tmp_path / "raw"
    state_path = tmp_path / "state" / "cursors.json"

    monkeypatch.setattr(common, "DB_PATH", db_path)
    monkeypatch.setattr(common, "RAW_ROOT", raw_root)
    monkeypatch.setattr(common, "STATE_PATH", state_path)

    class _Paths:
        pass

    p = _Paths()
    p.root = tmp_path
    p.db_path = db_path
    p.raw_root = raw_root
    p.state_path = state_path
    return p


@pytest.fixture
def db_conn(tmp_paths):
    """A freshly-bootstrapped events.db on the temp tree (full real schema)."""
    conn = common.get_db(tmp_paths.db_path)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Event factory — minimal valid Event, override any field via kwargs.
# ---------------------------------------------------------------------------

@pytest.fixture
def make_event():
    _counter = {"n": 0}

    def _make(**overrides):
        _counter["n"] += 1
        n = _counter["n"]
        defaults = dict(
            id=f"evt-{n}",
            source="github",
            event_type="pr_opened",
            ts="2026-06-10T12:00:00Z",
            actor="alice",
            subject="org/repo#1",
            title="a title",
            body="a body",
            url="https://example.com/1",
        )
        defaults.update(overrides)
        return common.Event(**defaults)

    return _make


# ---------------------------------------------------------------------------
# Config injection — synthetic people.yaml / projects.yaml + handle prefixes.
# ---------------------------------------------------------------------------

@pytest.fixture
def patch_config(monkeypatch):
    """Inject deterministic people/projects config into ingest.common.

    People keyed by the same field shapes enrich_refs resolves against
    (github, email, slack_id). Projects exercise keyword / jira_epics /
    confluence_pages matching. github handle-prefix filter set to ('org/',).
    """
    people = [
        {"canonical": "alice", "github": "alice-gh", "email": "alice@example.com",
         "jira_id": "acc-alice", "slack_id": "U0ALICE"},
        {"canonical": "bob", "github": "bob-gh", "email": "bob@example.com",
         "jira_id": "acc-bob"},
        {"canonical": "carol", "slack_id": "U0CAROL"},
    ]
    projects = [
        {"slug": "payments", "keywords": ["withholding", "payout"],
         "jira_epics": ["EX-2238"], "confluence_pages": ["123456789"]},
        {"slug": "ledger", "keywords": ["ledger"], "jira_epics": [],
         "confluence_pages": []},
    ]
    monkeypatch.setattr(common, "_people_config", people, raising=False)
    monkeypatch.setattr(common, "_projects_config", projects, raising=False)

    # enrich_refs does `from derive.sources_config import github_handle_prefixes`
    # at call time, so patch it on the source module.
    import derive.sources_config as sc
    monkeypatch.setattr(sc, "github_handle_prefixes", lambda: ["org"])

    return {"people": people, "projects": projects}


# ---------------------------------------------------------------------------
# Seeded DB — a realistic multi-source events.db for the DB-driven derive
# tests (rollup, validators, ask_engine, github_metrics, …). One small but
# representative dataset so a test can assert on real derivation output without
# standing up its own fixtures.
# ---------------------------------------------------------------------------

# Richer roster than patch_config — every id shape populated for both people.
SEED_PEOPLE = [
    {"canonical": "alice", "github": "alice-gh", "email": "alice@example.com",
     "jira_id": "acc-alice", "slack_id": "U0ALICE", "scope": "team"},
    {"canonical": "bob", "github": "bob-gh", "email": "bob@example.com",
     "jira_id": "acc-bob", "slack_id": "U0BOB", "scope": "team"},
]
SEED_PROJECTS = [
    {"slug": "payments", "name": "Payments", "keywords": ["withholding", "payout"],
     "jira_epics": ["EX-2238"], "confluence_pages": ["123456789"]},
    {"slug": "ledger", "name": "Ledger", "keywords": ["ledger"], "jira_epics": [],
     "confluence_pages": []},
]

# Canonical subjects the seed creates — referenced by tests.
SEED_SUBJECTS = {
    "epic": "EX-2238",
    "story": "EX-2301",
    "pr": "org/repo#10",
    "page": "page:123456789",
    "thread": "slack:C0A:1700000000.000100",
}


def _seed_subject_summary_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subject_summary (
            subject TEXT NOT NULL, content_hash TEXT NOT NULL,
            domains TEXT NOT NULL, summary TEXT NOT NULL,
            risk_flags TEXT, confidence REAL, source TEXT NOT NULL,
            model TEXT, classified_at TEXT NOT NULL,
            input_tokens INTEGER, output_tokens INTEGER, detail TEXT,
            owned_by_primary TEXT, co_owners_json TEXT,
            owned_by_confidence REAL, ownership_reasoning TEXT,
            PRIMARY KEY (subject, content_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_subject_summary_subject ON subject_summary(subject);
    """)


def build_seed(conn):
    """Populate `conn` (a full-schema events.db) with a multi-source dataset.

    Shapes covered: a Jira epic + story (created → In Progress → Done, with an
    assignment), a GitHub PR (opened → merged + an approving review), a
    Confluence page, and a Slack thread (parent + reply). Plus subject_summary
    domain rows, a pr_meta row, and one incident topic cluster.
    """
    import json as _json
    from ingest.common import Event, insert_event, enrich_refs

    # Slack U-id → canonical, mirroring the real ingest's users cache.
    _slack_cache = {p["slack_id"]: p["canonical"] for p in SEED_PEOPLE if p.get("slack_id")}
    _actor_field = {"jira": "email", "github": "github", "confluence": "jira_id"}

    def ev(**kw):
        kw.setdefault("title", "")
        kw.setdefault("body", "")
        kw.setdefault("url", "")
        e = Event(**kw)
        # Enrich refs the way each source's normalizer would, so event_refs +
        # project tagging are populated for the DB-driven tests.
        if e.source == "slack":
            enrich_refs(e, slack_users_cache=_slack_cache)
        else:
            enrich_refs(e, actor_field=_actor_field.get(e.source, "github"))
        insert_event(conn, e)

    # ── Jira: epic + story lifecycle ─────────────────────────────────────
    ev(id="jira:EX-2238:created", source="jira", event_type="issue_created",
       ts="2026-06-01T09:00:00Z", actor="alice@example.com", subject="EX-2238",
       title="Withholding revamp", issue_type="Epic", to_status="To Do",
       assignee="alice@example.com")
    ev(id="jira:EX-2301:created", source="jira", event_type="issue_created",
       ts="2026-06-02T09:00:00Z", actor="alice@example.com", subject="EX-2301",
       title="[Epic EX-2238] payout calc", body="fix payout rounding",
       issue_type="Story", story_points=3.0, sprint_name="S1",
       sprint_state="active", to_status="To Do", assignee="alice@example.com")
    ev(id="jira:EX-2301:assignee:1:0", source="jira", event_type="assignment",
       ts="2026-06-02T10:00:00Z", actor="alice@example.com", subject="EX-2301",
       title="assignee: ∅ → Alice Example")
    ev(id="jira:EX-2301:status:2:0", source="jira", event_type="status_change",
       ts="2026-06-03T09:00:00Z", actor="alice@example.com", subject="EX-2301",
       title="[Epic EX-2238] status: To Do → In Progress", to_status="In Progress")
    ev(id="jira:EX-2301:status:3:0", source="jira", event_type="status_change",
       ts="2026-06-05T09:00:00Z", actor="alice@example.com", subject="EX-2301",
       title="[Epic EX-2238] status: In Progress → Done", to_status="Done")

    # ── GitHub: PR opened → merged + review ──────────────────────────────
    ev(id="github:org/repo:pr:10:pr_opened", source="github", event_type="pr_opened",
       ts="2026-06-02T12:00:00Z", actor="alice-gh", subject="org/repo#10",
       title="payout rounding fix", body="see EX-2301", url="https://github.com/org/repo/pull/10")
    ev(id="github:org/repo:pr:10:review:1", source="github", event_type="review",
       ts="2026-06-03T08:00:00Z", actor="bob-gh", subject="org/repo#10",
       title="Review on #10: APPROVED", body="lgtm")
    ev(id="github:org/repo:pr:10:pr_merged", source="github", event_type="pr_merged",
       ts="2026-06-04T08:00:00Z", actor="alice-gh", subject="org/repo#10",
       title="payout rounding fix", url="https://github.com/org/repo/pull/10")

    # ── Confluence page ──────────────────────────────────────────────────
    ev(id="confluence:page:123456789:v1", source="confluence", event_type="page_created",
       ts="2026-06-01T11:00:00Z", actor="acc-alice", subject="page:123456789",
       title="Ledger design", body="ledger reconciliation notes")

    # ── Slack thread parent + reply ──────────────────────────────────────
    ev(id="slack:C0A:1700000000.000100", source="slack", event_type="thread_started",
       ts="2026-06-03T07:00:00Z", actor="U0ALICE", subject="slack:C0A:1700000000.000100",
       title="prod payout outage", body="payout job is down")
    ev(id="slack:C0A:1700000000.000200", source="slack", event_type="thread_reply",
       ts="2026-06-03T07:05:00Z", actor="U0BOB", subject="slack:C0A:1700000000.000100",
       title="", body="fixed it, merged the rollback")

    # ── subject_summary (domain classification cache) ────────────────────
    _seed_subject_summary_schema(conn)
    now = "2026-06-05T00:00:00Z"
    for subject, domains, summary in [
        ("EX-2301", ["payments"], "payout rounding"),
        ("org/repo#10", ["payments"], "payout rounding fix"),
        ("page:123456789", ["ledger"], "ledger design notes"),
    ]:
        conn.execute(
            "INSERT INTO subject_summary (subject, content_hash, domains, summary, "
            "risk_flags, confidence, source, classified_at) VALUES (?,?,?,?,?,?,?,?)",
            (subject, "h-" + subject, _json.dumps(domains), summary,
             "[]", 0.9, "claude", now),
        )

    # ── pr_meta ──────────────────────────────────────────────────────────
    conn.execute(
        "INSERT INTO pr_meta (subject, repo, number, state, additions, deletions, "
        "files_changed, is_draft, created_at, merged_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("org/repo#10", "org/repo", 10, "merged", 40, 5, 3, 0,
         "2026-06-02T12:00:00Z", "2026-06-04T08:00:00Z"),
    )

    # ── topic_brief incident cluster + member (slack thread) ─────────────
    # v2 columns (migration 006) the validators read but base schema lacks.
    for col in ("outcomes_json", "followups_json", "risk_areas_json",
                "stakeholders_json", "artifacts_json"):
        common._add_column_if_missing(conn, "topic_brief", col, "TEXT")
    conn.execute("INSERT INTO topic_brief (cluster_id, label, root_cause, status) "
                 "VALUES (1, 'Payout outage', 'job crash', 'RESOLVED')")
    conn.execute("INSERT INTO topic_brief_member (cluster_id, subject, source) "
                 "VALUES (1, 'slack:C0A:1700000000.000100', 'slack')")
    conn.commit()


# ---------------------------------------------------------------------------
# Fake Anthropic client — lets classifier/narrative code run without network.
# Mirrors the surface llm_classifier._call_claude touches: a `.messages.create`
# that returns an object with `.content` (tool_use blocks) and `.usage`.
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_anthropic():
    """Factory: fake_anthropic({subject: {domains, summary, …}}) -> client.

    Each create() call emits one tool_use block per configured subject; the
    classifier filters to the subjects actually in its batch. Override token
    counts with in_tok / out_tok.
    """
    def _make(verdicts: dict[str, dict], *, tool_name: str = "classify_subject",
              in_tok: int = 10, out_tok: int = 20):
        blocks = [
            SimpleNamespace(type="tool_use", name=tool_name, input={"subject": s, **v})
            for s, v in verdicts.items()
        ]
        resp = SimpleNamespace(
            content=blocks,
            usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
        )
        return SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: resp))

    return _make


@pytest.fixture
def seeded_db(tmp_paths, monkeypatch):
    """Full-schema events.db pre-loaded with the multi-source seed.

    Returns the live connection. People/projects config is injected so
    enrich_refs / derive lookups resolve against the seed roster.
    """
    monkeypatch.setattr(common, "_people_config", SEED_PEOPLE, raising=False)
    monkeypatch.setattr(common, "_projects_config", SEED_PROJECTS, raising=False)
    import derive.sources_config as sc
    monkeypatch.setattr(sc, "github_handle_prefixes", lambda: ["org"])

    conn = common.get_db(tmp_paths.db_path)
    build_seed(conn)
    yield conn
    conn.close()

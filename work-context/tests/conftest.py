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

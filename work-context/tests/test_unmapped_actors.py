"""derive/unmapped_actors.py — full per-source list of unscoped ingest actors.

compute(conn) must mirror the {github,jira,confluence}_validate selection
exactly: github counts only commit_in_pr and drops [bot]; jira/confluence count
all actors; an actor is unmapped iff _build_actor_scope_map() has no scope for
it. Roster is injected via common._people_config (the loader source) so no
config file is read.
"""

from __future__ import annotations

import pytest

from ingest import common
from derive import unmapped_actors

ROSTER = [
    {"github": "alice-gh", "scope": "team", "git_names": ["alice-gh"]},
    {"email": "known@example.com", "scope": "org"},
]


def _ev(conn, **kw):
    cols = ("id", "source", "event_type", "ts", "actor", "subject", "raw_path")
    vals = tuple(kw.get(c) for c in cols)
    conn.execute(
        f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        vals,
    )


@pytest.fixture
def populated(db_conn, monkeypatch):
    monkeypatch.setattr(common, "_people_config", ROSTER, raising=False)
    rows = [
        # github — only commit_in_pr counts
        dict(id="g1", source="github", event_type="commit_in_pr",
             ts="2026-06-10T00:00:00Z", actor="alice-gh", subject="o/r#1", raw_path="r#1"),  # mapped
        dict(id="g2", source="github", event_type="commit_in_pr",
             ts="2026-06-11T00:00:00Z", actor="charlie-gh", subject="o/r#2", raw_path="r#2"),  # UNMAPPED
        dict(id="g3", source="github", event_type="commit_in_pr",
             ts="2026-06-12T00:00:00Z", actor="charlie-gh", subject="o/r#3", raw_path="r#3"),  # UNMAPPED (same)
        dict(id="g4", source="github", event_type="commit_in_pr",
             ts="2026-06-12T00:00:00Z", actor="dependabot[bot]", subject="o/r#4", raw_path="r#4"),  # bot → skip
        dict(id="g5", source="github", event_type="review",
             ts="2026-06-12T00:00:00Z", actor="zoe-gh", subject="o/r#5", raw_path="r#5"),  # not commit_in_pr → skip
        # jira — all actors count
        dict(id="j1", source="jira", event_type="issue_created",
             ts="2026-06-10T00:00:00Z", actor="known@example.com", subject="EX-1", raw_path="r#6"),  # mapped
        dict(id="j2", source="jira", event_type="comment",
             ts="2026-06-11T00:00:00Z", actor="dave@example.com", subject="EX-2", raw_path="r#7"),  # UNMAPPED
        # confluence — all actors count
        dict(id="c1", source="confluence", event_type="page_updated",
             ts="2026-06-11T00:00:00Z", actor="acc-eve", subject="page:99", raw_path="r#8"),  # UNMAPPED
    ]
    for r in rows:
        _ev(db_conn, **r)
    db_conn.commit()
    return db_conn


def test_lists_only_unmapped_per_source(populated):
    rep = unmapped_actors.compute(populated)
    gh = {r["actor"] for r in rep["by_source"]["github"]}
    assert gh == {"charlie-gh"}                    # alice mapped, bot+review excluded
    assert {r["actor"] for r in rep["by_source"]["jira"]} == {"dave@example.com"}
    assert {r["actor"] for r in rep["by_source"]["confluence"]} == {"acc-eve"}
    assert rep["n_unmapped_total"] == 3


def test_count_and_samples(populated):
    rep = unmapped_actors.compute(populated)
    charlie = rep["by_source"]["github"][0]
    assert charlie["actor"] == "charlie-gh"
    assert charlie["count"] == 2                   # two commit_in_pr rows
    assert set(charlie["samples"]) == {"o/r#2", "o/r#3"}


def test_all_scoped_is_empty(db_conn, monkeypatch):
    monkeypatch.setattr(common, "_people_config", ROSTER, raising=False)
    _ev(db_conn, id="g1", source="github", event_type="commit_in_pr",
        ts="2026-06-10T00:00:00Z", actor="alice-gh", subject="o/r#1", raw_path="r#1")
    db_conn.commit()
    rep = unmapped_actors.compute(db_conn)
    assert rep["n_unmapped_total"] == 0
    assert rep["by_source"]["github"] == []


def test_render_human_empty_vs_populated(capsys):
    unmapped_actors._render_human({"computed_at": "t", "n_unmapped_total": 0, "by_source": {}})
    assert "all ingest actors scoped" in capsys.readouterr().out
    unmapped_actors._render_human({
        "computed_at": "t", "n_unmapped_total": 1,
        "by_source": {"github": [{"actor": "x-gh", "count": 2, "samples": ["o/r#1"]}],
                      "jira": [], "confluence": []}})
    out = capsys.readouterr().out
    assert "1 unmapped actor(s)" in out and "x-gh" in out


def test_main_json_smoke(populated, monkeypatch, capsys):
    # main() reads its own DB_PATH + sys.argv; point it at the populated temp db.
    monkeypatch.setattr(unmapped_actors, "DB_PATH", common.DB_PATH)
    monkeypatch.setattr("sys.argv", ["unmapped_actors.py", "--json"])
    assert unmapped_actors.main() == 0
    import json
    rep = json.loads(capsys.readouterr().out)
    assert rep["n_unmapped_total"] == 3


def test_main_db_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(unmapped_actors, "DB_PATH", tmp_path / "nope.db")
    monkeypatch.setattr("sys.argv", ["unmapped_actors.py"])
    assert unmapped_actors.main() == 2

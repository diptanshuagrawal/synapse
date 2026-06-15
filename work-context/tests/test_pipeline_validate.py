"""derive/pipeline_validate.py — cross-cutting events.db integrity checks.

Two layers:
  - one happy-path INTEGRATION test that stores real events through the actual
    ingest writer (common.store_event) and asserts the whole battery is clean;
  - targeted unit tests that hand-build a minimal db with ONE injected
    violation each, so every FAIL/WARN branch is pinned independently.

Freshness uses a monkeypatched _now() so the tests are deterministic regardless
of wall-clock.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta

import pytest

from ingest import common
from derive import pipeline_validate as pv

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _freeze_now(monkeypatch):
    monkeypatch.setattr(pv, "_now", lambda: NOW)


def _sev(report, check):
    """Severity of the (first) finding whose check == name, or None."""
    for sev, c, _msg in report["findings"]:
        if c == check:
            return sev
    return None


def _checks(report):
    return {c for _s, c, _m in report["findings"]}


# ── minimal controllable db ────────────────────────────────────────────────

def _mini_db(events, refs=None, fts_count=None):
    """Build an in-memory db with just enough surface for compute().

    events: list of dicts with keys id, source, event_type, ts, raw_path.
    refs:   list of (event_id, ref_type, ref_value) — event_refs rows.
    fts_count: if set, events_fts gets this many rows; else mirrors events.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE events (id TEXT, source TEXT, event_type TEXT, ts TEXT, raw_path TEXT)")
    conn.executemany(
        "INSERT INTO events (id, source, event_type, ts, raw_path) VALUES (?,?,?,?,?)",
        [(e.get("id"), e.get("source"), e.get("event_type"), e.get("ts"),
          e.get("raw_path")) for e in events],
    )
    conn.execute("CREATE TABLE event_refs (event_id TEXT, ref_type TEXT, ref_value TEXT)")
    if refs:
        conn.executemany(
            "INSERT INTO event_refs (event_id, ref_type, ref_value) VALUES (?,?,?)", refs)
    conn.execute("CREATE TABLE events_fts (rowid INTEGER)")
    n_fts = fts_count if fts_count is not None else len(events)
    conn.executemany("INSERT INTO events_fts (rowid) VALUES (?)",
                     [(i,) for i in range(n_fts)])
    conn.commit()
    return conn


def _evt(**kw):
    base = dict(id="e1", source="jira", event_type="issue_created",
                ts="2026-06-15T10:00:00Z", raw_path="raw/jira/2026/06/15.jsonl#1")
    base.update(kw)
    return base


# ── integration: real writer → all green ────────────────────────────────────

def test_integration_clean_db_all_pass(db_conn, tmp_paths):
    # Store a handful of valid events through the real ingest path, all recent.
    recent = (NOW - timedelta(hours=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    for i, src in enumerate(["jira", "github", "confluence", "slack"]):
        ev = common.Event(
            id=f"i{i}", source=src,
            event_type={"jira": "issue_created", "github": "pr_opened",
                        "confluence": "page_created", "slack": "thread_started"}[src],
            ts=recent, actor="alice", subject=f"s{i}", title="t", body="b",
            url="u", raw_path=f"raw/{src}/x#{i}")
        common.insert_event(db_conn, ev)
    report = pv.compute(db_conn)
    sevs = {sev for sev, _c, _m in report["findings"]}
    assert "FAIL" not in sevs, [f for f in report["findings"] if f[0] == "FAIL"]
    assert _sev(report, "schema_nulls") == "PASS"
    assert _sev(report, "orphan_refs") == "PASS"
    assert _sev(report, "fts_sync") == "PASS"
    assert _sev(report, "raw_path_dupes") == "PASS"


# ── empty db ─────────────────────────────────────────────────────────────────

def test_empty_db_warns(db_conn):
    report = pv.compute(db_conn)
    assert _sev(report, "empty_db") == "WARN"
    assert report["n_total_events"] == 0


# ── schema_nulls ─────────────────────────────────────────────────────────────

def test_schema_null_required_column_fails():
    conn = _mini_db([_evt(), _evt(id="e2", raw_path=None)])
    report = pv.compute(conn)
    assert _sev(report, "schema_nulls") == "FAIL"


# ── ts_format / ts_future ────────────────────────────────────────────────────

def test_bad_ts_shape_fails():
    conn = _mini_db([_evt(), _evt(id="e2", ts="15/06/2026 10:00")])
    report = pv.compute(conn)
    assert _sev(report, "ts_format") == "FAIL"


def test_future_ts_warns():
    far = (NOW + timedelta(hours=72)).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn = _mini_db([_evt(), _evt(id="e2", ts=far)])
    report = pv.compute(conn)
    assert _sev(report, "ts_future") == "WARN"


def test_recent_future_within_skew_passes():
    near = (NOW + timedelta(hours=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn = _mini_db([_evt(), _evt(id="e2", ts=near)])
    report = pv.compute(conn)
    assert _sev(report, "ts_future") == "PASS"


# ── vocabulary ───────────────────────────────────────────────────────────────

def test_unknown_source_warns():
    conn = _mini_db([_evt(), _evt(id="e2", source="bitbucket")])
    report = pv.compute(conn)
    assert _sev(report, "source_vocab") == "WARN"


def test_unknown_event_type_warns():
    conn = _mini_db([_evt(), _evt(id="e2", event_type="frobnicated")])
    report = pv.compute(conn)
    assert _sev(report, "type_vocab") == "WARN"


# ── orphan refs ──────────────────────────────────────────────────────────────

def test_orphan_ref_fails():
    conn = _mini_db([_evt(id="e1")],
                    refs=[("e1", "person", "alice"), ("GHOST", "ticket", "EX-9")])
    report = pv.compute(conn)
    assert _sev(report, "orphan_refs") == "FAIL"


def test_clean_refs_pass():
    conn = _mini_db([_evt(id="e1")], refs=[("e1", "person", "alice")])
    report = pv.compute(conn)
    assert _sev(report, "orphan_refs") == "PASS"


# ── fts sync ─────────────────────────────────────────────────────────────────

def test_fts_count_mismatch_warns():
    conn = _mini_db([_evt(id="e1"), _evt(id="e2")], fts_count=1)
    report = pv.compute(conn)
    assert _sev(report, "fts_sync") == "WARN"


# ── raw_path dupes (the append_raw race) ─────────────────────────────────────

def test_raw_path_collision_fails():
    conn = _mini_db([
        _evt(id="e1", raw_path="raw/jira/2026/06/15.jsonl#7"),
        _evt(id="e2", raw_path="raw/jira/2026/06/15.jsonl#7"),  # collision
    ])
    report = pv.compute(conn)
    assert _sev(report, "raw_path_dupes") == "FAIL"


def test_derived_source_shared_path_not_flagged():
    # 'service' briefs reuse a rendered .md path as raw_path — NOT the
    # append_raw 'raw/…#N' shape, so a shared value must not false-alarm.
    conn = _mini_db([
        _evt(id="s1", source="service", event_type="service_brief",
             raw_path="derived/services/example-svc.md"),
        _evt(id="s2", source="service", event_type="service_brief",
             raw_path="derived/services/example-svc.md"),
    ])
    report = pv.compute(conn)
    assert _sev(report, "raw_path_dupes") == "PASS"


# ── freshness ────────────────────────────────────────────────────────────────

def test_stale_source_fails():
    # jira fail budget = 96h; make latest jira event 120h old.
    old = (NOW - timedelta(hours=120)).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn = _mini_db([_evt(id="e1", source="jira", ts=old)])
    report = pv.compute(conn)
    assert _sev(report, "freshness:jira") == "FAIL"


def test_drifting_source_warns():
    # jira warn budget = 36h; 48h old → WARN, not FAIL.
    old = (NOW - timedelta(hours=48)).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn = _mini_db([_evt(id="e1", source="jira", ts=old)])
    report = pv.compute(conn)
    assert _sev(report, "freshness:jira") == "WARN"


def test_fresh_source_passes():
    fresh = (NOW - timedelta(hours=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn = _mini_db([_evt(id="e1", source="jira", ts=fresh)])
    report = pv.compute(conn)
    assert _sev(report, "freshness") == "PASS"


def test_service_source_skipped_from_freshness():
    # 'service' has no freshness budget → no freshness:service finding.
    old = (NOW - timedelta(hours=999)).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn = _mini_db([_evt(id="e1", source="service",
                          event_type="service_brief", ts=old)])
    report = pv.compute(conn)
    assert "freshness:service" not in _checks(report)

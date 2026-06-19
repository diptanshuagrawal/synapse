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
        "CREATE TABLE events (id TEXT, source TEXT, event_type TEXT, ts TEXT, "
        "raw_path TEXT, actor TEXT, subject TEXT, channel_id TEXT)")
    conn.executemany(
        "INSERT INTO events (id, source, event_type, ts, raw_path, actor, subject, "
        "channel_id) VALUES (?,?,?,?,?,?,?,?)",
        [(e.get("id"), e.get("source"), e.get("event_type"), e.get("ts"),
          e.get("raw_path"), e.get("actor"), e.get("subject"),
          e.get("channel_id")) for e in events],
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
    # Defaults are a well-formed jira event (shaped subject, actor present) so
    # the new ingest-shape checks PASS unless a test injects a specific defect.
    base = dict(id="e1", source="jira", event_type="issue_created",
                ts="2026-06-15T10:00:00Z", raw_path="raw/jira/2026/06/15.jsonl#1",
                actor="alice", subject="EX-1", channel_id=None)
    base.update(kw)
    return base


# ── integration: real writer → all green ────────────────────────────────────

def test_integration_clean_db_all_pass(db_conn, tmp_paths):
    # Store a handful of valid events through the real ingest path, all recent.
    # Subjects use each source's real id grammar + slack carries a channel_id,
    # so the ingest-shape checks (subject_shape / slack_channel_id) stay green.
    recent = (NOW - timedelta(hours=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    spec = {
        "jira":       ("issue_created", "EX-1", None),
        "github":     ("pr_opened", "org/repo#1", None),
        "confluence": ("page_created", "page:123", None),
        "slack":      ("thread_started", "slack:C0A:1700000000.000100", "C0A"),
    }
    for i, (src, (etype, subject, chan)) in enumerate(spec.items()):
        ev = common.Event(
            id=f"i{i}", source=src, event_type=etype,
            ts=recent, actor="alice", subject=subject, title="t", body="b",
            url="u", raw_path=f"raw/{src}/x#{i}")
        common.insert_event(db_conn, ev)
    # The real slack path back-fills channel_id in a later step (insert_event
    # omits it); mirror that so the slack_channel_id check sees a populated value.
    db_conn.execute("UPDATE events SET channel_id='C0A' WHERE source='slack'")
    db_conn.commit()
    report = pv.compute(db_conn)
    sevs = {sev for sev, _c, _m in report["findings"]}
    assert "FAIL" not in sevs, [f for f in report["findings"] if f[0] == "FAIL"]
    assert "WARN" not in sevs, [f for f in report["findings"] if f[0] == "WARN"]
    for chk in ("schema_nulls", "orphan_refs", "fts_sync", "raw_path_dupes",
                "slack_channel_id", "null_actor_subject", "subject_shape", "ref_vocab"):
        assert _sev(report, chk) == "PASS", chk


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


def test_future_ts_exactly_at_boundary_passes():
    # Check is strict `ts > now+skew`, so a ts exactly at the boundary is OK.
    edge = (NOW + timedelta(hours=pv.FUTURE_SKEW_H)).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    conn = _mini_db([_evt(), _evt(id="e2", ts=edge)])
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


# ── slack_channel_id (attribution integrity) ─────────────────────────────────

def test_slack_missing_channel_id_fails():
    conn = _mini_db([
        _evt(id="s1", source="slack", event_type="thread_started",
             subject="slack:C0A:1700000000.000100", channel_id="C0A"),
        _evt(id="s2", source="slack", event_type="thread_reply",
             subject="slack:C0A:1700000000.000200", channel_id=None),  # missing
    ])
    report = pv.compute(conn)
    assert _sev(report, "slack_channel_id") == "FAIL"


def test_slack_with_channel_id_passes():
    conn = _mini_db([_evt(id="s1", source="slack", event_type="thread_started",
                          subject="slack:C0A:1700000000.000100", channel_id="C0A")])
    report = pv.compute(conn)
    assert _sev(report, "slack_channel_id") == "PASS"


def test_no_slack_no_channel_finding():
    # check only runs when slack events exist.
    conn = _mini_db([_evt()])  # jira only
    report = pv.compute(conn)
    assert "slack_channel_id" not in _checks(report)


# ── null_actor_subject (parse-regression guard) ──────────────────────────────

def test_missing_actor_on_non_service_fails():
    conn = _mini_db([_evt(), _evt(id="e2", actor=None)])
    report = pv.compute(conn)
    assert _sev(report, "null_actor_subject") == "FAIL"


def test_missing_subject_fails():
    conn = _mini_db([_evt(), _evt(id="e2", subject=None)])
    report = pv.compute(conn)
    assert _sev(report, "null_actor_subject") == "FAIL"


def test_service_null_actor_is_exempt():
    # 'service' briefs are author-less by design → null actor must NOT fail,
    # but they still need a subject.
    conn = _mini_db([_evt(id="s1", source="service", event_type="service_brief",
                          subject="service:acct#x", actor=None)])
    report = pv.compute(conn)
    assert _sev(report, "null_actor_subject") == "PASS"


# ── subject_shape (per-source id grammar) ────────────────────────────────────

def test_offshape_subject_warns():
    # a jira subject that isn't PROJ-N shaped.
    conn = _mini_db([_evt(), _evt(id="e2", subject="not-a-key")])
    report = pv.compute(conn)
    assert _sev(report, "subject_shape") == "WARN"
    assert report["stats"]["subject_shape_bad"].get("jira") == 1


@pytest.mark.parametrize("src,subject", [
    ("jira", "EX-2301"),
    ("github", "org/repo#10"),
    ("github", "org/svc@1a2b3c4d"),
    ("confluence", "page:123456789"),
    ("slack", "slack:C0A:1700000000.000100"),
    ("service", "service:acct#endpoints"),
])
def test_wellshaped_subjects_pass(src, subject):
    et = {"jira": "issue_created", "github": "pr_opened",
          "confluence": "page_created", "slack": "thread_started",
          "service": "service_brief"}[src]
    chan = "C0A" if src == "slack" else None
    conn = _mini_db([_evt(id="e1", source=src, event_type=et,
                          subject=subject, channel_id=chan)])
    report = pv.compute(conn)
    assert _sev(report, "subject_shape") == "PASS"


# ── ref_vocab (event_refs ref_type + ref_value) ──────────────────────────────

def test_unknown_ref_type_warns():
    conn = _mini_db([_evt(id="e1")],
                    refs=[("e1", "person", "alice"), ("e1", "gadget", "x")])
    report = pv.compute(conn)
    assert _sev(report, "ref_vocab") == "WARN"


def test_empty_ref_value_warns():
    conn = _mini_db([_evt(id="e1")], refs=[("e1", "person", "")])
    report = pv.compute(conn)
    assert _sev(report, "ref_vocab") == "WARN"


def test_clean_refs_vocab_passes():
    conn = _mini_db([_evt(id="e1")],
                    refs=[("e1", "person", "alice"), ("e1", "ticket", "EX-1")])
    report = pv.compute(conn)
    assert _sev(report, "ref_vocab") == "PASS"


# ── dangling_derived (derived rows outliving their source event) ─────────────

def _add_derived(conn, table, subjects):
    """Create a derived table with a `subject` column and seed it."""
    conn.execute(f"CREATE TABLE {table} (subject TEXT)")
    conn.executemany(f"INSERT INTO {table} (subject) VALUES (?)",
                     [(s,) for s in subjects])
    conn.commit()


def test_dangling_derived_warns():
    # EX-1 exists as an event; GHOST-9 does not → one dangling member.
    conn = _mini_db([_evt(id="e1", subject="EX-1")])
    _add_derived(conn, "topic_brief_member", ["EX-1", "GHOST-9"])
    report = pv.compute(conn)
    assert _sev(report, "dangling_derived") == "WARN"
    assert report["stats"]["dangling_derived"].get("topic_brief_member") == 1


def test_dangling_derived_all_resolved_passes():
    conn = _mini_db([_evt(id="e1", subject="EX-1")])
    _add_derived(conn, "embedding", ["EX-1"])
    report = pv.compute(conn)
    assert _sev(report, "dangling_derived") == "PASS"


def test_dangling_derived_no_derived_tables_passes():
    # none of the derived tables exist → check runs without crashing, PASS.
    conn = _mini_db([_evt(id="e1", subject="EX-1")])
    report = pv.compute(conn)
    assert _sev(report, "dangling_derived") == "PASS"


def test_dangling_derived_table_without_subject_col_skipped():
    # a derived table that lacks a `subject` column must be skipped, not crash.
    conn = _mini_db([_evt(id="e1", subject="EX-1")])
    conn.execute("CREATE TABLE thread_enriched (channel_id TEXT)")
    conn.execute("INSERT INTO thread_enriched (channel_id) VALUES ('C0A')")
    conn.commit()
    report = pv.compute(conn)
    assert _sev(report, "dangling_derived") == "PASS"

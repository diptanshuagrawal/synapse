"""insert_event / store_event — dedup, refs fan-out, FTS sync.

insert_event is the only writer into events + event_refs + events_fts. Its
idempotency (dedup on the event.id PK) is what makes ingest safe to re-run on
overlapping cursor windows — a regression here means either lost events or
double-counted ones. The FTS sync is load-bearing for /ask full-text search.
"""

from __future__ import annotations

from ingest import common


def _refs_for(conn, event_id):
    return {
        (r["ref_type"], r["ref_value"])
        for r in conn.execute(
            "SELECT ref_type, ref_value FROM event_refs WHERE event_id=?",
            (event_id,),
        )
    }


def test_topic_brief_has_all_queried_columns(db_conn):
    # Regression guard (review finding): ask_engine / topic_brief_validate /
    # cluster_ownership_rollup query these columns; _ensure_schema must create
    # them, not rely on a later derive step ALTERing them in (order-of-execution
    # crash on a fresh DB).
    cols = {r[1] for r in db_conn.execute("PRAGMA table_info(topic_brief)")}
    required = {"root_cause", "confidence", "outcomes_json", "followups_json",
                "risk_areas_json", "stakeholders_json", "artifacts_json",
                "owner_distribution_json"}
    assert required <= cols, f"missing: {required - cols}"


def test_insert_returns_true_then_false_on_dup(db_conn, make_event):
    ev = make_event(id="dup-1")
    assert common.insert_event(db_conn, ev) is True
    # Re-inserting the same id is a no-op duplicate.
    assert common.insert_event(db_conn, ev) is False
    n = db_conn.execute("SELECT COUNT(*) FROM events WHERE id='dup-1'").fetchone()[0]
    assert n == 1


def test_insert_persists_core_columns(db_conn, make_event):
    ev = make_event(id="e1", source="jira", event_type="status_change",
                    to_status="Done", story_points=3.0, issue_type="Task")
    common.insert_event(db_conn, ev)
    row = db_conn.execute(
        "SELECT source, event_type, to_status, story_points, issue_type "
        "FROM events WHERE id='e1'").fetchone()
    assert tuple(row) == ("jira", "status_change", "Done", 3.0, "Task")


def test_refs_fan_out_into_event_refs(db_conn, make_event):
    ev = make_event(id="e2")
    ev.refs = common.Refs(
        people=["alice"], projects=["payments"], tickets=["EX-1"],
        pages=["123456789"], pull_requests=["org/repo#1"],
        slack_threads=["slack:C0A:1700000000.000100"],
    )
    common.insert_event(db_conn, ev)
    got = _refs_for(db_conn, "e2")
    assert got == {
        ("person", "alice"), ("project", "payments"), ("ticket", "EX-1"),
        ("page", "123456789"), ("pull_request", "org/repo#1"),
        ("slack_thread", "slack:C0A:1700000000.000100"),
    }


def test_refs_idempotent_on_reinsert(db_conn, make_event):
    ev = make_event(id="e3")
    ev.refs = common.Refs(people=["alice"], tickets=["EX-1"])
    common.insert_event(db_conn, ev)
    common.insert_event(db_conn, ev)  # dup — must not duplicate refs
    n = db_conn.execute(
        "SELECT COUNT(*) FROM event_refs WHERE event_id='e3'").fetchone()[0]
    assert n == 2


def test_fts_index_kept_in_sync(db_conn, make_event):
    common.insert_event(db_conn, make_event(
        id="e4", title="quarterly ledger migration", body="payout details"))
    hit = db_conn.execute(
        "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'ledger'"
    ).fetchone()[0]
    assert hit == 1


def test_dry_run_writes_nothing(db_conn, make_event):
    assert common.insert_event(db_conn, make_event(id="e5"), dry_run=True) is True
    n = db_conn.execute("SELECT COUNT(*) FROM events WHERE id='e5'").fetchone()[0]
    assert n == 0


def test_store_event_round_trip(db_conn, tmp_paths, make_event):
    # store_event = append_raw + insert_event. raw_path must be stamped + the
    # row must be queryable.
    ev = make_event(id="e6", source="github", ts="2026-06-10T09:00:00Z")
    assert common.store_event(db_conn, ev) is True
    row = db_conn.execute(
        "SELECT raw_path FROM events WHERE id='e6'").fetchone()
    assert row["raw_path"].startswith("raw/github/2026/06/10.jsonl#")

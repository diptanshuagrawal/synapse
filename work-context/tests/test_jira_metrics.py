"""derive/jira_metrics.py — shared Jira-interpretation primitives.

This module is the single source of truth for ticket-credit attribution,
dev-vs-reviewer inference, ops detection, and identity resolution; every
people-facing skill (/standup, /retro, /ask) consumes it. The rules encoded
here (dedup one-credit-per-ticket, changelog→creation→unknown chain, status
transitions are clerical not ownership) are subtle — these tests pin them so a
refactor can't silently change who gets credit.

All tests use synthetic in-memory DBs + an explicit people_lookup, so they
never depend on the live events.db or config/people.yaml.
"""

from __future__ import annotations

import sqlite3

import pytest

from derive import jira_metrics as jm

# Email-only aliases (frank/grace) exercise the token-signature fallback;
# sam-a/sam-b deliberately collide to test ambiguity-drop.
PL = {
    "alice@x.com": "alice", "alice example": "alice", "alice": "alice",
    "bob@x.com": "bob", "bob example": "bob", "bob": "bob",
    "frank.lee@x.com": "frank",
    "sam.jones@a.com": "sam-a", "jones.sam@b.com": "sam-b",
}


def _events_conn():
    """In-memory events table with every column jira_metrics touches."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE events (
            id TEXT, source TEXT, event_type TEXT, ts TEXT, actor TEXT,
            subject TEXT, title TEXT, body TEXT, assignee TEXT, to_status TEXT,
            issue_type TEXT, story_points REAL, sprint_name TEXT, sprint_state TEXT
        )
    """)
    return conn


def _ins(conn, **kw):
    cols = ("id", "source", "event_type", "ts", "actor", "subject", "title",
            "body", "assignee", "to_status", "issue_type", "story_points",
            "sprint_name", "sprint_state")
    row = {c: kw.get(c) for c in cols}
    row["source"] = kw.get("source") or "jira"
    conn.execute(
        f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        tuple(row[c] for c in cols),
    )
    conn.commit()


# ── identity resolution ────────────────────────────────────────────────────

def test_name_sig_collapses_representations():
    assert jm._name_sig("Alice Example") == jm._name_sig("alice.example@x.com")


def test_resolve_canonical_direct_alias():
    assert jm.resolve_canonical("alice@x.com", PL) == "alice"


def test_resolve_canonical_token_sig_fallback():
    # "Frank Lee" is not a literal alias; only the email is on file.
    assert jm.resolve_canonical("Frank Lee", PL) == "frank"


def test_resolve_canonical_ambiguous_is_none():
    # sam-a and sam-b share {sam,jones} → signature dropped → no guess.
    assert jm.resolve_canonical("Sam Jones", PL) is None


def test_resolve_canonical_none_input():
    assert jm.resolve_canonical(None, PL) is None


def test_same_person_cross_representation():
    assert jm._same_person(None, "Frank Lee", None, "frank.lee@x.com")
    assert not jm._same_person("alice", None, "bob", None)


# ── compute_done_credits: dedup + attribution chain ────────────────────────

def test_done_credit_changelog_attribution():
    conn = _events_conn()
    _ins(conn, event_type="issue_created", ts="2026-06-01", subject="EX-1",
         assignee="alice@x.com", story_points=5.0, sprint_name="S1",
         sprint_state="active", issue_type="Task")
    _ins(conn, event_type="assignment", ts="2026-06-02", subject="EX-1",
         title="assignee: ∅ → Alice Example")
    _ins(conn, event_type="status_change", ts="2026-06-03", subject="EX-1",
         title="EX-1 In Progress → Done", to_status="Done")
    credits = jm.compute_done_credits(conn, "2026-06-01", "2026-06-30", PL)
    assert len(credits) == 1
    c = credits[0]
    assert c.canonical == "alice" and c.source == "changelog" and c.story_points == 5.0


def test_done_credit_dedups_reopen_reclose():
    conn = _events_conn()
    _ins(conn, event_type="issue_created", ts="2026-06-01", subject="EX-2",
         assignee="alice@x.com", story_points=3.0)
    # Two Done transitions on the same subject → exactly one credit.
    _ins(conn, event_type="status_change", ts="2026-06-03", subject="EX-2",
         title="EX-2 In Review → Done", to_status="Done")
    _ins(conn, event_type="status_change", ts="2026-06-09", subject="EX-2",
         title="EX-2 Reopened → Done", to_status="Done")
    credits = jm.compute_done_credits(conn, "2026-06-01", "2026-06-30", PL)
    assert len(credits) == 1


def test_done_credit_creation_fallback():
    conn = _events_conn()
    # No assignment changelog → falls back to issue_created.assignee.
    _ins(conn, event_type="issue_created", ts="2026-06-01", subject="EX-3",
         assignee="bob@x.com", story_points=2.0)
    _ins(conn, event_type="status_change", ts="2026-06-04", subject="EX-3",
         title="EX-3 In Progress → Done", to_status="Done")
    credits = jm.compute_done_credits(conn, "2026-06-01", "2026-06-30", PL)
    assert credits[0].canonical == "bob" and credits[0].source == "creation_fallback"


def test_done_credit_unknown_assignee():
    conn = _events_conn()
    _ins(conn, event_type="issue_created", ts="2026-06-01", subject="EX-4",
         assignee="ghost@nowhere.com", story_points=1.0)
    _ins(conn, event_type="status_change", ts="2026-06-04", subject="EX-4",
         title="EX-4 → Done", to_status="Done")
    credits = jm.compute_done_credits(conn, "2026-06-01", "2026-06-30", PL)
    assert credits[0].canonical is None and credits[0].source == "unknown"


def test_done_credit_window_excludes_outside():
    conn = _events_conn()
    _ins(conn, event_type="issue_created", ts="2026-05-01", subject="EX-5",
         assignee="alice@x.com", story_points=8.0)
    _ins(conn, event_type="status_change", ts="2026-05-05", subject="EX-5",
         title="EX-5 → Done", to_status="Done")
    credits = jm.compute_done_credits(conn, "2026-06-01", "2026-06-30", PL)
    assert credits == []


# ── aggregations ───────────────────────────────────────────────────────────

def test_aggregate_velocity_by_actor():
    credits = [
        jm.TicketCredit("EX-1", "alice", 5.0, "S1", "active", "Task", "t", "changelog"),
        jm.TicketCredit("EX-2", "alice", 3.0, "S1", "active", "Task", "t", "creation_fallback"),
        jm.TicketCredit("EX-3", None, 2.0, "S1", "active", "Task", "t", "unknown"),
    ]
    agg = jm.aggregate_velocity_by_actor(credits)
    assert agg["alice"]["sp"] == 8.0 and agg["alice"]["tickets"] == 2
    assert "alice" in agg and None not in agg  # unattributed dropped


def test_attribution_source_summary():
    credits = [
        jm.TicketCredit("EX-1", "alice", 1, "", "", "", "t", "changelog"),
        jm.TicketCredit("EX-2", None, 1, "", "", "", "t", "unknown"),
    ]
    summ = jm.attribution_source_summary(credits)
    assert summ == {"changelog": 1, "creation_fallback": 0, "unknown": 1}


# ── ops detection ──────────────────────────────────────────────────────────

def test_ops_regex_matches_incident_terms():
    assert jm._OPS_RE.search("P1 outage in prod")
    assert jm._OPS_RE.search("RCA for double-credit bug")
    assert not jm._OPS_RE.search("add a new dashboard widget")


def test_detect_ops_tickets_title_scan():
    conn = _events_conn()
    _ins(conn, event_type="issue_created", ts="2026-06-02", actor="alice@x.com",
         subject="EX-9", title="P0 incident: payout outage", issue_type="Bug")
    _ins(conn, event_type="issue_created", ts="2026-06-02", actor="alice@x.com",
         subject="EX-10", title="routine config tweak")
    ops = jm.detect_ops_tickets(conn, ["alice@x.com"], "2026-06-01", "2026-06-30")
    assert [o.subject for o in ops] == ["EX-9"]


# ── role inference (the dev-vs-reviewer rule) ──────────────────────────────

def _roles_conn(rows):
    """rows: (event_type, ts, subject, assignee_or_pair, to_status)."""
    conn = _events_conn()
    for et, ts, sub, asg, tost in rows:
        if et == "assignment":
            frm, to = asg
            _ins(conn, event_type=et, ts=ts, subject=sub,
                 title=f"assignee: {frm} → {to}")
        else:
            _ins(conn, event_type=et, ts=ts, subject=sub, assignee=asg,
                 title=f"{et} {sub}", to_status=tost)
    return conn


def test_role_dev_awaiting_reviewer():
    conn = _roles_conn([
        ("issue_created", "01", "S1", "alice@x.com", "To Do"),
        ("assignment", "02", "S1", ("∅", "Alice Example"), None),
        ("status_change", "03", "S1", None, "In Progress"),
        ("status_change", "04", "S1", None, "In Review"),
    ])
    r = jm.infer_ticket_roles(conn, "S1", people_lookup=PL)
    assert r.state == "in_review_awaiting_reviewer"
    assert r.dev == "alice" and r.reviewer is None
    assert jm.member_review_role(r, "alice") == "dev_awaiting_review"


def test_role_active_reviewer():
    conn = _roles_conn([
        ("issue_created", "01", "S2", "alice@x.com", "To Do"),
        ("status_change", "02", "S2", None, "In Progress"),
        ("status_change", "03", "S2", None, "In Review"),
        ("assignment", "04", "S2", ("Alice Example", "Bob Example"), None),
    ])
    r = jm.infer_ticket_roles(conn, "S2", people_lookup=PL)
    assert r.state == "in_review_active"
    assert r.dev == "alice" and r.reviewer == "bob"
    assert jm.member_review_role(r, "bob") == "reviewing"
    assert jm.member_review_role(r, "alice") == "dev_under_review"


def test_role_in_progress_reassignment_current_holder_is_dev():
    conn = _roles_conn([
        ("issue_created", "01", "S4", "alice@x.com", "To Do"),
        ("status_change", "02", "S4", None, "In Progress"),
        ("assignment", "03", "S4", ("Alice Example", "Bob Example"), None),
    ])
    r = jm.infer_ticket_roles(conn, "S4", people_lookup=PL)
    assert r.state == "in_progress" and r.dev == "bob"


def test_infer_all_matches_per_subject():
    conn = _roles_conn([
        ("issue_created", "01", "S1", "alice@x.com", "To Do"),
        ("status_change", "02", "S1", None, "In Progress"),
        ("issue_created", "01", "S2", "bob@x.com", "To Do"),
        ("status_change", "02", "S2", None, "Done"),
    ])
    batch = jm.infer_all_ticket_roles(conn, PL)
    assert set(batch) == {"S1", "S2"}
    for sub in ("S1", "S2"):
        per = jm.infer_ticket_roles(conn, sub, people_lookup=PL)
        assert (batch[sub].state, batch[sub].dev) == (per.state, per.dev)


# ── misc ───────────────────────────────────────────────────────────────────

def test_strip_epic_prefix():
    assert jm.strip_epic_prefix("[Epic EX-2238] do the thing") == "do the thing"
    assert jm.strip_epic_prefix("no prefix here") == "no prefix here"

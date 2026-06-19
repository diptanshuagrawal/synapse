"""derive/ask_engine.py — the DB-backed query functions (seed-driven).

Companion to test_ask_engine.py (window-state + person). Covers the larger query
surface: event_metrics (raw term counts/timeline), root_causes_in_window,
ticket_gaps (the no-embedding gap path), clusters_by_project, and
projects_active_in_window. Each opens its own connection via get_db, injected
to the seed; cluster-map / lifetime-ts rows are added in-test as needed.
"""

from __future__ import annotations

import json

import pytest

from derive import ask_engine as ae

SINCE, UNTIL = "2026-05-01T00:00:00Z", "2026-07-01T00:00:00Z"


@pytest.fixture
def seed(seeded_db, monkeypatch):
    monkeypatch.setattr(ae, "get_db", lambda *a, **k: seeded_db)
    return seeded_db


# ── event_metrics ────────────────────────────────────────────────────────────

def test_event_metrics_counts_matches(seed):
    out = ae.event_metrics(["payout"], SINCE, UNTIL)
    assert out["total"] >= 1                       # seed has 'payout' events
    assert out["first_ts"] and out["per_day"]
    assert out["match"] == "all"


def test_event_metrics_and_vs_any(seed):
    # AND of two disjoint terms → 0; OR → ≥1.
    assert ae.event_metrics(["payout", "zzznomatch"], SINCE, UNTIL)["total"] == 0
    assert ae.event_metrics(["payout", "zzznomatch"], SINCE, UNTIL, match_any=True)["total"] >= 1


def test_event_metrics_source_filter(seed):
    out = ae.event_metrics(["payout"], SINCE, UNTIL, source="github")
    assert out["source_filter"] == "github" and out["total"] >= 1
    # every citation is a github subject (owner/repo#N), not jira/slack.
    assert all("#" in c["subject"] for c in out["sample_citations"])


# ── root_causes_in_window ────────────────────────────────────────────────────

def test_root_causes_in_window(seed):
    # seed cluster 1 has root_cause; give it lifetime ts so the overlap filter hits.
    seed.execute("UPDATE topic_brief SET first_ts=?, last_activity_ts=? WHERE cluster_id=1",
                 ("2026-06-03T07:00:00Z", "2026-06-03T07:05:00Z"))
    seed.commit()
    out = ae.root_causes_in_window(SINCE, UNTIL)
    assert any(c["cluster_id"] == 1 for c in out)
    assert out[0]["window_state"] in ("fully_in", "started_in", "ended_in", "spans")


# ── ticket_gaps (no-embedding path) ──────────────────────────────────────────

def test_ticket_gaps_flags_unlinked_decision_thread(seed):
    # cluster 1's slack thread carries a decision but no linked jira + no embeddings
    # → surfaces as a gap.
    seed.execute(
        "UPDATE topic_brief SET decisions_json=? WHERE cluster_id=1",
        (json.dumps([{"text": "decided to roll back",
                      "evidence_subject": "slack:C0A:1700000000.000100"}]),))
    seed.commit()
    out = ae.ticket_gaps(SINCE, UNTIL)
    subs = {g["subject"] for g in out}
    assert "slack:C0A:1700000000.000100" in subs


# ── clusters_by_project / projects_active_in_window ──────────────────────────

def _link_cluster_to_project(conn, cid, slug, conf=0.9):
    conn.execute("""CREATE TABLE IF NOT EXISTS cluster_project_map (
        cluster_id INTEGER, project_slug TEXT, confidence REAL,
        source TEXT, evidence_json TEXT)""")
    conn.execute("INSERT INTO cluster_project_map (cluster_id, project_slug, confidence, source, evidence_json) "
                 "VALUES (?,?,?,?,?)", (cid, slug, conf, "domain", "[]"))
    conn.commit()


def test_clusters_by_project(seed):
    _link_cluster_to_project(seed, 1, "payments")
    out = ae.clusters_by_project("payments", min_confidence=0.6)
    assert any(c["cluster_id"] == 1 for c in out)


def test_projects_active_in_window(seed):
    seed.execute("UPDATE topic_brief SET first_ts=?, last_activity_ts=?, member_count=1 WHERE cluster_id=1",
                 ("2026-06-03T07:00:00Z", "2026-06-03T07:05:00Z"))
    _link_cluster_to_project(seed, 1, "payments")
    out = ae.projects_active_in_window(SINCE, UNTIL)
    assert out and out[0]["project_slug"] == "payments"
    assert out[0]["cluster_count"] == 1

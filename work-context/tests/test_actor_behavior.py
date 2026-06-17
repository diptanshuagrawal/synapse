"""derive/actor_behavior.py — actor identity maps + incident-cluster metrics.

actor_behavior builds the raw-id → canonical and raw-id → scope maps that the
per-source validators and several derive paths depend on, then computes
per-actor incident behaviour (first-responder, resolver, latency) over
incident-flavoured topic clusters. The map builders are foundational identity
logic; compute_report is the analytic on top. Both pinned here.

People config is injected (monkeypatched common._people_config) so these never
read the live roster; the DB is the temp events.db from conftest.
"""

from __future__ import annotations

import pytest

from ingest import common
from derive import actor_behavior as ab


@pytest.fixture
def people(monkeypatch):
    roster = [
        {"canonical": "alice", "slack_id": "U_ALICE", "github": "alice-gh",
         "email": "alice@x.com", "scope": "team"},
        {"canonical": "bob", "slack_id": "U_BOB", "github": "bob-gh",
         "git_names": ["Bob Builder"], "github_aliases": ["bobby"], "scope": "team"},
        # org-scoped, no canonical → in scope map, NOT in canonical map.
        {"jira_id": "ORG1", "name": "Automation for Jira", "scope": "org"},
        # legacy singular git_name + default scope (team).
        {"canonical": "carol", "git_name": "Carol Legacy"},
    ]
    monkeypatch.setattr(common, "_people_config", roster, raising=False)
    return roster


# ── canonical map ────────────────────────────────────────────────────────────

def test_canonical_map_all_id_shapes(people):
    m = ab._build_actor_canonical_map()
    assert m["U_ALICE"] == "alice"
    assert m["alice-gh"] == "alice"
    assert m["alice@x.com"] == "alice"
    assert m["Bob Builder"] == "bob"       # git_names list
    assert m["bobby"] == "bob"             # github_aliases
    assert m["Carol Legacy"] == "carol"    # legacy singular git_name


def test_canonical_map_skips_entries_without_canonical(people):
    m = ab._build_actor_canonical_map()
    assert "ORG1" not in m  # org entry has no canonical


# ── scope map ─────────────────────────────────────────────────────────────────

def test_scope_map_honours_entries_without_canonical(people):
    m = ab._build_actor_scope_map()
    assert m["ORG1"] == "org"
    assert m["Automation for Jira"] == "org"  # name key when no email
    assert m["U_ALICE"] == "team"


def test_scope_map_defaults_to_team(people):
    m = ab._build_actor_scope_map()
    assert m["Carol Legacy"] == "team"  # no explicit scope → team


# ── _canon ────────────────────────────────────────────────────────────────────

def test_canon_resolves_maps_and_falls_back():
    m = {"U_ALICE": "alice"}
    assert ab._canon("U_ALICE", m) == "alice"
    assert ab._canon("U_GHOST", m) == "<raw:U_GHOST>"
    assert ab._canon(None, m) == "<unknown>"


# ── _parse_iso ─────────────────────────────────────────────────────────────────

def test_parse_iso():
    assert ab._parse_iso("2026-06-10T12:00:00Z") is not None
    assert ab._parse_iso(None) is None
    assert ab._parse_iso("garbage") is None


# ── resolution regex ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "fixed it", "merged now", "rolled out to prod", "✅ done",
    "see github.com/o/r/pull/12", ":white_check_mark:",
])
def test_resolution_pattern_matches(text):
    assert ab._RESOLUTION_PATTERNS.search(text)


def test_resolution_pattern_ignores_plain_text():
    assert not ab._RESOLUTION_PATTERNS.search("still investigating the issue")


# ── compute_report (incident-cluster scoped) ───────────────────────────────────

def _seed_incident(conn, make_event):
    # cluster 1 = incident (root_cause set); cluster 2 = non-incident.
    conn.execute("INSERT INTO topic_brief (cluster_id, label, root_cause) VALUES (1,'Outage','db lock')")
    conn.execute("INSERT INTO topic_brief (cluster_id, label, root_cause) VALUES (2,'Chatter', NULL)")
    conn.execute("INSERT INTO topic_brief_member (cluster_id, subject, source) VALUES (1,'slack:C:1','slack')")
    conn.commit()
    # alice starts the thread; bob first-responds 60s later with a resolving msg.
    conn_insert = lambda **kw: common.insert_event(conn, make_event(**kw))
    conn_insert(id="e1", source="slack", event_type="thread_started",
                subject="slack:C:1", actor="U_ALICE", ts="2026-06-10T10:00:00Z", body="db is down")
    conn_insert(id="e2", source="slack", event_type="thread_reply",
                subject="slack:C:1", actor="U_BOB", ts="2026-06-10T10:01:00Z",
                body="fixed it, merged the rollback")


def test_compute_report_incident_actor_stats(db_conn, people, make_event):
    _seed_incident(db_conn, make_event)
    rep = ab.compute_report(db_conn)
    actors = rep["actors"]
    assert rep["scope"]["incident_clusters"] == 1
    assert set(actors) == {"alice", "bob"}

    # alice authored the thread (can't first-respond to own thread).
    assert actors["alice"]["threads_authored"] == 1
    assert actors["alice"]["first_responder_rate"] is None

    # bob first-responded + resolved.
    assert actors["bob"]["first_responder_count"] == 1
    assert actors["bob"]["reply_count"] == 1
    assert actors["bob"]["resolver_count"] == 1
    assert actors["bob"]["resolver_rate"] == 1.0
    # first-reply latency = 60s; p50 over a single sample is that sample.
    assert actors["bob"]["first_reply_latency_p50_sec"] == 60.0


def test_compute_report_no_incident_clusters(db_conn, people, make_event):
    # Only a non-incident cluster (root_cause NULL) → no incident scope.
    db_conn.execute("INSERT INTO topic_brief (cluster_id, label, root_cause) VALUES (2,'Chatter',NULL)")
    db_conn.commit()
    rep = ab.compute_report(db_conn)
    assert rep["actors"] == {}
    assert rep["scope"]["incident_clusters"] == 0

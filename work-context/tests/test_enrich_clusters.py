"""derive/enrich_clusters.py — per-cluster enrichment payload builder.

enrich_clusters assembles the context the LLM enrichment pass reads for one
cluster: members, per-subject timestamps + actor counts, sampled content, and
canonical-resolved participants. Driven against the seed's incident cluster
(cluster 1 = the slack thread alice authored).
"""

from __future__ import annotations

from derive import enrich_clusters as ec


def test_cluster_members(seeded_db):
    assert ec._cluster_members(seeded_db, 1) == ["slack:C0A:1700000000.000100"]
    assert ec._cluster_members(seeded_db, 999) == []


def test_event_summary_for_subject(seeded_db):
    info = ec._event_summary_for_subject(seeded_db, "slack:C0A:1700000000.000100")
    assert info["subject"] == "slack:C0A:1700000000.000100"
    assert info["first_ts"] == "2026-06-03T07:00:00Z"          # parent
    assert info["last_activity_ts"] == "2026-06-03T07:05:00Z"  # reply
    # alice authored the parent, bob the reply (slack U-ids).
    assert info["actor_counts"].get("U0ALICE") == 1
    assert info["actor_counts"].get("U0BOB") == 1
    assert "payout" in (info["content"] or "").lower()


def test_cluster_payload_on_seed(seeded_db):
    actor_map = {"U0ALICE": "alice", "U0BOB": "bob"}
    p = ec._cluster_payload(seeded_db, 1, actor_map, member_chars=600)
    assert p["cluster_id"] == 1 and p["label"] == "Payout outage"
    assert p["first_ts"] == "2026-06-03T07:00:00Z"
    assert p["last_activity_ts"] == "2026-06-03T07:05:00Z"
    # participants resolved to canonical names.
    assert p["participants_observed"].get("alice") == 1
    assert p["participants_observed"].get("bob") == 1
    assert [b["subject"] for b in p["members"]] == ["slack:C0A:1700000000.000100"]


def test_cluster_payload_missing(seeded_db):
    assert ec._cluster_payload(seeded_db, 999, {}, member_chars=600) == {}


def test_cluster_payload_unmapped_actor_kept_raw(seeded_db, monkeypatch):
    # empty actor_map → ids fall back to <raw:…> rather than dropping.
    p = ec._cluster_payload(seeded_db, 1, {}, member_chars=600)
    assert any(k.startswith("<raw:U0") for k in p["participants_observed"])

"""derive/finalize_refresh.py — per-cluster label/enrich payload builder.

_cluster_payload assembles the context (members, sampled content, observed
participants) the chat-labeling pass consumes for one cluster. Run against the
seed's incident cluster. _resolve_cluster_ids (the --cluster-ids / plan-file
selector) is pinned for the explicit-ids path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from derive import finalize_refresh as fr


def test_resolve_cluster_ids_explicit():
    args = SimpleNamespace(cluster_ids=["1", "2", "3"])
    assert fr._resolve_cluster_ids(args) == [1, 2, 3]


def test_cluster_payload_on_seed(seeded_db):
    actor_map = {"U0ALICE": "alice", "U0BOB": "bob"}
    payload = fr._cluster_payload(seeded_db, 1, actor_map)
    assert payload["cluster_id"] == 1
    assert payload["existing_label"] == "Payout outage"
    assert payload["existing_status"] == "RESOLVED"
    # the slack thread is the cluster's member; alice authored it.
    subjects = {m["subject"] for m in payload["members"]}
    assert "slack:C0A:1700000000.000100" in subjects
    assert "alice" in payload["participants_observed"]


def test_cluster_payload_missing_cluster(seeded_db):
    assert fr._cluster_payload(seeded_db, 999, {}) == {}

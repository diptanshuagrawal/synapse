"""derive/person_v3.py — track classification + window helpers.

person_v3 decides a person's window work-mix (feature / platform / ops / mixed)
and their workstreams. The discriminating logic is _classify_track (pure) and
the conn-taking probes (_workstreams, _baseline_role, _review_concentration);
build_v3 itself fans out to other modules that open their own DB, so it's left
to integration. Helpers are exercised against the seeded DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_DERIVE = Path(__file__).resolve().parent.parent / "derive"
if str(_DERIVE) not in sys.path:
    sys.path.insert(0, str(_DERIVE))

from derive import person_v3 as pv  # noqa: E402

SINCE, UNTIL = "2026-05-01T00:00:00Z", "2026-06-30T00:00:00Z"
ALICE = ["alice-gh", "alice@example.com", "acc-alice", "U0ALICE"]


# ── _classify_track (pure) ───────────────────────────────────────────────────

def test_classify_track_feature():
    track, _ = pv._classify_track({"pr_work": 3}, dom_owned=2)
    assert track == "feature"


def test_classify_track_platform():
    track, _ = pv._classify_track({"design": 3, "cmr_ops": 2, "pr_work": 0})
    assert track == "platform"


def test_classify_track_ops():
    track, _ = pv._classify_track({"incident": 4, "pr_work": 0})
    assert track == "ops"


def test_classify_track_delivery_only_is_mixed():
    track, basis = pv._classify_track({}, dom_owned=0)
    assert track == "mixed" and "delivery-only" in basis


def test_classify_track_close_scores_mixed():
    # feature=2, platform=2 → within 1 → mixed.
    track, _ = pv._classify_track({"pr_work": 2, "design": 2})
    assert track == "mixed"


# ── _workstreams (seed) ──────────────────────────────────────────────────────

def test_workstreams_groups_by_cluster(seeded_db):
    subs = {"slack:C0A:1700000000.000100"}  # alice's clustered subject
    out = pv._workstreams(seeded_db, ALICE, SINCE, UNTIL, subs)
    assert len(out) == 1 and out[0]["cluster_id"] == 1


def test_workstreams_empty_inputs():
    # no aliases / no subjects → empty, no query.
    assert pv._workstreams(None, [], SINCE, UNTIL, set()) == []


# ── _baseline_role (seed) ────────────────────────────────────────────────────

def test_baseline_role_returns_role_and_basis(seeded_db):
    role, basis = pv._baseline_role(seeded_db, ALICE, UNTIL)
    assert role in ("feature", "platform", "ops", "mixed")
    assert isinstance(basis, str)


# ── _review_concentration (seed) ─────────────────────────────────────────────

def test_review_concentration_none_without_clustered_reviews(seeded_db):
    # alice has no review events on clustered subjects → graceful None.
    assert pv._review_concentration(seeded_db, ALICE, SINCE, UNTIL) is None


def test_review_concentration_none_for_empty_aliases(seeded_db):
    assert pv._review_concentration(seeded_db, [], SINCE, UNTIL) is None

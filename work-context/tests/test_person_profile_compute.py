"""derive/person_profile.py — the compute_* signal builders (seed-driven).

person_profile is the per-person signal engine; these compute_* functions are
its bulk (contribution, behavioral, throughput, quality, velocity, PR/ticket
fate, lookahead, narrative signals). They run against the seeded DB with a
stubbed tier-config (passed as a param or via a work-hours stub), so they
exercise the real query/aggregation paths offline — no live config, no network.
Assertions are type+shape (these are aggregators); the point is to run the paths.
"""

from __future__ import annotations

import sys
from datetime import timezone
from pathlib import Path

import pytest

# Some compute_* use bare sibling imports (`import jira_metrics`), so derive/
# must be on sys.path — mirrors the runtime.
_DERIVE = Path(__file__).resolve().parent.parent / "derive"
if str(_DERIVE) not in sys.path:
    sys.path.insert(0, str(_DERIVE))

from derive import person_profile as pp

ALICE = ["alice-gh", "alice@example.com", "acc-alice", "U0ALICE"]
ALIAS_LOWER = {a.lower() for a in ALICE}
SINCE, UNTIL = "2026-05-01T00:00:00Z", "2026-06-30T00:00:00Z"

# Generic tier config (thresholds only — no org identity), mirrors the real
# config/tier_expectations.yaml shape the compute_* functions read.
TIER_CFG = {
    "sprint": {"working_days": 10, "default_sp_per_sprint": 10, "sprints_per_month": 2},
    "window": {"lookahead_days": 30, "lookbehind_days": 0, "fate_max_days": 90},
    "status_classes": {
        "shipped": ["Done", "Released"], "ops_closed": ["Review Complete"],
        "cancelled": ["Cancelled", "Rolled Back"],
        "in_flight": ["To Do", "Open", "In Progress", "In Review"],
    },
    "reliability_gates": {"sp_coverage_min": 0.7, "cmr_share_threshold": 0.3,
                          "min_window_sprints": 1, "min_sprinted_tickets_for_verdict": 5},
    "ops_band": {"cmrs_closed_per_sprint_low": 3, "cmrs_closed_per_sprint_high": 6},
    "quality": {"matterai_code_quality_target_pct": 85,
                "bugs_per_quarter_warn": {"SDE2": 2}, "reverts_per_quarter_warn": 1},
    "tiers": {"SDE2": {"sp_efficiency_low": 0.7, "sp_efficiency_high": 0.8}},
}
CLASSES = TIER_CFG["status_classes"]


@pytest.fixture(autouse=True)
def _work_hours(monkeypatch):
    monkeypatch.setattr(pp, "_load_work_hours",
                        lambda: {"tz": timezone.utc, "start_hour": 9, "end_hour": 18})


# ── config-free aggregators ──────────────────────────────────────────────────

def test_compute_contribution(seeded_db):
    out = pp.compute_contribution(seeded_db, ALICE, SINCE, UNTIL)
    assert isinstance(out, dict)
    # alice authored PR + story/epic + thread in window → authorship signal present.
    assert any(isinstance(v, int) for v in out.values())


def test_compute_behavioral(seeded_db):
    out = pp.compute_behavioral(seeded_db, ALICE, SINCE, UNTIL)
    assert isinstance(out, dict)


def test_compute_quality(seeded_db):
    out = pp.compute_quality(seeded_db, ALICE, SINCE, UNTIL)
    assert isinstance(out, dict)


def test_compute_pr_fate(seeded_db):
    out = pp.compute_pr_fate(seeded_db, ALICE, SINCE, UNTIL, fate_max_days=90)
    assert isinstance(out, list)
    # alice opened org/repo#10 (merged) → one fate row, merged outcome.
    assert any(r.get("subject") == "org/repo#10" for r in out)


def test_compute_narrative_signals(seeded_db):
    out = pp.compute_narrative_signals(seeded_db, "alice", ALICE, SINCE, UNTIL)
    assert isinstance(out, dict)


def test_compute_lookahead_ownership(seeded_db):
    out = pp.compute_lookahead_ownership(seeded_db, ALICE, SINCE, UNTIL, lookahead_days=30)
    assert isinstance(out, list)


# ── classes-param aggregators ────────────────────────────────────────────────

def test_compute_velocity(seeded_db):
    out = pp.compute_velocity(seeded_db, ALICE, ALIAS_LOWER, SINCE, UNTIL, CLASSES)
    assert isinstance(out, dict)


def test_compute_ticket_fate(seeded_db):
    out = pp.compute_ticket_fate(seeded_db, ["EX-2301"], SINCE, UNTIL,
                                 lookahead_days=30, classes=CLASSES)
    assert isinstance(out, dict)


# ── tier_cfg aggregators ─────────────────────────────────────────────────────

def test_compute_throughput(seeded_db):
    out = pp.compute_throughput(seeded_db, ALICE, ALIAS_LOWER, "alice", "SDE2",
                                SINCE, UNTIL, TIER_CFG)
    assert isinstance(out, dict)


def test_compute_lookahead_throughput(seeded_db):
    out = pp.compute_lookahead_throughput(seeded_db, ALICE, ALIAS_LOWER, "alice", "SDE2",
                                          SINCE, UNTIL, TIER_CFG, lookahead_days=30)
    assert isinstance(out, dict)

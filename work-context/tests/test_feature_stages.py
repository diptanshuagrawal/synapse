"""derive/feature_stages.py — feature lifecycle stage detection.

compute_stages derives the planning → trd → code_dev → rollout onsets from a
feature's artefacts. Driven against the seeded DB with a hand-built
FeatureArtefacts (epic EX-2238 → child story, a confluence page, a merged PR).
_fmt_date and _min_ts (the timestamp probe) are pinned too.
"""

from __future__ import annotations

import pytest

from derive import feature_stages as fs
from derive.feature_resolve import FeatureArtefacts


def _fa():
    return FeatureArtefacts(
        slug="payments", name="Payments",
        epics=["EX-2238"], jira=["EX-2301"], github=["org/repo#10"],
        confluence=["page:123456789"], slack=[],
        declared_confluence=[], release_cmrs=[], mode="slug")


# ── _fmt_date (pure) ─────────────────────────────────────────────────────────

def test_fmt_date():
    assert fs._fmt_date("2026-06-10T12:00:00Z") == "2026-06-10"
    assert fs._fmt_date(None) == "—"


# ── _min_ts (seed) ───────────────────────────────────────────────────────────

def test_min_ts_picks_earliest(seeded_db):
    ts = fs._min_ts(seeded_db, ["EX-2238"], ["issue_created"])
    assert ts == "2026-06-01T09:00:00Z"


def test_min_ts_empty_subjects():
    assert fs._min_ts(None, [], ["issue_created"]) is None


def test_min_ts_event_type_filter(seeded_db):
    # no pr_merged on a jira subject → None
    assert fs._min_ts(seeded_db, ["EX-2238"], ["pr_merged"]) is None


# ── compute_stages (seed) ────────────────────────────────────────────────────

def test_compute_stages_detects_planning_trd_codedev(seeded_db):
    stages = fs.compute_stages(seeded_db, _fa())
    by_stage = {s["stage"]: s for s in stages}
    assert "planning" in by_stage and "trd" in by_stage and "code_dev" in by_stage
    # planning onset = the epic's creation ts.
    assert by_stage["planning"]["entered_at"] == "2026-06-01T09:00:00Z"
    # no release CMRs in the seed → no rollout stage.
    assert "rollout" not in by_stage


def test_compute_stages_sorted_by_lifecycle_order(seeded_db):
    stages = fs.compute_stages(seeded_db, _fa())
    order = [s["stage"] for s in stages]
    assert order == sorted(order, key=fs.STAGE_ORDER.index)

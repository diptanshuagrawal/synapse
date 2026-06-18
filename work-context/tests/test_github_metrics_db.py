"""derive/github_metrics.py — DB-driven friction + aggregation (seed).

Companion to test_github_metrics.py (pure predicates). Drives the friction
pipeline against the seed's full PR lifecycle (org/repo#10: opened by alice,
APPROVED review by bob, merged; pr_meta row with sizes). load_people_lookup /
team_canonicals are stubbed where the team filter is exercised, since the real
ones read the live people.yaml.
"""

from __future__ import annotations

import pytest

from derive import github_metrics as gm

PR = "org/repo#10"
LOOKUP = {"alice-gh": "alice", "bob-gh": "bob"}


def _pr_row(conn):
    return conn.execute("SELECT * FROM pr_meta WHERE subject=?", (PR,)).fetchone()


# ── basic probes ─────────────────────────────────────────────────────────────

def test_pr_author(seeded_db):
    assert gm.pr_author(seeded_db, PR) == "alice-gh"


def test_first_human_review_ts_is_bobs_review(seeded_db):
    ts = gm.first_human_review_ts(seeded_db, PR, "alice-gh")
    assert ts is not None and ts.isoformat().startswith("2026-06-03T08:00")


# ── mechanical_signals ───────────────────────────────────────────────────────

def test_mechanical_signals(seeded_db):
    m = gm.mechanical_signals(seeded_db, _pr_row(seeded_db))
    assert m["author"] == "alice-gh"
    assert m["review_rounds"] == 1          # bob's one review
    assert m["changes_requested"] == 0      # it was APPROVED
    assert m["rework_commits"] == 0
    assert m["additions"] == 40
    assert m["ttm_hours"] == pytest.approx(44.0)  # 06-02T12 → 06-04T08


# ── compute_friction (clean PR) ──────────────────────────────────────────────

def test_compute_friction_clean(seeded_db):
    f = gm.compute_friction(seeded_db, _pr_row(seeded_db))
    assert f["subject"] == PR
    assert f["score"] == 0.0                # no changes-requested / rework / slow / comments
    assert f["dominant_category"] == "clean"


# ── merged_prs ───────────────────────────────────────────────────────────────

def test_merged_prs_team_agnostic(seeded_db):
    rows = gm.merged_prs(seeded_db, team_only=False)
    assert {r["subject"] for r in rows} == {PR}


def test_merged_prs_team_only(seeded_db, monkeypatch):
    monkeypatch.setattr(gm, "load_people_lookup", lambda: LOOKUP)
    monkeypatch.setattr(gm, "team_canonicals", lambda: {"alice", "bob"})
    rows = gm.merged_prs(seeded_db, team_only=True)
    assert {r["subject"] for r in rows} == {PR}   # alice resolves to team


# ── aggregate_by_dev ─────────────────────────────────────────────────────────

def test_aggregate_by_dev(seeded_db, monkeypatch):
    monkeypatch.setattr(gm, "load_people_lookup", lambda: LOOKUP)
    agg = gm.aggregate_by_dev(seeded_db, team_only=False)
    assert agg["alice"]["prs"] == 1 and agg["alice"]["avg_score"] == 0.0


# ── category_counts / coverage_gap (empty until classified) ──────────────────

def test_category_counts_empty(seeded_db):
    assert gm.category_counts(seeded_db, PR) == {}


def test_coverage_gap_empty(seeded_db):
    assert gm.coverage_gap(seeded_db, team_only=False) == {}


# ── populate_pr_friction (writes pr_friction) ────────────────────────────────

def test_populate_pr_friction(seeded_db):
    n = gm.populate_pr_friction(seeded_db, team_only=False)
    assert n == 1
    row = seeded_db.execute(
        "SELECT score, dominant_category FROM pr_friction WHERE subject=?", (PR,)).fetchone()
    assert row is not None and row[1] == "clean"

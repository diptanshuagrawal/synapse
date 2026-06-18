"""derive/person_profile.py — pure helpers + signal regexes.

person_profile's compute_* are heavy DB orchestration (they open their own DB);
pinned here are the pure pieces every one of them leans on: identity resolution,
time-window classification (after-hours / weekend in the configured window),
percentile + status taxonomy, and the MatterAI / rectify / assignment /
resolution regexes that drive quality + credit signals.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from derive import person_profile as pp


# ── _resolve_canonical / _parse_iso ──────────────────────────────────────────

def test_resolve_canonical_substring():
    pm = {"alice": {}, "bob": {}}
    assert pp._resolve_canonical("Alice", pm) == "alice"
    assert pp._resolve_canonical("nobody", pm) is None


def test_parse_iso():
    assert pp._parse_iso("2026-06-10T12:00:00Z") is not None
    assert pp._parse_iso(None) is None
    assert pp._parse_iso("nonsense") is None


# ── _is_after_hours / _is_weekend (windowed) ─────────────────────────────────

@pytest.fixture
def work_window(monkeypatch):
    monkeypatch.setattr(pp, "_load_work_hours",
                        lambda: {"tz": timezone.utc, "start_hour": 9, "end_hour": 18})


def test_is_after_hours(work_window):
    assert pp._is_after_hours(datetime(2026, 6, 10, 8, tzinfo=timezone.utc)) is True   # before 9
    assert pp._is_after_hours(datetime(2026, 6, 10, 12, tzinfo=timezone.utc)) is False  # midday
    assert pp._is_after_hours(datetime(2026, 6, 10, 19, tzinfo=timezone.utc)) is True   # after 18


def test_is_weekend(work_window):
    assert pp._is_weekend(datetime(2026, 6, 13, 12, tzinfo=timezone.utc)) is True   # Saturday
    assert pp._is_weekend(datetime(2026, 6, 10, 12, tzinfo=timezone.utc)) is False  # Wednesday


# ── _pctl / _classify_status / _ph / _add_days ───────────────────────────────

def test_pctl():
    assert pp._pctl([], 50) is None
    assert pp._pctl([1, 2, 3, 4], 50) == 3.0
    assert pp._pctl([1, 2, 3, 4], 100) == 4.0


def test_classify_status():
    classes = {"done": ["Done", "Closed"], "wip": ["In Progress"]}
    assert pp._classify_status("Done", classes) == "done"
    assert pp._classify_status("In Progress", classes) == "wip"
    assert pp._classify_status("Backlog", classes) == "other"
    assert pp._classify_status(None, classes) == "unknown"


def test_ph():
    assert pp._ph([1, 2, 3]) == "?,?,?"


def test_add_days():
    assert pp._add_days("2026-06-10", 2).startswith("2026-06-12")
    assert pp._add_days("2026-06-10T09:00:00Z", 1).startswith("2026-06-11")


# ── signal regexes ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,hit", [
    ("Fix payout rounding", True),
    ("Rectify ledger entry", True),
    ("data correction for X", True),
    ("Add new dashboard", False),
])
def test_rectify_rx(title, hit):
    assert bool(pp.RECTIFY_TITLE_RX.search(title)) is hit


def test_matterai_quality_rx():
    assert pp.MATTERAI_QUALITY_RX.search("Code_Quality-85%").group(1) == "85"
    assert pp.MATTERAI_QUALITY_RX.search("Code Quality: 92 %").group(1) == "92"


def test_matterai_critical_rx():
    assert pp.MATTERAI_CRITICAL_RX.search("Critical issues found in this PR")
    assert not pp.MATTERAI_CRITICAL_RX.search("looks clean")


def test_assign_rx():
    assert pp.ASSIGN_RX.search("assignee: ∅ → Alice Example").group(1).strip() == "Alice Example"


def test_resolution_rx():
    assert pp.RESOLUTION_RX.search("merged and deployed")
    assert pp.RESOLUTION_RX.search("see github.com/o/r/pull/3")
    assert not pp.RESOLUTION_RX.search("still investigating")

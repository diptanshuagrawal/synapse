"""derive/apply_leaves._validate + _valid_date — leave-verdict gatekeeper.

Guards what lands in team_leaves: rejects stale/low-confidence verdicts,
enforces boolean is_leave, off-team actors, malformed or inverted date ranges,
and normalises reason to the enum. Each rejection keeps a false-positive leave
out of the rendered team-leaves doc.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_DERIVE = Path(__file__).resolve().parent.parent / "derive"
if str(_DERIVE) not in sys.path:
    sys.path.insert(0, str(_DERIVE))

from derive import apply_leaves as al  # noqa: E402

TEAM = {"alice", "bob"}
PENDING = {"evt-1": {"channel_id": "C0A"}}


def _v(**kw):
    base = {"event_id": "evt-1", "confidence": 0.9, "is_leave": True,
            "leaves": [{"actor": "alice", "date_start": "2026-06-10",
                        "date_end": "2026-06-12", "reason": "vacation"}]}
    base.update(kw)
    return base


# ── _valid_date ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("s,ok", [
    (None, True), ("2026-06-10", True),
    ("2026/06/10", False), ("not-a-date", False), ("2026-13-40", False),
])
def test_valid_date(s, ok):
    assert al._valid_date(s) is ok


# ── rejection paths ──────────────────────────────────────────────────────────

def test_missing_event_id():
    cleaned, _ = al._validate({"confidence": 0.9}, PENDING, TEAM)
    assert cleaned is None


def test_stale_event_id():
    cleaned, errs = al._validate(_v(event_id="ghost"), PENDING, TEAM)
    assert cleaned is None and any("not in pending" in e for e in errs)


def test_low_confidence_stays_pending():
    cleaned, errs = al._validate(_v(confidence=0.5), PENDING, TEAM)
    assert cleaned is None and any("confidence" in e for e in errs)


def test_is_leave_must_be_bool():
    cleaned, _ = al._validate(_v(is_leave="yes"), PENDING, TEAM)
    assert cleaned is None


def test_is_leave_true_but_empty_leaves():
    cleaned, _ = al._validate(_v(leaves=[]), PENDING, TEAM)
    assert cleaned is None


def test_off_team_actor_rejected():
    cleaned, errs = al._validate(
        _v(leaves=[{"actor": "stranger", "date_start": None, "date_end": None}]),
        PENDING, TEAM)
    assert cleaned is None and any("not in team" in e for e in errs)


def test_inverted_date_range_rejected():
    cleaned, _ = al._validate(
        _v(leaves=[{"actor": "alice", "date_start": "2026-06-12",
                    "date_end": "2026-06-10"}]), PENDING, TEAM)
    assert cleaned is None


# ── accept paths ─────────────────────────────────────────────────────────────

def test_not_a_leave_marked_processed():
    cleaned, _ = al._validate(_v(is_leave=False, leaves=[]), PENDING, TEAM)
    assert cleaned["is_leave"] is False and cleaned["leaves"] == []


def test_unknown_reason_normalised_to_other():
    cleaned, _ = al._validate(
        _v(leaves=[{"actor": "alice", "date_start": None, "date_end": None,
                    "reason": "sabbatical"}]), PENDING, TEAM)
    assert cleaned["leaves"][0]["reason"] == "other"


def test_happy_path_cleaned():
    cleaned, errs = al._validate(_v(), PENDING, TEAM)
    assert errs == [] and cleaned["is_leave"] is True
    assert cleaned["leaves"][0]["actor"] == "alice"

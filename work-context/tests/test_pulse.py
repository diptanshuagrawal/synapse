"""derive/pulse.py — trend direction + goodness mapping (pure).

pulse's 1:1 report leans on _direction (relative change with a dead-band so
noise reads flat) and _trend (maps up/down → better/worse, respecting metrics
where up is bad — rank, latency, after-hours). Getting these wrong flips a
report's read of someone. Plus the small _g / _iso / _ph helpers.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from derive import pulse


# ── _direction (dead-band relative change) ───────────────────────────────────

@pytest.mark.parametrize("recent,prior,expected", [
    (12, 10, "up"),        # +20% > 10% tol
    (8, 10, "down"),       # -20%
    (10.5, 10, "flat"),    # +5% within dead-band
    (5, 0, "up"),          # from zero
    (0, 0, "flat"),
    (None, 10, "n/a"),
    ("x", 10, "n/a"),      # non-numeric
])
def test_direction(recent, prior, expected):
    assert pulse._direction(recent, prior) == expected


# ── _trend (direction → goodness, polarity-aware) ────────────────────────────

def test_trend_higher_is_better():
    assert pulse._trend("up", higher_is_better=True) == "better"
    assert pulse._trend("down", higher_is_better=True) == "worse"


def test_trend_lower_is_better():
    # e.g. latency / after-hours / rank — up is WORSE.
    assert pulse._trend("up", higher_is_better=False) == "worse"
    assert pulse._trend("down", higher_is_better=False) == "better"


def test_trend_passthrough():
    assert pulse._trend("flat", True) == "flat"
    assert pulse._trend("n/a", False) == "n/a"


# ── _g (safe nested get) ─────────────────────────────────────────────────────

def test_g_nested():
    d = {"a": {"b": {"c": 1}}}
    assert pulse._g(d, "a", "b", "c") == 1
    assert pulse._g(d, "a", "x", "c") is None
    assert pulse._g(d, "a", "b", "c", "d") is None   # bottoms out on a non-dict


# ── _iso / _ph ───────────────────────────────────────────────────────────────

def test_iso():
    assert pulse._iso(datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc)) == "2026-06-10T09:00:00Z"


def test_ph():
    assert pulse._ph(["a", "b", "c"]) == "?,?,?"

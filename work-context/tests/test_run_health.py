"""bin/_run_health.py — ingest-overrun detection math.

These are the pure functions cron-status + dashboard use to decide whether an
ingest run is about to collide with its next scheduled fire (the failure mode
that silently killed Slack sweeps before the interval was widened). The
80%/100%-of-interval thresholds are the whole point — test them at the
boundaries.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

# _run_health lives in bin/ and is imported by name (no package), mirroring
# how cron-status.sh puts bin/ on sys.path.
_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import _run_health as rh  # noqa: E402


# ── fire_interval_min ──────────────────────────────────────────────────────

def test_fire_interval_tightest_gap():
    # 11:00, 18:00, 18:30, 19:00 → tightest intra-day gap = 30 min.
    assert rh.fire_interval_min([660, 1080, 1110, 1140]) == 30


def test_fire_interval_single_fire_is_none():
    assert rh.fire_interval_min([660]) is None


def test_fire_interval_dedups_and_sorts():
    assert rh.fire_interval_min([1140, 660, 660, 1080]) == 60


# ── parse_log_ts ───────────────────────────────────────────────────────────

def test_parse_log_ts_with_millis():
    dt = rh.parse_log_ts("2026-06-10 11:00:05,123")
    assert dt == datetime(2026, 6, 10, 11, 0, 5)


def test_parse_log_ts_garbage_is_none():
    assert rh.parse_log_ts("not-a-timestamp") is None
    assert rh.parse_log_ts("") is None


# ── run_duration_min ───────────────────────────────────────────────────────

def test_run_duration_positive():
    d = rh.run_duration_min("2026-06-10 11:00:00", "2026-06-10 11:09:00")
    assert d == 9.0


def test_run_duration_negative_is_none():
    # done before start (clock skew / log interleave) → not computable.
    assert rh.run_duration_min("2026-06-10 11:09:00", "2026-06-10 11:00:00") is None


def test_inflight_duration_uses_now():
    now = datetime(2026, 6, 10, 11, 30, 0)
    assert rh.inflight_duration_min("2026-06-10 11:00:00", now=now) == 30.0


# ── overrun_verdict thresholds ─────────────────────────────────────────────

def test_verdict_below_warn_is_none():
    # 79% of interval → fine.
    assert rh.overrun_verdict(47.4, 60) is None


def test_verdict_warn_at_80_pct():
    v = rh.overrun_verdict(48, 60)  # exactly 80%
    assert v["level"] == "warn" and v["symbol"] == "!"


def test_verdict_fail_at_100_pct():
    v = rh.overrun_verdict(60, 60)  # exactly 100% → overrun
    assert v["level"] == "fail" and v["symbol"] == "x"
    assert v["pct"] == 100


def test_verdict_in_flight_label():
    v = rh.overrun_verdict(70, 60, in_flight=True)
    assert v["in_flight"] is True
    assert "running" in v["label"].lower()


def test_verdict_none_when_no_interval():
    assert rh.overrun_verdict(100, None) is None
    assert rh.overrun_verdict(None, 60) is None

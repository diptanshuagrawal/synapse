"""derive/build_thread_summary.py + mom_extractor.py — pure detectors.

- _detect_ops_pattern: first-matching ops label (incident/drill/rca/year_end/
  rollback) over a thread's text.
- _is_mom_title / _slack_permalink: MoM-title gate (with anti-patterns) and the
  slack-permalink builder.
"""

from __future__ import annotations

import pytest

from derive import build_thread_summary as bts
from derive import mom_extractor as mom


# ── _detect_ops_pattern ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text,label", [
    ("P1 outage in prod", "incident"),
    ("running a DR-drill today", "drill"),
    ("RCA for the payout bug", "rca"),
    ("year-end freeze starts", "year_end"),
    ("pushed a hotfix rollback", "rollback"),
])
def test_detect_ops_pattern(text, label):
    assert bts._detect_ops_pattern(text) == label


def test_detect_ops_pattern_none():
    assert bts._detect_ops_pattern("just a normal feature discussion") is None
    assert bts._detect_ops_pattern("") is None


def test_detect_ops_pattern_first_wins():
    # incident is earlier in OPS_PATTERNS than rollback → incident wins.
    assert bts._detect_ops_pattern("incident: had to rollback") == "incident"


# ── _is_mom_title ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,is_mom", [
    ("MoM: weekly sync", True),
    ("TL;DR - MoM of the standup", True),
    ("Minutes of the design review", True),
    ("*MoM: payments sync", True),          # leading * stripped
    ("please join the MoM call", False),    # anti-pattern
    ("any mom?", False),                    # anti-pattern
    ("random channel chatter", False),
    ("", False),
])
def test_is_mom_title(title, is_mom):
    assert mom._is_mom_title(title) is is_mom


# ── _slack_permalink ─────────────────────────────────────────────────────────

def test_slack_permalink(monkeypatch):
    monkeypatch.setattr(mom, "slack_workspace", lambda: "acme")
    url = mom._slack_permalink("slack:C0ABC:1700000000.000100")
    assert url == "https://acme.slack.com/archives/C0ABC/p1700000000000100"


def test_slack_permalink_malformed():
    assert mom._slack_permalink("not-a-slack-subject") == ""


# ── collect_moms on empty corpus ─────────────────────────────────────────────

def test_collect_moms_empty(db_conn):
    assert mom.collect_moms(db_conn, "2026-06-01T00:00:00Z", "2026-06-30T00:00:00Z",
                            channels={"C0ABC"}) == []

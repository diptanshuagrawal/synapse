"""derive/github_metrics.py — pure PR-signal helpers.

The friction scorer and aggregations are DB-driven (covered by their callers);
pinned here are the pure predicates that gate everything upstream: bot
detection, team-author resolution, timestamp parsing, and review-state parsing
out of the canonical 'Review on #N: STATE' title.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from derive import github_metrics as gm


# ── is_bot ─────────────────────────────────────────────────────────────────

def test_is_bot():
    assert gm.is_bot("github-actions[bot]") is True
    assert gm.is_bot("alice-gh") is False
    assert gm.is_bot(None) is False


# ── is_team_author ───────────────────────────────────────────────────────────

def test_is_team_author_resolves_to_team():
    lookup = {"alice-gh": "alice", "ext-gh": "ext-person"}
    team = {"alice"}
    assert gm.is_team_author("alice-gh", lookup, team) is True
    assert gm.is_team_author("ext-gh", lookup, team) is False    # resolved but not team
    assert gm.is_team_author("ghost", lookup, team) is False     # unresolved


def test_is_team_author_case_insensitive():
    lookup = {"alice-gh": "alice"}
    assert gm.is_team_author("ALICE-GH", lookup, {"alice"}) is True


def test_is_team_author_none():
    assert gm.is_team_author(None, {}, set()) is False


# ── _parse_ts ─────────────────────────────────────────────────────────────

def test_parse_ts_z_suffix():
    dt = gm._parse_ts("2026-06-10T12:00:00Z")
    assert dt == datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


def test_parse_ts_none_and_garbage():
    assert gm._parse_ts(None) is None
    assert gm._parse_ts("not-a-ts") is None


# ── _review_state ─────────────────────────────────────────────────────────

def test_review_state_parses_and_uppercases():
    assert gm._review_state("Review on #12: approved") == "APPROVED"
    assert gm._review_state("Review on #12: CHANGES_REQUESTED") == "CHANGES_REQUESTED"


def test_review_state_no_colon_is_empty():
    assert gm._review_state("Review on #12") == ""
    assert gm._review_state(None) == ""

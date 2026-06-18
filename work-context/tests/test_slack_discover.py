"""derive/slack_discover_channels.py — channel-mode decision tree + helpers.

The auto-discovery scorer that decides each channel's ingest_mode. _decide_mode
is the core tree (skip / alert-bypass / activity-floor / MPIM / bot-name /
announce / team-ratio); the name helpers (_slugify, _channel_kind,
_mpim_team_count, _is_alert_channel, _name_has_team_domain) feed it. All pure.
"""

from __future__ import annotations

import pytest

from derive import slack_discover_channels as sd


# ── small helpers ────────────────────────────────────────────────────────────

def test_slugify():
    assert sd._slugify("  Eng Discuss ") == "eng-discuss"


@pytest.mark.parametrize("meta,kind", [
    ({"is_im": True}, "DM"),
    ({"is_mpim": True}, "MPIM"),
    ({"is_private": True}, "private"),
    ({}, "public"),
])
def test_channel_kind(meta, kind):
    assert sd._channel_kind(meta) == kind


def test_mpim_team_count_exact_and_truncated():
    team = {"alice", "bob", "carol.example"}
    # exact handles + a Slack-truncated "carol" prefix-matches carol.example.
    assert sd._mpim_team_count("mpdm-alice--bob--carol-1", team) == 3
    assert sd._mpim_team_count("not-an-mpim", team) == 0


def test_is_alert_channel():
    assert sd._is_alert_channel("service-a-alerts", 0.0) is True       # name token
    assert sd._is_alert_channel("random-chat", 0.95) is True           # bot-dominated
    assert sd._is_alert_channel("random-chat", 0.1) is False


# ── _decide_mode tree ────────────────────────────────────────────────────────

def test_decide_skip_dm():
    v, _ = sd._decide_mode({"is_im": True, "name": "x"}, set(), 0, 0, 0)
    assert v == "skip"


def test_decide_alert_channel_bypasses_floor():
    # bot-dominated + team-domain name → auto_full even with ~0 team msgs.
    meta = {"name": "accounting-alerts"}
    v, extras = sd._decide_mode(meta, set(), team_msgs=0, total_msgs=500,
                                mpim_team_count=0, bot_ratio=0.95)
    assert v == "auto_full" and extras["mode"] == "full" and extras["no_threads"] is True


def test_decide_below_floor_needs_review():
    v, _ = sd._decide_mode({"name": "quiet-chan"}, set(), team_msgs=2, total_msgs=100,
                           mpim_team_count=0)
    assert v == "needs_review"


def test_decide_mpim_with_enough_team_handles():
    v, extras = sd._decide_mode({"name": "mpdm-a--b--c-1", "is_mpim": True}, set(),
                                team_msgs=5, total_msgs=10, mpim_team_count=3)
    assert v == "auto_full" and extras["allow_mpim"] is True


def test_decide_bot_name_pattern():
    v, _ = sd._decide_mode({"name": "opsgenie-prod"}, set(), team_msgs=50,
                           total_msgs=100, mpim_team_count=0)
    assert v == "auto_team_involved"


def test_decide_announcement_name():
    v, _ = sd._decide_mode({"name": "all-hands"}, set(), team_msgs=50,
                           total_msgs=100, mpim_team_count=0)
    assert v == "auto_team_involved"


def test_decide_high_team_ratio_full():
    # team_msgs/total ≥ 0.5 → full.
    v, extras = sd._decide_mode({"name": "eng-payments"}, set(), team_msgs=60,
                                total_msgs=100, mpim_team_count=0)
    assert v == "auto_full" and "high team ratio" in extras["rationale"]


def test_decide_low_ratio_active_team_involved():
    v, _ = sd._decide_mode({"name": "eng-payments"}, set(), team_msgs=10,
                           total_msgs=100, mpim_team_count=0)
    assert v == "auto_team_involved"

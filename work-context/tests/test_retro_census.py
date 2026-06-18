"""derive/retro_census.py — work-signal classification detectors.

_signal_type is the structural router that buckets every event into delivery /
fix / incident / design / pr_work / noise / discussion etc. Structural signals
(jira issue_type, source, channel role, status→Done) take priority over phrase
matching. All inputs are args, so the whole routing tree is pinned here, plus
the ownership-class and rollout-tense helpers.
"""

from __future__ import annotations

import pytest

from derive import retro_census as rc


def _sig(title="", body="", source="slack", events=None, **kw):
    return rc._signal_type(title, body, source, events or set(), **kw)


# ── _has (pure) ──────────────────────────────────────────────────────────────

def test_has():
    assert rc._has("the wfh day", ["wfh", "ooo"]) == "wfh"
    assert rc._has("nothing matches", ["wfh"]) is None


# ── _signal_type: structural (jira issue_type) ───────────────────────────────

def test_signal_jira_epic_is_design():
    assert _sig("Big epic", source="jira", issue_type="Epic") == ("design", "jira-epic")


def test_signal_jira_bug_open_is_fix_done_is_delivery():
    assert _sig("bug", source="jira", issue_type="Bug", went_done=False)[0] == "fix"
    assert _sig("bug", source="jira", issue_type="Bug", went_done=True)[0] == "delivery"


def test_signal_jira_cmr():
    assert _sig("cmr", source="jira", issue_type="CMR", went_done=False)[0] == "cmr_ops"


def test_signal_jira_task_open_is_work():
    assert _sig("task", source="jira", issue_type="Task", went_done=False)[0] == "work"


# ── _signal_type: structural (source / channel) ──────────────────────────────

def test_signal_confluence_is_design():
    assert _sig("Design doc", source="confluence") == ("design", "confluence-page")


def test_signal_github_pr_is_pr_work():
    assert _sig("a PR", source="github", events={"pr_opened"}) == ("pr_work", "github-pr")


def test_signal_incident_channel():
    assert _sig("anything", channel_id="C1", incident_channels={"C1"}) == ("incident", "oncall-channel")


def test_signal_alert_channel():
    assert _sig("alert", channel_id="C2", alert_channels={"C2"}) == ("alert_auto", "alert-feed-channel")


# ── _signal_type: phrase routing ─────────────────────────────────────────────

def test_signal_ooo_is_noise():
    typ, ev = _sig(body="wfh today")
    assert typ == "noise" and ev.startswith("ooo:")


def test_signal_design_keyword():
    assert _sig("TRD for payments")[0] == "design"


def test_signal_channel_join_is_noise():
    assert _sig("alice has joined the channel")[0] == "noise"


def test_signal_incident_commander_topic_exception():
    # a noise topic-set that mentions Incident Commander routes to incident.
    typ, ev = _sig("set the channel topic to Incident Commander")
    assert typ == "incident"


def test_signal_unmatched_is_discussion():
    assert _sig("just chatting about lunch") == ("discussion", "")


# ── _ownership_class ─────────────────────────────────────────────────────────

def test_ownership_class():
    team_ids = {"sister-team"}
    assert rc._ownership_class(rc.HOME_TEAM, team_ids) == "team"
    assert rc._ownership_class("sister-team", team_ids) == "sister"
    assert rc._ownership_class(None, team_ids) == "external"
    assert rc._ownership_class("randos", team_ids) == "external"


# ── _rollout_confirmed (tense) ───────────────────────────────────────────────

def test_rollout_confirmed_tense():
    assert rc._rollout_confirmed("rolled out to prod", "") is True
    assert rc._rollout_confirmed("going live next week", "") is False
    assert rc._rollout_confirmed("some deployment notes", "") is None

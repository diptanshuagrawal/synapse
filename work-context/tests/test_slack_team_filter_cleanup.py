"""derive/slack_team_filter_cleanup.py — team-actor / team-mention predicates.

The cleanup pass deletes non-team-involved rows from team_involved channels;
_is_team_actor + _body_mentions_team decide what's kept. They handle the three
actor shapes (canonical / slack-id / JSON dict) and the mention forms. Pure.
"""

from __future__ import annotations

import pytest

from derive import slack_team_filter_cleanup as c

CANON = {"bob-example"}
SIDS = {"U0BOB"}
SUBTEAMS = {"S0ENG"}


# ── _is_team_actor (three shapes) ────────────────────────────────────────────

def test_is_team_actor_canonical():
    assert c._is_team_actor("bob-example", CANON, SIDS) is True


def test_is_team_actor_slack_id():
    assert c._is_team_actor("U0BOB", CANON, SIDS) is True


def test_is_team_actor_json_dict():
    assert c._is_team_actor('{"id": "U0BOB", "name": "x"}', CANON, SIDS) is True
    assert c._is_team_actor('{"id": "U0X", "name": "bob-example"}', CANON, SIDS) is True


def test_is_team_actor_negative():
    assert c._is_team_actor("stranger", CANON, SIDS) is False
    assert c._is_team_actor(None, CANON, SIDS) is False
    assert c._is_team_actor("not json {", CANON, SIDS) is False


# ── _body_mentions_team ──────────────────────────────────────────────────────

def test_body_mentions_uid():
    assert c._body_mentions_team("ping <@U0BOB> pls", SIDS) is True
    assert c._body_mentions_team("<@U0BOB|bob> fyi", SIDS) is True


def test_body_mentions_subteam():
    assert c._body_mentions_team("<!subteam^S0ENG>", SIDS, SUBTEAMS) is True


def test_body_mentions_none():
    assert c._body_mentions_team("just chatting", SIDS, SUBTEAMS) is False
    assert c._body_mentions_team(None, SIDS) is False
    assert c._body_mentions_team("<!subteam^S0ENG>", SIDS) is False  # no subteams passed

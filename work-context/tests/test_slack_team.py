"""derive/slack_team.py — team-involvement filter for ingest_mode=team_involved.

is_team_involved decides whether a Slack message is kept under team_involved
mode via three signals: author is a team member, body @-mentions a team UID, or
body pings a team subteam. All inputs are args, so the match logic is pinned
directly (a regression here silently drops or floods team-channel ingest).
"""

from __future__ import annotations

import pytest

from derive import slack_team as st

TEAM = {"U0ALICE", "U0BOB"}
SUBTEAMS = {"S0ENG"}


def test_author_on_team():
    assert st.is_team_involved("U0ALICE", "hello", TEAM) is True


def test_author_not_on_team():
    assert st.is_team_involved("U0EXAMPLE", "hello", TEAM) is False


def test_mention_of_team_uid():
    assert st.is_team_involved("U0EXAMPLE", "ping <@U0BOB> please", TEAM) is True


def test_mention_with_name_form():
    assert st.is_team_involved("U0EXAMPLE", "<@U0ALICE|alice> fyi", TEAM) is True


def test_subteam_ping():
    assert st.is_team_involved("U0EXAMPLE", "heads up <!subteam^S0ENG>", TEAM, SUBTEAMS) is True


def test_subteam_ping_with_handle_form():
    assert st.is_team_involved("U0EXAMPLE", "<!subteam^S0ENG|@eng> review", TEAM, SUBTEAMS) is True


def test_subteam_skipped_when_not_provided():
    # legacy: no subteam set → subteam ping alone doesn't count.
    assert st.is_team_involved("U0EXAMPLE", "<!subteam^S0ENG>", TEAM) is False


def test_no_involvement():
    assert st.is_team_involved("U0EXAMPLE", "just chatting", TEAM, SUBTEAMS) is False


def test_none_actor_and_body():
    assert st.is_team_involved(None, None, TEAM, SUBTEAMS) is False

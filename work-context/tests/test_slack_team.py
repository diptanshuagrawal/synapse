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


# ── load_owner_slack_id ──────────────────────────────────────────────────────
# Resolves the owner's Slack UID via OWNER_EMAIL → people.yaml. Drives the
# owner-presence bypass in slack_discover_channels, so a regression here would
# silently disable owner-room discovery.

def _write_people(tmp_path, people):
    import yaml
    p = tmp_path / "people.yaml"
    p.write_text(yaml.safe_dump({"people": people}))
    return p


def test_owner_slack_id_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "OWNER_EMAIL", "owner@example.com")
    monkeypatch.setattr(st, "PEOPLE_YAML", _write_people(tmp_path, [
        {"email": "other@example.com", "slack_id": "U0OTHER"},
        {"email": "owner@example.com", "slack_id": "U0OWNER"},
    ]))
    assert st.load_owner_slack_id() == "U0OWNER"


def test_owner_slack_id_missing_mapping(tmp_path, monkeypatch):
    # Owner present but no slack_id → None (cannot resolve).
    monkeypatch.setattr(st, "OWNER_EMAIL", "owner@example.com")
    monkeypatch.setattr(st, "PEOPLE_YAML", _write_people(tmp_path, [
        {"email": "owner@example.com"},
    ]))
    assert st.load_owner_slack_id() is None


def test_owner_slack_id_not_in_file(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "OWNER_EMAIL", "owner@example.com")
    monkeypatch.setattr(st, "PEOPLE_YAML", _write_people(tmp_path, [
        {"email": "other@example.com", "slack_id": "U0OTHER"},
    ]))
    assert st.load_owner_slack_id() is None


def test_owner_slack_id_no_people_file(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "PEOPLE_YAML", tmp_path / "missing.yaml")
    assert st.load_owner_slack_id() is None


# ── roster = people.yaml scope:team (single source of truth, 2026-07-16) ────
# Membership used to come from a separate team.md roster kept in sync by hand;
# a dev added to one file but not the other got silent partial coverage.

PEOPLE_FIXTURE = [
    {"email": "owner@example.com", "slack_id": "U0OWNER", "canonical": "owner", "scope": "org"},
    {"email": "dev1@example.com", "slack_id": "U0DEV1", "canonical": "dev-one", "scope": "team"},
    {"email": "dev2@example.com", "slack_id": "U0DEV2", "canonical": "dev-two", "scope": "team"},
    {"email": "friend@example.com", "slack_id": "U0FRIEND", "canonical": "friend", "scope": "org"},
    {"email": "noslack@example.com", "canonical": "no-slack", "scope": "team"},
]


def test_team_emails_from_scope_team_plus_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "OWNER_EMAIL", "owner@example.com")
    monkeypatch.setattr(st, "PEOPLE_YAML", _write_people(tmp_path, PEOPLE_FIXTURE))
    assert st.load_team_emails() == {
        "owner@example.com", "dev1@example.com", "dev2@example.com", "noslack@example.com"}


def test_team_slack_ids_exclude_org_scope(tmp_path, monkeypatch):
    # org-scope collaborators are NOT roster; owner included regardless of scope.
    monkeypatch.setattr(st, "OWNER_EMAIL", "owner@example.com")
    monkeypatch.setattr(st, "PEOPLE_YAML", _write_people(tmp_path, PEOPLE_FIXTURE))
    ids = st.load_team_slack_ids()
    assert ids == {"U0OWNER": "owner", "U0DEV1": "dev-one", "U0DEV2": "dev-two"}


def test_team_loaders_empty_when_people_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "OWNER_EMAIL", "owner@example.com")
    monkeypatch.setattr(st, "PEOPLE_YAML", tmp_path / "missing.yaml")
    assert st.load_team_slack_ids() == {}
    assert st.load_team_emails() == {"owner@example.com"}  # owner always included

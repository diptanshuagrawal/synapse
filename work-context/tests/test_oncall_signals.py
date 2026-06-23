"""Shared on-call identity (derive/oncall_signals) + the @oncall-handle incident route.

Guards the 2026-06-23 fix: on-call work is detected by the @oncall HANDLE + class:oncall
channels org-wide (not alert-channel names), consistently across standup + census skills.
"""

from __future__ import annotations

from derive import oncall_signals as oc
from derive import retro_census as rc

TOK = "<!subteam^S0ONCALL"          # short fake id (avoids real-ID leak pattern)


def test_pings_oncall():
    assert oc.pings_oncall("hey <!subteam^S0ONCALL|@oncall> please look", [TOK])
    assert not oc.pings_oncall("no ping here", [TOK])
    assert not oc.pings_oncall(None, [TOK])
    assert not oc.pings_oncall("text", [])


def test_signal_type_oncall_handle_routes_to_incident():
    # A slack thread in a plain DOMAIN channel whose body pings @oncall → incident,
    # even though the channel is not in incident_channels (the GAP-1 fix).
    st, ev = rc._signal_type(
        "lien query", "can <!subteam^S0ONCALL|@oncall> check this account?", "slack",
        set(), channel_id="C0DOMAIN", incident_channels=set(), alert_channels=set(),
        oncall_tokens=[TOK])
    assert (st, ev) == ("incident", "oncall-handle")


def test_signal_type_without_handle_not_incident():
    st, _ = rc._signal_type(
        "general chat", "just discussing the schema", "slack", set(),
        channel_id="C0DOMAIN", incident_channels=set(), alert_channels=set(),
        oncall_tokens=[TOK])
    assert st != "incident"


def test_signal_type_no_tokens_is_safe():
    # No tokens passed (e.g. config absent) → handle route is inert, no crash.
    st, _ = rc._signal_type(
        "x", "<!subteam^S0ONCALL|@oncall>", "slack", set(),
        channel_id="C0DOMAIN", incident_channels=set(), alert_channels=set(),
        oncall_tokens=None)
    assert st != "incident"


def test_config_loaders_with_tmp_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "slack_channels.yaml").write_text(
        "channels:\n"
        "  - {id: C0ONCALL, name: some-bot-feed, class: oncall}\n"
        "  - {id: C0PLAIN, name: a-domain-channel}\n")
    (cfg / "team_subteams.yaml").write_text(
        "subteams:\n"
        "  - {id: S0HANDLE, handle: team-oncall}\n"
        "  - {id: S0DEV, handle: team-devs}\n")
    monkeypatch.setattr(oc, "_CHANNELS_YAML", cfg / "slack_channels.yaml")
    monkeypatch.setattr(oc, "_SUBTEAMS_YAML", cfg / "team_subteams.yaml")
    # class:oncall channel picked up by class, NOT name; plain domain channel excluded.
    assert oc.oncall_channel_ids() == {"C0ONCALL"}
    # only the oncall-named handle, not the dev handle.
    assert oc.oncall_handle_tokens() == ["<!subteam^S0HANDLE"]


def test_config_loaders_fail_soft(monkeypatch, tmp_path):
    # Absent config (public clone / CI) → empty, never crash.
    monkeypatch.setattr(oc, "_CHANNELS_YAML", tmp_path / "missing.yaml")
    monkeypatch.setattr(oc, "_SUBTEAMS_YAML", tmp_path / "missing.yaml")
    assert oc.oncall_channel_ids() == set()
    assert oc.oncall_handle_tokens() == []

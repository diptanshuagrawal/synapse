"""ingest/slack_api_client.py — Slack API message → ParsedMessage (no network).

Slack is the highest-volume, messiest source: bot messages hide content in
attachments/blocks, mentions arrive as raw <@U…>, files come as fat dicts, and
thread replies must be distinguished from parents. api_message_to_parsed is the
single funnel for all of it. The block/attachment recovery in particular guards
against the "empty body → similarity-1.0 cluster noise" failure mode.
"""

from __future__ import annotations

import json

import pytest

from ingest import slack_api_client as sac


# ── mention / subteam expansion ─────────────────────────────────────────────

def test_expand_mentions_via_cache():
    out = sac._expand_mentions("hi <@U0CAROL> there", {"U0CAROL": "carol"})
    assert out == "hi <@U0CAROL|carol> there"


def test_expand_mentions_unknown_left_raw():
    assert sac._expand_mentions("hi <@U0NOBODY>", {}) == "hi <@U0NOBODY>"


def test_expand_subteams():
    out = sac._expand_subteams("ping <!subteam^S123>", {"S123": "payments-oncall"})
    assert out == "ping <!subteam^S123|@payments-oncall>"


# ── attachment / block flattening (empty-text recovery) ─────────────────────

def test_flatten_attachments():
    msg = {"attachments": [{"pretext": "Alert", "title": "DB down",
                            "text": "connection refused",
                            "fallback": "connection refused",  # dup → deduped
                            "fields": [{"title": "Severity", "value": "P1"}]}]}
    out = sac._flatten_attachments_blocks(msg)
    assert "Alert" in out and "DB down" in out
    assert "connection refused" in out
    assert "Severity: P1" in out
    assert out.count("connection refused") == 1  # dedup


def test_flatten_blocks_nested_elements():
    msg = {"blocks": [
        {"type": "section", "text": {"type": "mrkdwn", "text": "header line"}},
        {"type": "rich_text", "elements": [
            {"type": "rich_text_section", "elements": [
                {"type": "text", "text": "nested deep"}]}]}]}
    out = sac._flatten_attachments_blocks(msg)
    assert "header line" in out and "nested deep" in out


# ── file struct + summary ────────────────────────────────────────────────────

def test_files_to_struct_keeps_whitelist_only():
    files = [{"id": "F1", "name": "diag.log", "mimetype": "text/plain",
              "size": 42, "url_private": "https://secret", "thumb_64": "x"}]
    out = json.loads(sac._files_to_struct(files))
    assert out[0]["name"] == "diag.log"
    assert "url_private" not in out[0] and "thumb_64" not in out[0]


def test_files_to_struct_empty_is_none():
    assert sac._files_to_struct([]) is None


def test_summarize_files_marks_tombstones():
    out = sac._summarize_files([{"name": "a.png"},
                                {"name": "old.txt", "mode": "tombstone"}])
    assert "[files: a.png, old.txt [deleted]]" in out


# ── api_message_to_parsed ────────────────────────────────────────────────────

def test_parse_user_message():
    msg = {"user": "U0CAROL", "ts": "1700000000.000100",
           "text": "hello <@U0ALICE>"}
    pm = sac.api_message_to_parsed(msg, users_cache={"U0CAROL": "carol", "U0ALICE": "alice"})
    assert pm.actor_id == "U0CAROL"
    assert pm.actor_name == "carol"
    assert pm.is_bot is False
    assert pm.body == "hello <@U0ALICE|alice>"
    assert pm.thread_parent_ts is None


def test_parse_bot_message_uses_username_and_block_recovery():
    msg = {"bot_id": "B0OPS", "username": "Opsgenie", "ts": "1700000000.000200",
           "text": "",  # bots leave text empty
           "attachments": [{"title": "Incident P1", "text": "service down"}]}
    pm = sac.api_message_to_parsed(msg)
    assert pm.is_bot is True
    assert pm.actor_id == "B0OPS" and pm.actor_name == "Opsgenie"
    assert "Incident P1" in pm.body and "service down" in pm.body


def test_parse_thread_reply_sets_parent():
    msg = {"user": "U0CAROL", "ts": "1700000050.000300",
           "thread_ts": "1700000000.000100", "text": "reply"}
    pm = sac.api_message_to_parsed(msg, users_cache={"U0CAROL": "carol"})
    assert pm.thread_parent_ts == "1700000000.000100"
    assert pm.reply_count is None  # replies don't carry a reply_count


def test_parse_parent_keeps_reply_count():
    msg = {"user": "U0CAROL", "ts": "1700000000.000100", "text": "root",
           "reply_count": 5, "thread_ts": "1700000000.000100"}  # thread_ts == ts → parent
    pm = sac.api_message_to_parsed(msg, users_cache={"U0CAROL": "carol"})
    assert pm.thread_parent_ts is None and pm.reply_count == 5


def test_parse_reactions_and_edited_and_files():
    msg = {"user": "U0CAROL", "ts": "1700000000.000400", "text": "x",
           "reactions": [{"name": "tada", "count": 3}],
           "edited": {"ts": "1700000001.0"},
           "files": [{"id": "F1", "name": "a.png", "mimetype": "image/png"}]}
    pm = sac.api_message_to_parsed(msg, users_cache={"U0CAROL": "carol"})
    assert json.loads(pm.reactions_json) == {"tada": 3}
    assert pm.edited is True
    assert "[files: a.png]" in pm.body
    assert json.loads(pm.files_json)[0]["name"] == "a.png"


def test_parse_unknown_user_falls_back_to_id_label():
    msg = {"user": "U0GHOST", "ts": "1700000000.000500", "text": "hi"}
    pm = sac.api_message_to_parsed(msg, users_cache={})
    assert pm.actor_name == "user-U0GHOST"

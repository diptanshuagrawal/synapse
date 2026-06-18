"""derive/slack_upsert.py — Slack id/subject builders + UPSERT (no network).

The id vs subject split is load-bearing: each reply gets a unique *id* but
shares the thread's *subject*, which is how the whole pipeline treats a thread
as one unit. upsert_event is idempotent on id and re-extracts refs on edit —
the steady-state ingest correctness contract. Exercised on a temp events.db.
"""

from __future__ import annotations

import pytest

from derive import slack_upsert as su
from derive.slack_upsert import ParsedMessage


# ── pure id / subject / ts / title / url builders ──────────────────────────

def test_event_id_top_level():
    assert su._event_id("C01", "1700.5", None) == "slack:C01:1700.5"


def test_event_id_reply():
    assert su._event_id("C01", "1779.6", "1700.5") == "slack:C01:1700.5:1779.6"


def test_event_id_self_parent_is_top_level():
    # thread_parent_ts == ts means it's the parent, not a reply.
    assert su._event_id("C01", "1700.5", "1700.5") == "slack:C01:1700.5"


def test_subject_collapses_thread():
    # parent and reply share ONE subject (the thread).
    assert su._subject("C01", "1700.5", None) == "slack:C01:1700.5"
    assert su._subject("C01", "1779.6", "1700.5") == "slack:C01:1700.5"


def test_ts_to_iso():
    assert su._ts_to_iso("1700000000.123456") == "2023-11-14T22:13:20.123456Z"


def test_title_from_body_first_line_truncated():
    assert su._title_from_body("line one\nline two") == "line one"
    assert su._title_from_body("x" * 300) == "x" * 200
    assert su._title_from_body("") == ""


def test_url_top_and_reply():
    top = su._url("C01", "1700000000.123456")
    assert ".slack.com/archives/C01/p1700000000123456" in top
    reply = su._url("C01", "1700000000.500000", "1700000000.123456")
    assert "thread_ts=1700000000.123456" in reply and "cid=C01" in reply


# ── upsert integration ──────────────────────────────────────────────────────

def _pm(**kw):
    base = dict(actor_id="U0CAROL", actor_name="carol", ts="1700000000.000100",
                body="hello team", is_bot=False, edited=False)
    base.update(kw)
    return ParsedMessage(**base)


def test_upsert_insert_then_unchanged(db_conn, patch_config):
    r1 = su.upsert_event(db_conn, _pm(), "C01",
                         slack_users_cache={"U0CAROL": "carol"})
    assert r1 == "inserted"
    r2 = su.upsert_event(db_conn, _pm(), "C01",
                         slack_users_cache={"U0CAROL": "carol"})
    assert r2 == "unchanged"
    n = db_conn.execute("SELECT COUNT(*) FROM events WHERE source='slack'").fetchone()[0]
    assert n == 1


def test_upsert_updates_on_edited_body(db_conn, patch_config):
    su.upsert_event(db_conn, _pm(), "C01")
    r = su.upsert_event(db_conn, _pm(body="hello team — corrected", edited=True), "C01")
    assert r == "updated"
    body = db_conn.execute(
        "SELECT body FROM events WHERE id='slack:C01:1700000000.000100'").fetchone()[0]
    assert "corrected" in body


def test_upsert_reply_distinct_id_shared_subject(db_conn, patch_config):
    su.upsert_event(db_conn, _pm(ts="1700000000.000100"), "C01")
    su.upsert_event(db_conn, _pm(ts="1700000050.000200"), "C01",
                    thread_parent_ts="1700000000.000100")
    rows = db_conn.execute(
        "SELECT id, subject, event_type FROM events WHERE source='slack' ORDER BY ts"
    ).fetchall()
    assert len(rows) == 2
    parent, reply = rows
    assert parent["event_type"] == "thread_started"
    assert reply["event_type"] == "thread_reply"
    # distinct ids, same subject (one thread).
    assert parent["id"] != reply["id"]
    assert parent["subject"] == reply["subject"] == "slack:C01:1700000000.000100"


def test_upsert_resolves_mention_ref(db_conn, patch_config):
    su.upsert_event(db_conn, _pm(body="cc <@U0CAROL>"), "C01",
                    slack_users_cache={"U0CAROL": "carol"})
    refs = {r[0] for r in db_conn.execute(
        "SELECT ref_value FROM event_refs WHERE ref_type='person'")}
    assert "carol" in refs


# ── uncovered branches: cursor, DM detection, reactions/reply_count updates ──

def test_extract_cursor():
    assert su.extract_cursor("End of results") is None
    assert su.extract_cursor("") is None


def test_is_dm_channel_dict():
    assert su.is_dm_channel({"is_im": True}) is True
    assert su.is_dm_channel({"is_mpim": True}) is True
    assert su.is_dm_channel({"is_private": True, "is_im": False}) is False


def test_upsert_reactions_update_without_body_change(db_conn, patch_config):
    su.upsert_event(db_conn, _pm(), "C01")
    # same body, new reactions → 'updated' (reactions refreshed silently).
    r = su.upsert_event(db_conn, _pm(reactions_json='{"tada": 2}'), "C01")
    assert r == "updated"
    rj = db_conn.execute(
        "SELECT reactions_json FROM events WHERE id='slack:C01:1700000000.000100'").fetchone()[0]
    assert rj == '{"tada": 2}'


def test_upsert_reply_count_grows_without_body_change(db_conn, patch_config):
    su.upsert_event(db_conn, _pm(reply_count=1), "C01")
    r = su.upsert_event(db_conn, _pm(reply_count=5), "C01")   # only reply_count changed
    assert r == "updated"
    rc = db_conn.execute(
        "SELECT reply_count FROM events WHERE id='slack:C01:1700000000.000100'").fetchone()[0]
    assert rc == 5


# ── parse_mcp_messages (the MCP text parser) ─────────────────────────────────

def test_parse_mcp_empty():
    assert su.parse_mcp_messages("") == []
    assert su.parse_mcp_messages("no headers here") == []


def test_parse_mcp_channel_format():
    text = (
        "=== Message from Alice (U0ALICE) at 2026-06-10 09:00 ===\n"
        "Message TS: 1700000000.000100\n"
        "deploying the payout fix now\n"
        "Reactions: tada (2)\n"
        "\n"
        "=== Message from Bob (U0BOB) at 2026-06-10 09:05 (edited) ===\n"
        "Message TS: 1700000050.000200\n"
        "lgtm\n"
    )
    msgs = su.parse_mcp_messages(text)
    assert len(msgs) == 2
    assert msgs[0].actor_id == "U0ALICE" and "payout fix" in msgs[0].body
    assert msgs[0].reactions_json is not None        # reactions line parsed out of body
    assert "Reactions:" not in msgs[0].body
    assert msgs[1].edited is True                    # (edited) marker on the header


def test_parse_mcp_thread_format():
    text = (
        "=== THREAD PARENT MESSAGE ===\n"
        "From: Alice (U0ALICE)\n"
        "Time: 2026-06-10 09:00\n"
        "Message TS: 1700000000.000100\n"
        "prod is down\n"
        "Thread: 1 reply\n"
        "--- Reply 1 of 1 ---\n"
        "From: Bob (U0BOB)\n"
        "Time: 2026-06-10 09:02\n"
        "Message TS: 1700000050.000200\n"
        "on it, rolling back\n"
    )
    msgs = su.parse_mcp_messages(text)
    assert [m.actor_id for m in msgs] == ["U0ALICE", "U0BOB"]
    assert msgs[0].reply_count == 1                  # "Thread: 1 reply" captured
    assert "Thread: 1 reply" not in msgs[0].body
    assert "rolling back" in msgs[1].body


def test_parse_mcp_bot_detection():
    text = ("=== Message from OpsgenieBot (B0OPS) at 2026-06-10 ===\n"
            "Message TS: 1700000000.000100\n"
            "P1 alert fired\n")
    msgs = su.parse_mcp_messages(text)
    assert msgs[0].is_bot is True                    # actor id starts with B

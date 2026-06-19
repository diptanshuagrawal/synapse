"""derive/slack_validate.py — per-channel Slack health checks.

check_channel runs the per-channel battery (counts, reply-drift, cursor-lag,
orphan replies, raw mentions, bot leaks, duplicate ts) — it takes the channel
cfg + cursors as params, so it's driven directly against the seed's C0A channel
(one parent + one reply) with scenarios injected in-test. No config files read.
"""

from __future__ import annotations

import pytest

from derive import slack_validate as sv

CID = "C0A"
CH = {"name": "incidents", "keep_bot_messages": False}


def _findings(result):
    return {(f[1], f[0]) for f in result["findings"]}   # {(check, severity)}


def test_no_cursor_flags_cursor_lag(seeded_db):
    out = sv.check_channel(seeded_db, CID, CH, cursors={})
    assert out["top"] == 1 and out["replies_db"] == 1
    assert ("cursor_lag", "FAIL") in _findings(out)   # rows but no cursor


def test_future_cursor_no_lag(seeded_db):
    out = sv.check_channel(seeded_db, CID, CH, cursors={CID: "9999999999"})
    checks = {f[1] for f in out["findings"]}
    assert "cursor_lag" not in checks                 # cursor ahead of newest row


def test_cursor_far_behind_fails(seeded_db):
    out = sv.check_channel(seeded_db, CID, CH, cursors={CID: "1"})   # epoch 1970
    assert ("cursor_lag", "FAIL") in _findings(out)   # cursor far behind


def test_duplicate_ts_flagged(seeded_db):
    # inject a second thread_started with the SAME ts in C0A → dup_ts.
    seeded_db.execute(
        "INSERT INTO events (id, source, event_type, ts, subject, channel_id, raw_path) "
        "VALUES ('dup1','slack','thread_started','2026-06-03T07:00:00Z','slack:C0A:dup','C0A','raw/x#d')")
    seeded_db.commit()
    assert ("dup_ts", "FAIL") in _findings(sv.check_channel(seeded_db, CID, CH, cursors={CID: "9999999999"}))


def test_orphan_reply_flagged(seeded_db):
    # a reply whose thread_ts has no parent thread_started row.
    seeded_db.execute(
        "INSERT INTO events (id, source, event_type, ts, subject, channel_id, thread_ts, raw_path) "
        "VALUES ('orph1','slack','thread_reply','2026-06-03T09:00:00Z','slack:C0A:9999.9','C0A','9999.9','raw/x#o')")
    seeded_db.commit()
    assert ("orphan_replies", "WARN") in _findings(sv.check_channel(seeded_db, CID, CH, cursors={CID: "9999999999"}))


def test_bot_leak_flagged(seeded_db):
    # a bot-actor row (json actor with is_bot) in a keep_bot=false channel.
    seeded_db.execute(
        "INSERT INTO events (id, source, event_type, ts, subject, channel_id, actor, raw_path) "
        "VALUES ('bot1','slack','thread_started','2026-06-03T10:00:00Z','slack:C0A:bot','C0A',"
        "'{\"is_bot\": 1}','raw/x#b')")
    seeded_db.commit()
    assert ("bot_leaks", "WARN") in _findings(sv.check_channel(seeded_db, CID, CH, cursors={CID: "9999999999"}))

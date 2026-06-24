"""Pre-standup active-thread sweep + cooldown-bypass (2026-06-23 late-reply fix).

A reply landing on a thread whose root has scrolled below the channel cursor is
invisible to cursor-bound history; the active-thread pass re-walks it by ACTIVITY.
The pre-standup sweep additionally bypasses the 24h drain cooldown, so a previous-
evening reply lands before the 06:00 digest (slack ingest itself only fires 12:00+).

These exercise the helper's parent-selection on a temp events.db (no network) and
the sweep's reply-walk via a fake client.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from ingest import slack_ingest_app as app
from derive import slack_backfill_helper as helper

CH = "C0SWEEP"
ROOT_ISO = "2026-06-20T10:00:00.000000Z"          # a recent thread root
ROOT_EPOCH = f"{dt.datetime.fromisoformat(ROOT_ISO.replace('Z', '+00:00')).timestamp():.6f}"


def _insert(conn, *, id, event_type, ts, channel_id=CH, **extra):
    cols = dict(id=id, source="slack", event_type=event_type, ts=ts, raw_path="x",
                channel_id=channel_id, **extra)
    keys = ",".join(cols)
    conn.execute(f"INSERT INTO events ({keys}) VALUES ({','.join('?' * len(cols))})",
                 tuple(cols.values()))
    conn.commit()


def _iso(delta):
    return ((dt.datetime.now(dt.timezone.utc) + delta)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


@pytest.fixture
def app_db(tmp_paths, db_conn, monkeypatch):
    """Point the ingest app's module-level DB_PATH at the bootstrapped temp events.db."""
    monkeypatch.setattr(app, "DB_PATH", tmp_paths.db_path)
    return tmp_paths.db_path


# ── helper: active-thread selection honours / bypasses the drain cooldown ──

def test_active_thread_cooldown_bypass(db_conn):
    # A thread root with replies, drained JUST now (inside the 24h cooldown), with a
    # recent reply so it qualifies on activity.
    _insert(db_conn, id=f"slack:{CH}:{ROOT_EPOCH}", event_type="thread_started",
            ts=ROOT_ISO, reply_count=3, drain_attempted_at=_iso(dt.timedelta(0)))
    _insert(db_conn, id=f"slack:{CH}:r1", event_type="thread_reply",
            ts=_iso(dt.timedelta(hours=-2)), thread_ts=ROOT_EPOCH)

    # Cooldown ON → suppressed (drained <24h ago).
    assert helper._active_thread_parents(db_conn, CH) == []
    # Cooldown BYPASSED (pre-standup sweep) → surfaced.
    assert helper._active_thread_parents(db_conn, CH, ignore_cooldown=True) == [ROOT_EPOCH]


def test_active_thread_old_inactive_excluded(db_conn):
    # Root with replies but no activity within --days → excluded even with bypass.
    _insert(db_conn, id=f"slack:{CH}:old", event_type="thread_started",
            ts="2025-01-01T00:00:00.000000Z", reply_count=2)
    assert helper._active_thread_parents(db_conn, CH, days=14, ignore_cooldown=True) == []


# ── helper: stale detector flags an under-ingested thread ──

def test_stale_thread_parents_flags_undercount(db_conn):
    ch = "C0STALE"
    root_epoch = f"{dt.datetime.fromisoformat('2026-06-18T09:00:00+00:00').timestamp():.6f}"
    _insert(db_conn, id=f"slack:{ch}:root", event_type="thread_started",
            ts="2026-06-18T09:00:00.000000Z", channel_id=ch, reply_count=5)
    for i in range(2):
        _insert(db_conn, id=f"slack:{ch}:r{i}", event_type="thread_reply",
                ts="2026-06-18T09:05:00.000000Z", channel_id=ch, thread_ts=root_epoch)
    stale = helper._stale_thread_parents(db_conn, ch)
    assert stale, "declared 5 > 2 stored should be flagged stale"
    assert stale[0][1] == 5 and stale[0][2] == 2


# ── helper: pending = thread_starteds with replies but none ingested yet ──

def test_pending_thread_parents(db_conn):
    ch = "C0PEND"
    e_pending = f"{dt.datetime.fromisoformat('2026-06-19T08:00:00+00:00').timestamp():.6f}"
    e_done = f"{dt.datetime.fromisoformat('2026-06-19T09:00:00+00:00').timestamp():.6f}"
    # has replies declared but none ingested → pending
    _insert(db_conn, id=f"slack:{ch}:p", event_type="thread_started",
            ts="2026-06-19T08:00:00.000000Z", channel_id=ch, reply_count=2)
    # has a reply ingested → NOT pending
    _insert(db_conn, id=f"slack:{ch}:d", event_type="thread_started",
            ts="2026-06-19T09:00:00.000000Z", channel_id=ch, reply_count=1)
    _insert(db_conn, id=f"slack:{ch}:dr", event_type="thread_reply",
            ts="2026-06-19T09:05:00.000000Z", channel_id=ch, thread_ts=e_done)
    pending = helper._pending_thread_parents(db_conn, ch)
    assert e_pending in pending and e_done not in pending


# ── helper: re-clamp reply_count to the true count after a drain ──

def test_clamp_parent_after_drain(db_conn):
    ch = "C0CLAMP"
    # parent declares 99 but only 2 replies actually stored
    _insert(db_conn, id=f"slack:{ch}:root", event_type="thread_started",
            ts=ROOT_ISO, channel_id=ch, reply_count=99)
    for i in range(2):
        _insert(db_conn, id=f"slack:{ch}:r{i}", event_type="thread_reply",
                ts=ROOT_ISO, channel_id=ch, thread_ts=ROOT_EPOCH)
    helper._clamp_parent_after_drain(db_conn, ch, ROOT_EPOCH)
    row = db_conn.execute(
        "SELECT reply_count, drain_attempted_at FROM events "
        "WHERE id=?", (f"slack:{ch}:root",)).fetchone()
    assert row[0] == 2                      # re-clamped to the true stored count
    assert row[1] is not None              # drain stamped (engages the cooldown)


# ── sweep: re-walks a thread and lands a late reply ──

class _FakeClient:
    def __init__(self, replies):
        self._replies = replies

    def history(self, *a, **k):
        return {"messages": [], "response_metadata": {"next_cursor": ""}}

    def iter_replies(self, channel_id, ts, limit=200):
        yield from self._replies.get(ts, [])


def test_sweep_channel_threads_pulls_late_reply(app_db, patch_config, monkeypatch):
    member = "U0MEMBER"
    # active_thread_parents shells out to a subprocess in real use — stub it to the one
    # active thread, and neutralise the thread_summary subprocess.
    monkeypatch.setattr(app, "active_thread_parents",
                        lambda cid, days=90, ignore_cooldown=False: [ROOT_EPOCH])
    monkeypatch.setattr("subprocess.run", lambda *a, **k: None)

    client = _FakeClient(replies={ROOT_EPOCH: [
        {"ts": ROOT_EPOCH, "user": "U0OTHER", "text": "root"},          # skipped (== parent)
        {"ts": "1782000099.000000", "thread_ts": ROOT_EPOCH, "user": member,
         "text": "late reply that scrolled below the cursor"},
    ]})

    s = app.sweep_channel_threads(client, {"id": CH}, dry_run=False,
                                  users_cache={}, name_resolver=None, subteams_cache={})
    assert s["replies_inserted"] >= 1

    conn = sqlite3.connect(str(app_db))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE source='slack' AND channel_id=? "
            "AND event_type='thread_reply' AND actor=?", (CH, member)).fetchone()[0]
        assert n == 1, "sweep should have landed the late reply"
    finally:
        conn.close()


# ── widened sweep scope: roster-active channels (incl. busy cross-team) ──

def test_channels_with_recent_roster_activity(app_db, db_conn):
    recent = _iso(dt.timedelta(days=-2))
    old = "2026-01-01T00:00:00Z"
    _insert(db_conn, id="slack:C0RECENT:1", event_type="thread_reply", ts=recent,
            channel_id="C0RECENT", actor="U0MEMBER")        # roster, in window → include
    _insert(db_conn, id="slack:C0OLD:1", event_type="thread_reply", ts=old,
            channel_id="C0OLD", actor="U0MEMBER")           # roster, too old → exclude
    _insert(db_conn, id="slack:C0OTHER:1", event_type="thread_reply", ts=recent,
            channel_id="C0OTHER", actor="U0OTHER")          # not roster → exclude
    chans = app.channels_with_recent_roster_activity({"U0MEMBER"}, days=14)
    assert "C0RECENT" in chans
    assert "C0OLD" not in chans
    assert "C0OTHER" not in chans
    assert app.channels_with_recent_roster_activity(set(), days=14) == set()


def test_fetch_threads_capped_respects_cap(app_db):
    # cap truncates the parent list (sweep passes a higher cap so busy channels don't starve)
    client = _FakeClient(replies={})
    _repl, _bot, _err, hit = app.fetch_threads_capped(
        client, "C0X", ["a", "b", "c"], dry_run=True, users_cache={},
        keep_bot_messages=False, name_resolver=None, subteams_cache={}, cap=2)
    assert hit is True                                       # 3 parents > cap 2 → capped

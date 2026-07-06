"""Regression guard for the team_involved LATE-reply blind spot.

Failure observed 2026-07-03 in a kiosk-support channel
(ingest_mode: team_involved; anonymised here as C0KIOSK):

  A non-team member rooted a thread (kiosk lien-flow failure). When the
  ingest scanned the root it had no team replies yet, so it was DROPPED —
  correct per the filter — and the channel cursor advanced past it. Sixteen
  days later a roster member replied with the actual investigation.
  That reply was permanently unfetchable: conversations.history only returns
  parents newer than the cursor, and every reconcile path (Phase 2.4 pending
  queue, Phase 2.5 stale/active re-drain) only revisits parents already in
  events.db. The standup digest missed his work.

  Fix: a search-driven safety net (search_net_recover_missed_threads).
  search.messages `from:<member>` keys on the MEMBER, not the root, so it
  surfaces the late reply regardless of cursor position; any hit whose
  thread is missing from events.db is drained whole (root + replies).

Tests exercise the net directly on a temp events.db with a fake client —
no network.
"""

from __future__ import annotations

import sqlite3

import pytest

from ingest import slack_ingest_app as app


CHANNEL = "C0KIOSK"
ROOT_TS = "1781681921.119229"      # non-team root, 17-Jun — long below the cursor
LATE_TS = "1783088796.706419"      # roster member's late reply, 03-Jul
MEMBER_UID = "U0ALICE"
EXT_UID = "U0EXT"

TEAM_CHANNELS = [{"id": CHANNEL, "name": "kiosk-support",
                  "ingest_mode": "team_involved"}]

_ROOT_MSG = {
    "ts": ROOT_TS,
    "user": EXT_UID,
    "text": "which flow is breaking?",
    "reply_count": 2,
}
_EXT_REPLY = {
    "ts": "1783080000.000001",
    "thread_ts": ROOT_TS,
    "user": EXT_UID,
    "text": "lien creation fails on kiosk, purpose comes as null",
}
_MEMBER_LATE_REPLY = {
    "ts": LATE_TS,
    "thread_ts": ROOT_TS,
    "user": MEMBER_UID,
    "text": "checked the payload — purpose is null, please send KIOSK_HOLD",
}
_BOT_REPLY = {
    "ts": "1783088900.000001",
    "thread_ts": ROOT_TS,
    "bot_id": "B0NOISE",
    "username": "Noise Bot",
    "text": "thread archived reminder",
}


def _match(channel_id: str, ts: str, thread_ts: str | None) -> dict:
    """Shape of a search.messages hit (permalink carries thread_ts for replies)."""
    p_ts = ts.replace(".", "")
    permalink = f"https://x.slack.com/archives/{channel_id}/p{p_ts}"
    if thread_ts:
        permalink += f"?thread_ts={thread_ts}&cid={channel_id}"
    return {"ts": ts, "channel": {"id": channel_id, "name": "x"},
            "permalink": permalink}


class _FakeClient:
    """search.messages + conversations.replies stand-in."""

    def __init__(self, matches_by_uid, replies_by_parent):
        self._matches = matches_by_uid          # uid -> [match, ...]
        self._replies = replies_by_parent       # root_ts -> [msg, ...]
        self.search_calls = 0
        self.replies_calls = 0

    def search_messages(self, query, count=100, page=1, sort="timestamp",
                        sort_dir="desc"):
        self.search_calls += 1
        uid = query.split("from:<@", 1)[1].split(">", 1)[0]
        matches = self._matches.get(uid, [])
        if isinstance(matches, Exception):
            raise matches
        return {"messages": {"matches": list(matches),
                             "paging": {"page": 1, "pages": 1}}}

    def iter_replies(self, channel_id, ts, limit=200):
        self.replies_calls += 1
        for m in self._replies.get(ts, []):
            yield m


@pytest.fixture
def app_db(tmp_paths, db_conn, monkeypatch):
    """Redirect the app's DB_PATH at the bootstrapped temp events.db, and stub
    the build_thread_summary subprocess (it would open the REAL events.db)."""
    monkeypatch.setattr(app, "DB_PATH", tmp_paths.db_path)
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    return tmp_paths.db_path


def _ro(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _run_net(client, dry_run=False):
    return app.search_net_recover_missed_threads(
        client, TEAM_CHANNELS, {MEMBER_UID}, dry_run,
        users_cache={}, name_resolver=None, subteams_cache={},
        team_subteam_ids=set(),
    )


# ── The regression ──────────────────────────────────────────────────────────

def test_late_reply_to_dropped_root_is_recovered(app_db, patch_config):
    """The kiosk miss: whole thread absent from events.db, member's late reply
    surfaces via search → root + human replies land; bot reply skipped."""
    client = _FakeClient(
        matches_by_uid={MEMBER_UID: [_match(CHANNEL, LATE_TS, ROOT_TS)]},
        replies_by_parent={ROOT_TS: [_ROOT_MSG, _EXT_REPLY,
                                     _MEMBER_LATE_REPLY, _BOT_REPLY]},
    )
    out = _run_net(client)

    assert out["hits_in_scope"] == 1
    assert out["threads_missing"] == 1
    assert out["threads_drained"] == 1
    assert out["threads_kept"] == 1
    assert out["root_inserted"] == 1
    assert out["replies_inserted"] == 2      # ext + member; bot skipped
    assert out["errors"] == []

    conn = _ro(app_db)
    try:
        # THE assertion the miss demands: the member's late reply is present.
        row = conn.execute(
            "SELECT event_type, thread_ts FROM events WHERE id=?",
            (f"slack:{CHANNEL}:{ROOT_TS}:{LATE_TS}",),
        ).fetchone()
        assert row is not None, "late reply was not recovered"
        assert row["event_type"] == "thread_reply"
        assert row["thread_ts"] == ROOT_TS

        # Non-team root kept (it gives the reply meaning), reply_count clamped
        # to the replies that actually landed.
        parent = conn.execute(
            "SELECT event_type, reply_count, drain_attempted_at FROM events "
            "WHERE id=?", (f"slack:{CHANNEL}:{ROOT_TS}",),
        ).fetchone()
        assert parent is not None
        assert parent["event_type"] == "thread_started"
        assert parent["reply_count"] == 2
        assert parent["drain_attempted_at"] is not None
    finally:
        conn.close()


def test_already_ingested_thread_is_not_redrained(app_db, patch_config):
    """Second pass over the same hits must be a no-op (id check, no replies walk)."""
    client = _FakeClient(
        matches_by_uid={MEMBER_UID: [_match(CHANNEL, LATE_TS, ROOT_TS)]},
        replies_by_parent={ROOT_TS: [_ROOT_MSG, _EXT_REPLY, _MEMBER_LATE_REPLY]},
    )
    _run_net(client)
    walked_first = client.replies_calls

    out = _run_net(client)
    assert out["threads_missing"] == 0
    assert out["threads_drained"] == 0
    assert client.replies_calls == walked_first   # no second walk


def test_hits_outside_team_involved_channels_ignored(app_db, patch_config):
    client = _FakeClient(
        matches_by_uid={MEMBER_UID: [_match("C0OTHER", LATE_TS, ROOT_TS)]},
        replies_by_parent={},
    )
    out = _run_net(client)
    assert out["hits_seen"] == 1
    assert out["hits_in_scope"] == 0
    assert out["threads_drained"] == 0
    assert client.replies_calls == 0


def test_missing_top_level_member_message_drained_as_root(app_db, patch_config):
    """A hit with no thread_ts in its permalink is its own root."""
    top = {"ts": LATE_TS, "user": MEMBER_UID, "text": "standalone update"}
    client = _FakeClient(
        matches_by_uid={MEMBER_UID: [_match(CHANNEL, LATE_TS, None)]},
        replies_by_parent={LATE_TS: [top]},
    )
    out = _run_net(client)
    assert out["threads_kept"] == 1
    assert out["root_inserted"] == 1
    assert out["replies_inserted"] == 0

    conn = _ro(app_db)
    try:
        row = conn.execute(
            "SELECT event_type FROM events WHERE id=?",
            (f"slack:{CHANNEL}:{LATE_TS}",),
        ).fetchone()
        assert row is not None and row["event_type"] == "thread_started"
    finally:
        conn.close()


def test_drain_cap_defers_spillover(app_db, patch_config, monkeypatch):
    monkeypatch.setattr(app, "SEARCH_NET_DRAIN_CAP", 1)
    root2 = "1781700000.000001"
    m2 = dict(_MEMBER_LATE_REPLY, ts="1783090000.000001", thread_ts=root2)
    client = _FakeClient(
        matches_by_uid={MEMBER_UID: [
            _match(CHANNEL, LATE_TS, ROOT_TS),
            _match(CHANNEL, m2["ts"], root2),
        ]},
        replies_by_parent={
            ROOT_TS: [_ROOT_MSG, _MEMBER_LATE_REPLY],
            root2: [dict(_ROOT_MSG, ts=root2, reply_count=1), m2],
        },
    )
    out = _run_net(client)
    assert out["threads_missing"] == 2
    assert out["threads_drained"] == 1
    assert out["drain_cap_dropped"] == 1


def test_search_error_recorded_other_members_continue(app_db, patch_config):
    client = _FakeClient(
        matches_by_uid={
            "U0AARON": RuntimeError("search.messages failed: ratelimited"),
            MEMBER_UID: [_match(CHANNEL, LATE_TS, ROOT_TS)],
        },
        replies_by_parent={ROOT_TS: [_ROOT_MSG, _MEMBER_LATE_REPLY]},
    )
    out = app.search_net_recover_missed_threads(
        client, TEAM_CHANNELS, {"U0AARON", MEMBER_UID}, False,
        users_cache={}, name_resolver=None, subteams_cache={},
        team_subteam_ids=set(),
    )
    assert len(out["errors"]) == 1
    assert "U0AARON" in out["errors"][0]
    assert out["threads_kept"] == 1          # the healthy member still recovered


def test_thread_with_no_team_involvement_stores_nothing(app_db, patch_config):
    """If the member's message vanished between search-index and drain (deleted),
    the walk finds no team involvement → nothing upserted."""
    client = _FakeClient(
        matches_by_uid={MEMBER_UID: [_match(CHANNEL, LATE_TS, ROOT_TS)]},
        replies_by_parent={ROOT_TS: [_ROOT_MSG, _EXT_REPLY]},   # member gone
    )
    out = _run_net(client)
    assert out["threads_drained"] == 1
    assert out["threads_kept"] == 0
    assert out["root_inserted"] == 0

    conn = _ro(app_db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE source='slack' AND channel_id=?",
            (CHANNEL,),
        ).fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_dry_run_counts_without_writing(app_db, patch_config):
    client = _FakeClient(
        matches_by_uid={MEMBER_UID: [_match(CHANNEL, LATE_TS, ROOT_TS)]},
        replies_by_parent={ROOT_TS: [_ROOT_MSG, _EXT_REPLY, _MEMBER_LATE_REPLY]},
    )
    out = _run_net(client, dry_run=True)
    assert out["threads_kept"] == 1
    assert out["root_inserted"] == 1
    assert out["replies_inserted"] == 2

    conn = _ro(app_db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert n == 0
    finally:
        conn.close()

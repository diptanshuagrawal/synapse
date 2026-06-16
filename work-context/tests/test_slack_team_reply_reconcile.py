"""Regression guard for the team_involved starved-reply data-loss bug.

Failure observed 2026-06-15 in channel C0RELEASE (release-notifications,
ingest_mode: team_involved):

  A "Release Bot" bot (B0RELBOT) posted a release-thread root. Reply #3
  was a roster member (Alice, U0ALICE) asking the owner to approve a CMR —
  a real owner action item. In team_involved mode a bot-rooted thread is only
  KEPT if a budget-capped reply walk (TEAM_REPLY_CHECK_CAP) confirms a team
  member participated. On this ~95%-bot channel the per-fire budget was consumed
  by other release-bot threads, so this root was DROPPED without walking its
  replies — yet the cursor still advanced past it, so no future fire re-examined
  the thread. Alice's reply was lost forever and standup §7b missed it.

  Fix: when a bot-rooted thread with replies is starved of reply-walk budget,
  enqueue it into slack_pending_reply_check instead of silently dropping. A
  later (or same) fire drains the queue, walks the replies regardless of cursor
  position, and upserts the root + replies when a team member is involved.

These tests exercise the two ingest functions directly on a temp events.db so
the exact starvation path is reproduced without any network.
"""

from __future__ import annotations

import sqlite3

import pytest

from ingest import slack_ingest_app as app
from derive import slack_backfill_helper as helper


# ── The real C0RELEASE thread (anonymised owner mention) ───────────────────

CHANNEL = "C0RELEASE"
ROOT_TS = "1781524980.935269"          # bot release-thread root, 17:33 IST
MEMBER_TS = "1781525167.043179"        # reply #3 — the owner CMR-approval ask
MEMBER_UID = "U0ALICE"

_ROOT_MSG = {
    "ts": ROOT_TS,
    "bot_id": "B0RELBOT",
    "username": "Release Bot",
    "text": "Release EX-2904 deploying to PROD",
    "reply_count": 3,
}
_BOT_ACK = {
    "ts": "1781525100.000000",
    "thread_ts": ROOT_TS,
    "bot_id": "B0RELBOT",
    "username": "Release Bot",
    "text": "pipeline started",
}
_MEMBER_REPLY = {
    "ts": MEMBER_TS,
    "thread_ts": ROOT_TS,
    "user": MEMBER_UID,
    "text": "<@U0OWNER> can you approve CMR EX-2904 please?",
}


class _FakeClient:
    """Minimal SlackClient stand-in: serves one history page + thread replies."""

    def __init__(self, history_messages, replies_by_parent):
        self._history_messages = history_messages
        self._replies = replies_by_parent
        self.replies_calls = 0

    def history(self, channel_id, oldest=None, latest=None, limit=200, cursor=None):
        # Single page, no further cursor.
        return {"messages": list(self._history_messages),
                "response_metadata": {"next_cursor": ""}}

    def iter_replies(self, channel_id, ts, limit=200):
        self.replies_calls += 1
        for m in self._replies.get(ts, []):
            yield m


@pytest.fixture
def app_db(tmp_paths, db_conn, monkeypatch):
    """Point the ingest app's module-level DB_PATH at the temp events.db.

    `db_conn` already bootstrapped the full schema (incl. slack_pending_reply_check)
    on tmp_paths.db_path. The app functions open their own connections to that
    same file, so we just redirect the global.
    """
    monkeypatch.setattr(app, "DB_PATH", tmp_paths.db_path)
    return tmp_paths.db_path


def _ro(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ── The regression ──────────────────────────────────────────────────────────

def test_starved_bot_root_is_enqueued_not_dropped(app_db, patch_config, monkeypatch):
    """History pass with exhausted budget must enqueue the bot root, not drop it,
    and must NOT have stored the buried team reply inline."""
    # Force the budget to zero so the very first non-team bot root is starved —
    # exactly the state release-notifications reaches after other bot threads consume it.
    monkeypatch.setattr(app, "TEAM_REPLY_CHECK_CAP", 0)

    client = _FakeClient(
        history_messages=[_ROOT_MSG],
        replies_by_parent={ROOT_TS: [_ROOT_MSG, _BOT_ACK, _MEMBER_REPLY]},
    )

    (top_inserted, repl_inline, newest_ts, bot_skipped,
     dropped, deferred, starved_enqueued, hit_cap) = app.fetch_history_team_filtered(
        client, CHANNEL, oldest="0", dry_run=False,
        users_cache={}, keep_bot_messages=False, name_resolver=None,
        subteams_cache={}, team_slack_ids={MEMBER_UID}, team_subteam_ids=set(),
    )

    # Starved → enqueued, not silently dropped; nothing kept inline.
    assert starved_enqueued == 1
    assert top_inserted == 0
    assert repl_inline == 0
    assert newest_ts == ROOT_TS  # cursor would advance past the root

    conn = _ro(app_db)
    try:
        # Queue holds the starved parent.
        queued = conn.execute(
            "SELECT parent_ts, reply_count FROM slack_pending_reply_check "
            "WHERE channel_id=?", (CHANNEL,)
        ).fetchall()
        assert [(q["parent_ts"], q["reply_count"]) for q in queued] == [(ROOT_TS, 3)]

        # The buried team reply is NOT yet in events — that's the whole bug.
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE source='slack' AND channel_id=? "
            "AND event_type='thread_reply'", (CHANNEL,)
        ).fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_drain_recovers_buried_team_reply(app_db, patch_config, monkeypatch):
    """After enqueue, the drain pass must land Alice's reply in events.db."""
    monkeypatch.setattr(app, "TEAM_REPLY_CHECK_CAP", 0)
    client = _FakeClient(
        history_messages=[_ROOT_MSG],
        replies_by_parent={ROOT_TS: [_ROOT_MSG, _BOT_ACK, _MEMBER_REPLY]},
    )

    # Phase 2b — starve + enqueue.
    app.fetch_history_team_filtered(
        client, CHANNEL, oldest="0", dry_run=False,
        users_cache={}, keep_bot_messages=False, name_resolver=None,
        subteams_cache={}, team_slack_ids={MEMBER_UID}, team_subteam_ids=set(),
    )

    # Phase 2.4 — drain the queue.
    out = app.reconcile_pending_reply_checks(
        client, CHANNEL, dry_run=False,
        users_cache={}, keep_bot_messages=False, name_resolver=None,
        subteams_cache={}, team_slack_ids={MEMBER_UID}, team_subteam_ids=set(),
    )

    assert out["drained"] == 1
    assert out["kept"] == 1
    assert out["root_inserted"] == 1          # bot root kept (gives the reply meaning)
    assert out["replies_inserted"] == 1       # Alice only; bot ack skipped
    assert out["queue_out"] == 0              # dequeued after resolution

    conn = _ro(app_db)
    try:
        # THE assertion the bug report demands: Alice's reply is present.
        row = conn.execute(
            "SELECT actor, thread_ts, event_type FROM events "
            "WHERE source='slack' AND channel_id=? AND actor=?",
            (CHANNEL, MEMBER_UID),
        ).fetchone()
        assert row is not None, "Alice's CMR-approval reply was not recovered"
        assert row["event_type"] == "thread_reply"
        assert row["thread_ts"] == ROOT_TS

        # Bot root upserted as the thread parent.
        parent = conn.execute(
            "SELECT event_type FROM events WHERE id=?",
            (f"slack:{CHANNEL}:{ROOT_TS}",),
        ).fetchone()
        assert parent is not None and parent["event_type"] == "thread_started"
    finally:
        conn.close()


# ── The real-world trigger: bot root whose BODY tags a team member ───────────

_TEAM_MENTION_ROOT = {
    "ts": ROOT_TS,
    "bot_id": "B0RELBOT",
    "username": "Release Bot",
    # This is the actual shape of the live release-notifications root: the bot pings the
    # owner/approver in the body, so is_team_involved(root) is True.
    "text": "Release process initiated (<@U0ALICE|Alice Roster>)",
    "reply_count": 3,
}


def test_team_mentioned_bot_root_is_kept_not_dropped(app_db, patch_config):
    """A bot root that tags a team member and has replies must be KEPT (so
    Phase 2.5 reconcile walks its replies), not bot_skipped-and-forgotten.

    This is the exact path the live C0RELEASE thread hits — distinct from the
    budget-exhaustion path. With ample budget it never reaches the starve/enqueue
    branch; the loss came purely from dropping the team-mentioned bot root.
    """
    client = _FakeClient(
        history_messages=[_TEAM_MENTION_ROOT],
        replies_by_parent={ROOT_TS: [_TEAM_MENTION_ROOT, _BOT_ACK, _MEMBER_REPLY]},
    )
    (top_inserted, repl_inline, newest_ts, bot_skipped,
     dropped, deferred, starved_enqueued, hit_cap) = app.fetch_history_team_filtered(
        client, CHANNEL, oldest="0", dry_run=False,
        users_cache={}, keep_bot_messages=False, name_resolver=None,
        subteams_cache={}, team_slack_ids={MEMBER_UID}, team_subteam_ids=set(),
    )
    assert top_inserted == 1        # root KEPT
    assert bot_skipped == 0         # NOT dropped as bot noise
    assert starved_enqueued == 0    # plenty of budget — no starve path

    conn = _ro(app_db)
    try:
        # Root stored as thread parent with its reply_count → Phase 2.5's
        # pending_thread_parents will now find + walk it.
        row = conn.execute(
            "SELECT event_type, reply_count FROM events WHERE id=?",
            (f"slack:{CHANNEL}:{ROOT_TS}",),
        ).fetchone()
        assert row is not None
        assert row["event_type"] == "thread_started"
        assert row["reply_count"] == 3
    finally:
        conn.close()


def test_team_mentioned_bot_root_with_no_replies_is_dropped(app_db, patch_config):
    """A team-tagged bot ping with NO thread under it has nothing to recover —
    keep the old drop behaviour so we don't bloat the DB with standalone pings."""
    root = dict(_TEAM_MENTION_ROOT, reply_count=0)
    client = _FakeClient(history_messages=[root], replies_by_parent={})
    (top_inserted, _r, _n, bot_skipped, dropped, _d, starved, _h) = \
        app.fetch_history_team_filtered(
            client, CHANNEL, oldest="0", dry_run=False,
            users_cache={}, keep_bot_messages=False, name_resolver=None,
            subteams_cache={}, team_slack_ids={MEMBER_UID}, team_subteam_ids=set(),
        )
    assert top_inserted == 0
    assert bot_skipped == 1
    assert starved == 0


def test_drain_dequeues_no_team_thread_without_upsert(app_db, patch_config, monkeypatch):
    """A starved bot thread with NO team participation is dequeued, stores nothing."""
    monkeypatch.setattr(app, "TEAM_REPLY_CHECK_CAP", 0)
    client = _FakeClient(
        history_messages=[_ROOT_MSG],
        replies_by_parent={ROOT_TS: [_ROOT_MSG, _BOT_ACK]},  # bots only
    )
    app.fetch_history_team_filtered(
        client, CHANNEL, oldest="0", dry_run=False,
        users_cache={}, keep_bot_messages=False, name_resolver=None,
        subteams_cache={}, team_slack_ids={MEMBER_UID}, team_subteam_ids=set(),
    )
    out = app.reconcile_pending_reply_checks(
        client, CHANNEL, dry_run=False,
        users_cache={}, keep_bot_messages=False, name_resolver=None,
        subteams_cache={}, team_slack_ids={MEMBER_UID}, team_subteam_ids=set(),
    )
    assert out["drained"] == 1
    assert out["no_team"] == 1
    assert out["kept"] == 0
    assert out["queue_out"] == 0  # resolved-negative → dequeued, not retried forever

    conn = _ro(app_db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE source='slack' AND channel_id=?",
            (CHANNEL,),
        ).fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_errored_drain_bumps_attempts_then_abandons(app_db, db_conn, monkeypatch):
    """A parent whose replies endpoint keeps failing is retried, then abandoned
    at PENDING_REPLY_MAX_ATTEMPTS — never an infinite loop."""
    monkeypatch.setattr(app, "PENDING_REPLY_MAX_ATTEMPTS", 2)

    class _BrokenClient:
        def iter_replies(self, channel_id, ts, limit=200):
            raise RuntimeError("conversations.replies: fetch_failed")
            yield  # pragma: no cover

    helper.enqueue_pending_reply_check(db_conn, CHANNEL, ROOT_TS, 3)
    client = _BrokenClient()

    # Fire 1: error → attempts=1, still queued.
    out1 = app.reconcile_pending_reply_checks(
        client, CHANNEL, dry_run=False, users_cache={}, keep_bot_messages=False,
        name_resolver=None, subteams_cache={}, team_slack_ids={MEMBER_UID},
    )
    assert out1["errors"] and out1["queue_out"] == 1

    # Fire 2: error → attempts=2 (== ceiling), still present for one more pop.
    app.reconcile_pending_reply_checks(
        client, CHANNEL, dry_run=False, users_cache={}, keep_bot_messages=False,
        name_resolver=None, subteams_cache={}, team_slack_ids={MEMBER_UID},
    )

    # Fire 3: pop abandons it (attempts >= ceiling) before any walk → queue empty.
    out3 = app.reconcile_pending_reply_checks(
        client, CHANNEL, dry_run=False, users_cache={}, keep_bot_messages=False,
        name_resolver=None, subteams_cache={}, team_slack_ids={MEMBER_UID},
    )
    assert out3["drained"] == 0
    assert out3["queue_out"] == 0

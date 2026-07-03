"""Daily team-reply capture on no_threads alert channels.

no_threads channels skip per-fire reply reconcile; this once-a-day pass re-walks
recent alert threads and keeps only the team-involved replies. Exercised on a
temp events.db with a fake Slack client (no network).
"""

from __future__ import annotations

import datetime as dt
import sys

import pytest

from derive import slack_alert_thread_reconcile as mod

CH = "C0ALERT"
MEMBER = "U0MEMBER"


def _recent(delta_min=-30):
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=delta_min)
    iso = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
    epoch = f"{now.timestamp():.6f}"
    return iso, epoch


_PARENT_TS = "1782000000.000100"


class _FakeClient:
    """Alert parents come from LIVE history (bot parents never reach the DB)."""

    def __init__(self, *a, **k):
        pass

    def build_users_cache(self):
        return {}

    def build_subteams_cache(self):
        return {}

    def iter_history(self, channel_id, oldest=None, limit=200):
        # bot alert parent with replies → scanned
        yield {"ts": _PARENT_TS, "bot_id": "B0ALERT", "text": "alert fired",
               "reply_count": 2}
        # parent without replies → skipped
        yield {"ts": "1782000010.000000", "bot_id": "B0ALERT", "text": "quiet alert"}
        # a reply surfaced in history → skipped (not a parent)
        yield {"ts": "1782000050.000000", "thread_ts": _PARENT_TS, "user": MEMBER,
               "text": "inline reply"}

    def iter_replies(self, channel_id, ts, limit=200):
        yield {"ts": ts, "bot_id": "B0ALERT", "text": "alert fired"}      # parent → skipped
        yield {"ts": "1782000050.000000", "thread_ts": ts, "user": MEMBER,
               "text": "I picked this up — false positive"}


def test_recent_alert_parents_from_history():
    parents = mod._recent_alert_parents(_FakeClient(), CH, "1781999999.000000")
    assert [p["ts"] for p in parents] == [_PARENT_TS]


def test_main_dry_run_keeps_team_reply(tmp_paths, db_conn, monkeypatch):
    monkeypatch.setattr(mod, "SlackClient", _FakeClient)
    monkeypatch.setattr(mod, "load_team_slack_ids", lambda: {MEMBER: "x"})
    monkeypatch.setattr(mod, "load_team_subteam_ids", lambda: set())
    monkeypatch.setattr(mod, "_no_threads_channels", lambda: [(CH, "alert-chan")])
    monkeypatch.setattr(mod, "is_team_involved",
                        lambda author, text, ids, sub: author == MEMBER)
    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run"])
    assert mod.main() == 0


def test_main_live_upserts_parent_and_reply(tmp_paths, db_conn, monkeypatch):
    # get_db's default path binds at import — patch the module ref or main()
    # writes to the REAL events.db instead of the temp tree.
    monkeypatch.setattr(mod, "get_db", lambda: db_conn)
    monkeypatch.setattr(mod, "SlackClient", _FakeClient)
    monkeypatch.setattr(mod, "load_team_slack_ids", lambda: {MEMBER: "x"})
    monkeypatch.setattr(mod, "load_team_subteam_ids", lambda: set())
    monkeypatch.setattr(mod, "_no_threads_channels", lambda: [(CH, "alert-chan")])
    monkeypatch.setattr(mod, "is_team_involved",
                        lambda author, text, ids, sub: author == MEMBER)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert mod.main() == 0
    rows = {r[0]: r[1] for r in db_conn.execute(
        "SELECT id, event_type FROM events WHERE channel_id=?", (CH,))}
    # thread root lands too, so slack:<ch>:<thread_ts> subjects resolve
    assert rows[f"slack:{CH}:{_PARENT_TS}"] == "thread_started"
    reply_rows = [t for t in rows.values() if t == "thread_reply"]
    assert len(reply_rows) == 1


def test_main_aborts_without_team_ids(monkeypatch):
    monkeypatch.setattr(mod, "load_team_slack_ids", lambda: {})
    monkeypatch.setattr(mod, "load_team_subteam_ids", lambda: set())
    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run"])
    assert mod.main() == 2          # no team ids → guarded exit

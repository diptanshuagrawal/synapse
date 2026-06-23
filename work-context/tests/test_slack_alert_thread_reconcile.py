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


def _insert_alert(conn, ch=CH):
    iso, epoch = _recent()
    conn.execute(
        "INSERT INTO events (id,source,event_type,ts,raw_path,channel_id,reply_count) "
        "VALUES (?,?,?,?,?,?,?)",
        (f"slack:{ch}:{epoch}", "slack", "thread_started", iso, "x", ch, 2))
    conn.commit()
    return epoch


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    def build_users_cache(self):
        return {}

    def build_subteams_cache(self):
        return {}

    def iter_replies(self, channel_id, ts, limit=200):
        yield {"ts": ts, "user": "U0BOT", "text": "alert fired"}          # parent → skipped
        yield {"ts": "1782000050.000000", "thread_ts": ts, "user": MEMBER,
               "text": "I picked this up — false positive"}


def test_recent_alert_parents_in_window(db_conn):
    epoch = _insert_alert(db_conn)
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat().replace("+00:00", "Z")
    assert mod._recent_alert_parents(db_conn, CH, since) == [epoch]
    # outside the window → excluded
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat().replace("+00:00", "Z")
    assert mod._recent_alert_parents(db_conn, CH, future) == []


def test_main_dry_run_keeps_team_reply(tmp_paths, db_conn, monkeypatch):
    _insert_alert(db_conn)
    monkeypatch.setattr(mod, "SlackClient", _FakeClient)
    monkeypatch.setattr(mod, "load_team_slack_ids", lambda: {MEMBER: "x"})
    monkeypatch.setattr(mod, "load_team_subteam_ids", lambda: set())
    monkeypatch.setattr(mod, "_no_threads_channels", lambda: [(CH, "alert-chan")])
    monkeypatch.setattr(mod, "is_team_involved",
                        lambda author, text, ids, sub: author == MEMBER)
    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run"])
    assert mod.main() == 0


def test_main_aborts_without_team_ids(monkeypatch):
    monkeypatch.setattr(mod, "load_team_slack_ids", lambda: {})
    monkeypatch.setattr(mod, "load_team_subteam_ids", lambda: set())
    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run"])
    assert mod.main() == 2          # no team ids → guarded exit

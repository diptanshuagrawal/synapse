"""derive/person_v3.py — track classification + window helpers.

person_v3 decides a person's window work-mix (feature / platform / ops / mixed)
and their workstreams. The discriminating logic is _classify_track (pure) and
the conn-taking probes (_workstreams, _baseline_role, _review_concentration);
build_v3 itself fans out to other modules that open their own DB, so it's left
to integration. Helpers are exercised against the seeded DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_DERIVE = Path(__file__).resolve().parent.parent / "derive"
if str(_DERIVE) not in sys.path:
    sys.path.insert(0, str(_DERIVE))

from derive import person_v3 as pv  # noqa: E402

SINCE, UNTIL = "2026-05-01T00:00:00Z", "2026-06-30T00:00:00Z"
ALICE = ["alice-gh", "alice@example.com", "acc-alice", "U0ALICE"]


# ── _classify_track (pure) ───────────────────────────────────────────────────

def test_classify_track_feature():
    track, _ = pv._classify_track({"pr_work": 3}, dom_owned=2)
    assert track == "feature"


def test_classify_track_platform():
    track, _ = pv._classify_track({"design": 3, "cmr_ops": 2, "pr_work": 0})
    assert track == "platform"


def test_classify_track_ops():
    track, _ = pv._classify_track({"incident": 4, "pr_work": 0})
    assert track == "ops"


def test_classify_track_delivery_only_is_mixed():
    track, basis = pv._classify_track({}, dom_owned=0)
    assert track == "mixed" and "delivery-only" in basis


def test_classify_track_close_scores_mixed():
    # feature=2, platform=2 → within 1 → mixed.
    track, _ = pv._classify_track({"pr_work": 2, "design": 2})
    assert track == "mixed"


# ── _workstreams (seed) ──────────────────────────────────────────────────────

def test_workstreams_groups_by_cluster(seeded_db):
    subs = {"slack:C0A:1700000000.000100"}  # alice's clustered subject
    out = pv._workstreams(seeded_db, ALICE, SINCE, UNTIL, subs)
    assert len(out) == 1 and out[0]["cluster_id"] == 1


def test_workstreams_empty_inputs():
    # no aliases / no subjects → empty, no query.
    assert pv._workstreams(None, [], SINCE, UNTIL, set()) == []


# ── _baseline_role (seed) ────────────────────────────────────────────────────

def test_baseline_role_returns_role_and_basis(seeded_db):
    role, basis = pv._baseline_role(seeded_db, ALICE, UNTIL)
    assert role in ("feature", "platform", "ops", "mixed")
    assert isinstance(basis, str)


# ── _review_concentration (seed) ─────────────────────────────────────────────

def test_review_concentration_none_without_clustered_reviews(seeded_db):
    # alice has no review events on clustered subjects → graceful None.
    assert pv._review_concentration(seeded_db, ALICE, SINCE, UNTIL) is None


def test_review_concentration_none_for_empty_aliases(seeded_db):
    assert pv._review_concentration(seeded_db, [], SINCE, UNTIL) is None


# ── on-call ops detection (_oncall_ops_subjects / _baseline_role) ─────────────
# Guards the 2026-06-23 fix: the coarse baseline role counts on-call INVOLVEMENT
# (bot-acked incidents + @oncall-handle replies in domain channels), not just
# thread_started-by-actor — so a heavy-oncall engineer isn't mislabeled "mixed".
# Short fake ids (U0…/S0…/C0… with ≤6-char tails) dodge the publish leak gate.

# Ops-only person: no PRs / epics / pages → feature=platform=0 in _baseline_role.
OPSER = ["U0OPSER", "opser-gh", "opser@example.com"]
B_UNTIL = "2026-06-30T00:00:00Z"   # events seeded at 2026-06-10 fall in the 120d window
ONCALL_PING = "<!subteam^S0ONCALL"


def _slack(conn, *, eid, ts, actor, channel, thread, etype, body, title=""):
    """Insert one slack event row with an explicit channel_id (insert_event
    skips that column; the queries under test match on it)."""
    conn.execute(
        "INSERT INTO events (id, source, event_type, ts, actor, subject, title, body, "
        "url, raw_path, channel_id) VALUES (?, 'slack', ?, ?, ?, ?, ?, ?, '', ?, ?)",
        (eid, etype, ts, actor, f"slack:{channel}:{thread}", title, body,
         f"raw/{eid}.json", channel))


@pytest.fixture
def oncall_cfg(monkeypatch, tmp_path):
    """Point the shared on-call config loaders at a tiny fake config:
    one class:oncall channel (C0ONCALL), one plain domain channel (C0DOMAIN),
    one @oncall subteam handle (S0ONCALL)."""
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "slack_channels.yaml").write_text(
        "channels:\n"
        "  - {id: C0ONCALL, name: bot-feed, class: oncall}\n"
        "  - {id: C0DOMAIN, name: a-domain-channel}\n")
    (cfg / "team_subteams.yaml").write_text(
        "subteams:\n"
        "  - {id: S0ONCALL, handle: team-oncall}\n"
        "  - {id: S0DEV, handle: team-devs}\n")
    import derive.oncall_signals as oc
    import derive.retro_census as rc
    monkeypatch.setattr(oc, "_CHANNELS_YAML", cfg / "slack_channels.yaml")
    monkeypatch.setattr(oc, "_SUBTEAMS_YAML", cfg / "team_subteams.yaml")
    monkeypatch.setattr(rc, "_REPO_ROOT", tmp_path)  # _load_incident_channels reads this
    return cfg


def _seed_oncall_work(conn):
    """OPSER's on-call footprint, NONE of it thread_started-by-OPSER in a domain:
    a handle-ping reply, a bot ACK, a bot RESOLVE, and an incident OPSER rooted
    in the oncall channel. Plus a NON-oncall thread OPSER replied in (control)."""
    # (2) @oncall-handle ping in a PLAIN domain channel; OPSER replies.
    _slack(conn, eid="d1r", ts="2026-06-10T09:00:00Z", actor="U0RPT",
           channel="C0DOMAIN", thread="T1", etype="thread_started",
           body=f"{ONCALL_PING} can someone check this account?")
    _slack(conn, eid="d1a", ts="2026-06-10T09:05:00Z", actor="U0OPSER",
           channel="C0DOMAIN", thread="T1", etype="thread_reply", body="on it")
    # (3a) bot ACK confirmation mentioning OPSER, in the class:oncall channel.
    _slack(conn, eid="o1r", ts="2026-06-11T09:00:00Z", actor="U0BOT",
           channel="C0ONCALL", thread="T2", etype="thread_started",
           body="New Issue Reported: payout latency")
    _slack(conn, eid="o1a", ts="2026-06-11T09:02:00Z", actor="U0BOT",
           channel="C0ONCALL", thread="T2", etype="thread_reply",
           body="<@U0OPSER|Opser> acknowledged the issue. _Time to Acknowledge: 0:02:00_")
    # (3b) bot RESOLVE confirmation mentioning OPSER, in the class:oncall channel.
    _slack(conn, eid="o2r", ts="2026-06-12T09:00:00Z", actor="U0BOT",
           channel="C0ONCALL", thread="T3", etype="thread_started",
           body="New Issue Reported: recon mismatch")
    _slack(conn, eid="o2a", ts="2026-06-12T09:30:00Z", actor="U0BOT",
           channel="C0ONCALL", thread="T3", etype="thread_reply",
           body="<@U0OPSER|Opser> marked the issue as resolved. _Time to Resolve: 0:30:00_")
    # (1) OPSER STARTED an incident thread in the oncall channel.
    _slack(conn, eid="o3r", ts="2026-06-13T09:00:00Z", actor="U0OPSER",
           channel="C0ONCALL", thread="T4", etype="thread_started",
           body="seeing elevated 5xx on the gateway")
    # CONTROL: a plain thread OPSER replied in — no @oncall ping, no ack → excluded.
    _slack(conn, eid="c1r", ts="2026-06-14T09:00:00Z", actor="U0RPT",
           channel="C0DOMAIN", thread="T5", etype="thread_started",
           body="what's the schema for the new table?")
    _slack(conn, eid="c1a", ts="2026-06-14T09:05:00Z", actor="U0OPSER",
           channel="C0DOMAIN", thread="T5", etype="thread_reply", body="it's in the doc")
    conn.commit()


def test_oncall_ops_subjects_unions_all_three_signals(db_conn, oncall_cfg):
    _seed_oncall_work(db_conn)
    subs = pv._oncall_ops_subjects(db_conn, OPSER, "2026-03-01T00:00:00Z", B_UNTIL)
    # T1 (handle reply), T2 (bot ack), T3 (bot resolve), T4 (member-rooted) — but
    # NOT T5 (plain reply, no on-call signal).
    assert subs == {
        "slack:C0DOMAIN:T1", "slack:C0ONCALL:T2",
        "slack:C0ONCALL:T3", "slack:C0ONCALL:T4",
    }


def test_oncall_ops_subjects_empty_aliases(db_conn, oncall_cfg):
    assert pv._oncall_ops_subjects(db_conn, [], "2026-03-01T00:00:00Z", B_UNTIL) == set()


def test_oncall_ops_subjects_excludes_other_members_ack(db_conn, oncall_cfg):
    # A bot ACK that mentions a DIFFERENT member must not be credited to OPSER.
    _slack(db_conn, eid="x1r", ts="2026-06-11T09:00:00Z", actor="U0BOT",
           channel="C0ONCALL", thread="TX", etype="thread_started", body="New Issue")
    _slack(db_conn, eid="x1a", ts="2026-06-11T09:02:00Z", actor="U0BOT",
           channel="C0ONCALL", thread="TX", etype="thread_reply",
           body="<@U0OTHER|Other> acknowledged the issue.")
    db_conn.commit()
    assert pv._oncall_ops_subjects(db_conn, OPSER, "2026-03-01T00:00:00Z", B_UNTIL) == set()


def test_baseline_role_oncall_involvement_labels_ops(db_conn, oncall_cfg):
    # With the shared on-call config wired, OPSER's involvement → role "ops".
    _seed_oncall_work(db_conn)
    role, basis = pv._baseline_role(db_conn, OPSER, B_UNTIL)
    assert role == "ops"
    assert "'ops': 4" in basis


def test_baseline_role_mixed_without_oncall_config(db_conn, monkeypatch, tmp_path):
    # Same events, but on-call config ABSENT (public clone / CI): the handle and
    # class:oncall signals go inert → ops=0 → no discriminating signal → "mixed".
    # This is exactly the blind spot the fix closes when config IS present.
    import derive.oncall_signals as oc
    import derive.retro_census as rc
    monkeypatch.setattr(oc, "_CHANNELS_YAML", tmp_path / "missing.yaml")
    monkeypatch.setattr(oc, "_SUBTEAMS_YAML", tmp_path / "missing.yaml")
    monkeypatch.setattr(rc, "_REPO_ROOT", tmp_path)  # no config/ dir under it
    _seed_oncall_work(db_conn)
    role, _ = pv._baseline_role(db_conn, OPSER, B_UNTIL)
    assert role == "mixed"

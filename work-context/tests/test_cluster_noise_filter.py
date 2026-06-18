"""derive/cluster_noise_filter.py — channel exclusion decision.

compute_excluded scores each slack channel and excludes automation channels via:
  tier 4  RECURRING-cluster ratio (label-based, data-rich channels)
  tier 5  automation-ROOT content share (GENERIC, label-independent)
  tier 6  name bootstrap (sparse channels)
honouring force-include/exclude + protected classes. Driven on a controlled temp
DB with config + channel-meta stubbed (so it never reads the live yaml).
"""

from __future__ import annotations

import pytest

from derive import cluster_noise_filter as cnf


def _cfg(**over):
    base = {
        "noise_ratio_threshold": 0.90, "min_subjects_for_ratio": 5,
        "channel_automation_share": 0.90, "protect_classes": ["team"],
        "name_patterns": [], "automation_patterns": ["[firing", "request approved for"],
        "force_include": [], "force_exclude": [],
    }
    base.update(over)
    return base


def _seed_channel(conn, channel_id, n_recurring, n_real, root_text=""):
    """n_recurring RECURRING + n_real non-recurring clusters, one member subject
    each, whose event lives in `channel_id` (root title = root_text)."""
    cid = conn.execute("SELECT COALESCE(MAX(cluster_id),0) FROM topic_brief").fetchone()[0]
    for status, count in (("RECURRING", n_recurring), ("ACTIVE", n_real)):
        for _ in range(count):
            cid += 1
            subj = f"slack:{channel_id}:170000000{cid:03d}.0"
            conn.execute("INSERT INTO topic_brief (cluster_id, label, status) VALUES (?,?,?)",
                         (cid, f"c{cid}", status))
            conn.execute("INSERT INTO topic_brief_member (cluster_id, subject, source) VALUES (?,?,?)",
                         (cid, subj, "slack"))
            conn.execute(
                "INSERT INTO events (id, source, event_type, ts, subject, channel_id, title, raw_path) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"e{cid}", "slack", "thread_started", "2026-06-10T00:00:00Z", subj,
                 channel_id, root_text, f"raw/slack/x#{cid}"))
    conn.commit()


def _seed_unlabeled(conn, channel_id, root_text, n):
    """n slack subjects in channel with the given root title, but NO topic_brief
    row (NULL-status) — proves tier-5 is label-independent."""
    base = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    for i in range(n):
        subj = f"slack:{channel_id}:18000000{base + i:04d}.0"
        conn.execute(
            "INSERT INTO events (id, source, event_type, ts, subject, channel_id, title, raw_path) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"u{base + i}", "slack", "thread_started", "2026-06-10T00:00:00Z", subj,
             channel_id, root_text, f"raw/slack/u#{base + i}"))
    conn.commit()


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(cnf, "load_config", lambda: _cfg())
    monkeypatch.setattr(cnf, "_channel_meta", lambda: {
        "C0NOISE": {"id": "C0NOISE", "name": "alerts-feed", "class": ""},
        "C0REAL":  {"id": "C0REAL", "name": "eng-discuss", "class": ""},
    })


def test_high_noise_channel_excluded_by_ratio(db_conn, cfg):
    _seed_channel(db_conn, "C0NOISE", n_recurring=9, n_real=1)   # 90% recurring
    _seed_channel(db_conn, "C0REAL", n_recurring=1, n_real=9)    # 10% recurring
    out = cnf.compute_excluded(db_conn)
    assert out["C0NOISE"]["reason"] == "ratio"
    assert "C0REAL" not in out


def test_force_exclude(db_conn, monkeypatch):
    monkeypatch.setattr(cnf, "load_config", lambda: _cfg(protect_classes=[], force_exclude=["C0REAL"]))
    monkeypatch.setattr(cnf, "_channel_meta", lambda: {"C0REAL": {"id": "C0REAL", "name": "eng"}})
    _seed_channel(db_conn, "C0REAL", n_recurring=0, n_real=8)
    assert cnf.compute_excluded(db_conn)["C0REAL"]["reason"] == "force_exclude"


def test_force_include_overrides(db_conn, monkeypatch):
    monkeypatch.setattr(cnf, "load_config", lambda: _cfg(protect_classes=[], force_include=["C0NOISE"]))
    monkeypatch.setattr(cnf, "_channel_meta", lambda: {"C0NOISE": {"id": "C0NOISE", "name": "x"}})
    _seed_channel(db_conn, "C0NOISE", n_recurring=9, n_real=1)
    assert "C0NOISE" not in cnf.compute_excluded(db_conn)


def test_content_share_catches_unlabeled_alert_channel(db_conn, monkeypatch):
    # NULL-status channel (no topic_brief rows) whose roots are automation →
    # ratio can't see it (tot=0) but tier-5 content share does.
    monkeypatch.setattr(cnf, "load_config", lambda: _cfg())
    monkeypatch.setattr(cnf, "_channel_meta", lambda: {"C0ALERT": {"id": "C0ALERT", "name": "incidents-feed"}})
    _seed_unlabeled(db_conn, "C0ALERT", "[FIRING:1] disk full", 12)
    out = cnf.compute_excluded(db_conn)
    assert out["C0ALERT"]["reason"] == "content-share"


def test_content_share_keeps_mixed_triage_channel(db_conn, monkeypatch):
    # half automation roots, half human → share 0.5 < 0.90 → kept.
    monkeypatch.setattr(cnf, "load_config", lambda: _cfg())
    monkeypatch.setattr(cnf, "_channel_meta", lambda: {"C0MIX": {"id": "C0MIX", "name": "oncall-mixed"}})
    _seed_unlabeled(db_conn, "C0MIX", "[FIRING:1] x", 6)
    _seed_unlabeled(db_conn, "C0MIX", "hey can someone help debug this?", 6)
    assert "C0MIX" not in cnf.compute_excluded(db_conn)


def test_name_bootstrap_for_sparse_channel(db_conn, monkeypatch):
    monkeypatch.setattr(cnf, "load_config", lambda: _cfg(name_patterns=["alert"]))
    monkeypatch.setattr(cnf, "_channel_meta", lambda: {"C0NEW": {"id": "C0NEW", "name": "txn-alerts"}})
    _seed_channel(db_conn, "C0NEW", n_recurring=2, n_real=0)   # 2 subjects < min_n=5
    assert cnf.compute_excluded(db_conn)["C0NEW"]["reason"] == "name-bootstrap"


def test_refresh_and_excluded_subjects(db_conn, cfg):
    _seed_channel(db_conn, "C0NOISE", n_recurring=9, n_real=1)
    cnf.refresh(db_conn)
    assert "C0NOISE" in cnf.excluded_channel_ids(db_conn)
    subs = cnf.excluded_subjects(db_conn)
    assert subs and all(s.startswith("slack:C0NOISE:") for s in subs)

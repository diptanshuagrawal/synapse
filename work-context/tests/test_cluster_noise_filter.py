"""derive/cluster_noise_filter.py — channel exclusion decision.

compute_excluded scores each slack channel by its RECURRING-cluster share and
excludes high-noise channels (ratio ≥ threshold over ≥min_n subjects), honouring
force-include/exclude + protected classes. Driven on a controlled temp DB with
config + channel-meta stubbed (so it never reads the live yaml).
"""

from __future__ import annotations

import pytest

from derive import cluster_noise_filter as cnf


def _seed_channel(conn, channel_id, n_recurring, n_real):
    """Create n_recurring RECURRING + n_real non-recurring clusters, each with one
    member subject whose event lives in `channel_id`."""
    cid = conn.execute("SELECT COALESCE(MAX(cluster_id),0) FROM topic_brief").fetchone()[0]
    n = 0
    for status, count in (("RECURRING", n_recurring), ("ACTIVE", n_real)):
        for _ in range(count):
            cid += 1
            n += 1
            subj = f"slack:{channel_id}:170000000{cid:03d}.0"
            conn.execute("INSERT INTO topic_brief (cluster_id, label, status) VALUES (?,?,?)",
                         (cid, f"c{cid}", status))
            conn.execute("INSERT INTO topic_brief_member (cluster_id, subject, source) VALUES (?,?,?)",
                         (cid, subj, "slack"))
            conn.execute(
                "INSERT INTO events (id, source, event_type, ts, subject, channel_id, raw_path) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"e{cid}", "slack", "thread_started", "2026-06-10T00:00:00Z", subj,
                 channel_id, f"raw/slack/x#{cid}"))
    conn.commit()


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(cnf, "load_config", lambda: {
        "noise_ratio_threshold": 0.90, "min_subjects_for_ratio": 5,
        "protect_classes": ["team"], "name_patterns": [],
        "force_include": [], "force_exclude": [],
    })
    monkeypatch.setattr(cnf, "_channel_meta", lambda: {
        "C0NOISE": {"id": "C0NOISE", "name": "alerts-feed", "class": ""},
        "C0REAL":  {"id": "C0REAL", "name": "eng-discuss", "class": ""},
    })


def test_high_noise_channel_excluded(db_conn, cfg):
    _seed_channel(db_conn, "C0NOISE", n_recurring=9, n_real=1)   # 90% recurring
    _seed_channel(db_conn, "C0REAL", n_recurring=1, n_real=9)    # 10% recurring
    out = cnf.compute_excluded(db_conn)
    assert "C0NOISE" in out and out["C0NOISE"]["reason"] == "ratio"
    assert "C0REAL" not in out


def test_below_min_subjects_not_excluded(db_conn, cfg):
    # only 3 subjects (< min_n=5) even though all recurring → not excluded by ratio.
    _seed_channel(db_conn, "C0NOISE", n_recurring=3, n_real=0)
    assert "C0NOISE" not in cnf.compute_excluded(db_conn)


def test_force_exclude(db_conn, monkeypatch):
    monkeypatch.setattr(cnf, "load_config", lambda: {
        "noise_ratio_threshold": 0.90, "min_subjects_for_ratio": 5,
        "protect_classes": [], "name_patterns": [],
        "force_include": [], "force_exclude": ["C0REAL"],
    })
    monkeypatch.setattr(cnf, "_channel_meta", lambda: {"C0REAL": {"id": "C0REAL", "name": "eng"}})
    _seed_channel(db_conn, "C0REAL", n_recurring=0, n_real=8)    # all real
    out = cnf.compute_excluded(db_conn)
    assert out["C0REAL"]["reason"] == "force_exclude"


def test_force_include_overrides_ratio(db_conn, monkeypatch):
    monkeypatch.setattr(cnf, "load_config", lambda: {
        "noise_ratio_threshold": 0.90, "min_subjects_for_ratio": 5,
        "protect_classes": [], "name_patterns": [],
        "force_include": ["C0NOISE"], "force_exclude": [],
    })
    monkeypatch.setattr(cnf, "_channel_meta", lambda: {"C0NOISE": {"id": "C0NOISE", "name": "x"}})
    _seed_channel(db_conn, "C0NOISE", n_recurring=9, n_real=1)   # would be ratio-excluded
    assert "C0NOISE" not in cnf.compute_excluded(db_conn)        # force_include wins


def test_refresh_and_excluded_subjects(db_conn, cfg):
    _seed_channel(db_conn, "C0NOISE", n_recurring=9, n_real=1)
    cnf.refresh(db_conn)
    assert "C0NOISE" in cnf.excluded_channel_ids(db_conn)
    subs = cnf.excluded_subjects(db_conn)
    assert subs and all(s.startswith("slack:C0NOISE:") for s in subs)

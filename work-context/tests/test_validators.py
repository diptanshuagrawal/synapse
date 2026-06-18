"""derive/{jira,github,confluence,topic_brief}_validate.py — compute() on seed.

The per-source validators are pure compute(conn) → {findings, counts}. Run them
against the seeded multi-source DB (whose actors are all scope:team) and assert
the contract: well-formed findings, correct event counts, and a clean
attribution verdict when every actor resolves. These are the checks cron-status
renders, so their output shape is load-bearing.
"""

from __future__ import annotations

import pytest

from derive import jira_validate, github_validate, confluence_validate, topic_brief_validate


def _findings_ok(report):
    assert "findings" in report
    for row in report["findings"]:
        assert len(row) == 3
        assert row[0] in ("PASS", "WARN", "FAIL")


def _attribution(report):
    return next((r for r in report["findings"] if r[1] == "attribution"), None)


# ── jira_validate ────────────────────────────────────────────────────────────

def test_jira_validate_clean_attribution(seeded_db):
    rep = jira_validate.compute(seeded_db)
    _findings_ok(rep)
    assert rep["n_total_events"] == 5            # epic+story+assign+2 status
    assert _attribution(rep)[0] == "PASS"        # all seed actors scope=team


def test_jira_validate_status_capture_present(seeded_db):
    rep = jira_validate.compute(seeded_db)
    sc = next((r for r in rep["findings"] if r[1] == "status_capture"), None)
    assert sc is not None and sc[0] in ("PASS", "WARN", "FAIL")


# ── github_validate ──────────────────────────────────────────────────────────

def test_github_validate_clean(seeded_db):
    rep = github_validate.compute(seeded_db)
    _findings_ok(rep)
    assert rep["n_total_events"] >= 3
    assert _attribution(rep)[0] == "PASS"


# ── confluence_validate ──────────────────────────────────────────────────────

def test_confluence_validate_clean(seeded_db):
    rep = confluence_validate.compute(seeded_db)
    _findings_ok(rep)
    assert rep["n_total_events"] == 1
    assert _attribution(rep)[0] == "PASS"


# ── topic_brief_validate ─────────────────────────────────────────────────────

def test_topic_brief_validate_runs_on_seed(seeded_db):
    rep = topic_brief_validate.compute(seeded_db)
    _findings_ok(rep)
    assert rep["n_total"] == 1   # one seeded cluster


def test_topic_brief_validate_empty_db(db_conn):
    # No clusters → n_total 0, still well-formed.
    rep = topic_brief_validate.compute(db_conn)
    assert rep["n_total"] == 0

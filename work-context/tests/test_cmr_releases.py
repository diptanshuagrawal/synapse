"""derive/cmr_releases.py — CMR body parsing + release lifecycle.

CMRs are the rollout record (the deploy-timestamp source of truth). cmr_releases
parses the templated CMR body + status timeline into feature_release rows. The
pure parsers (field extraction, PR-link normalisation, outcome priority,
approver attribution, impacted→slug) are pinned directly; parse_cmrs is run
end-to-end against a hand-built CMR in a temp DB.
"""

from __future__ import annotations

import json

import pytest

from ingest import common
from derive import cmr_releases as cr


# ── _field ───────────────────────────────────────────────────────────────────

def test_field_extracts_value_and_stops_at_next_label():
    body = "Service: payments-svc\nPR: https://x\nOwner of release: alice"
    assert cr._field(body, "Service:") == "payments-svc"
    assert cr._field(body, "Owner of release:") == "alice"


def test_field_missing_returns_none():
    assert cr._field("no fields here", "Service:") is None


# ── _pr_subjects ─────────────────────────────────────────────────────────────

def test_pr_subjects_normalises_and_dedupes():
    body = ("https://github.com/org/repo/pull/10 and "
            "https://github.com/org/repo/pull/10 and "
            "https://github.com/org/other/pull/3")
    assert cr._pr_subjects(body) == ["org/repo#10", "org/other#3"]


def test_pr_subjects_none():
    assert cr._pr_subjects("no links") == []


# ── _outcome (priority order) ────────────────────────────────────────────────

@pytest.mark.parametrize("statuses,expected", [
    (["Released", "Rolled Back"], "rolled_back"),   # rolled_back wins
    (["Released", "Emergency change"], "emergency"),  # emergency over released
    (["Change Approved", "Released"], "released"),
    (["Cancelled"], "cancelled"),
    (["To Do", "In Review"], "pending"),
])
def test_outcome_priority(statuses, expected):
    assert cr._outcome(statuses) == expected


# ── _approved_by ─────────────────────────────────────────────────────────────

def test_approved_by_picks_short_approved_comment():
    comments = [
        ("2026-06-01T09:00:00Z", "bob", "looks fine, approved"),     # long-ish but startswith? no
        ("2026-06-01T10:00:00Z", "carol", "Approved"),               # short, startswith approved
    ]
    assert cr._approved_by(comments, "2026-06-01T12:00:00Z") == "carol"


def test_approved_by_ignores_after_approval_ts():
    comments = [("2026-06-02T10:00:00Z", "carol", "Approved")]
    # approval transition happened before this comment → not counted.
    assert cr._approved_by(comments, "2026-06-01T00:00:00Z") is None


def test_approved_by_ignores_long_bodies():
    comments = [("2026-06-01T10:00:00Z", "carol", "Approved " + "x" * 50)]
    assert cr._approved_by(comments, None) is None


# ── _slugs_from_impacted ─────────────────────────────────────────────────────

def test_slugs_from_impacted_keyword_match():
    kw = [("payments", ["payout", "withholding"]), ("ledger", ["ledger"])]
    assert cr._slugs_from_impacted("payout pipeline change", kw) == ["payments"]


@pytest.mark.parametrize("impacted", [None, "", "none", "N/A"])
def test_slugs_from_impacted_empty_sentinels(impacted):
    assert cr._slugs_from_impacted(impacted, [("payments", ["payout"])]) == []


# ── parse_cmrs (end-to-end on a temp DB) ─────────────────────────────────────

def test_parse_cmrs_builds_release_record(db_conn, monkeypatch):
    monkeypatch.setattr(cr, "_load_project_keywords",
                        lambda: [("payments", ["payout"])])

    body = ("Service: payments-svc\n"
            "PR: https://github.com/org/repo/pull/10\n"
            "Impacted Areas: payout flow\n"
            "Owner of release: alice")
    common.insert_event(db_conn, common.Event(
        id="cmr1:created", source="jira", event_type="issue_created",
        ts="2026-06-01T09:00:00Z", actor="alice@x.com", subject="CMR-1",
        title="CMR payout deploy", body=body, url="u", issue_type="CMR"))
    common.insert_event(db_conn, common.Event(
        id="cmr1:status:1", source="jira", event_type="status_change",
        ts="2026-06-02T09:00:00Z", actor="alice@x.com", subject="CMR-1",
        title="status", body="", url="u", to_status="Released"))

    recs = cr.parse_cmrs(db_conn)
    assert len(recs) == 1
    r = recs[0]
    assert r["cmr_subject"] == "CMR-1"
    assert r["service"] == "payments-svc"
    assert json.loads(r["pr_urls_json"]) == ["org/repo#10"]
    assert r["release_owner"] == "alice"
    assert r["outcome"] == "released"
    assert r["released_at"] == "2026-06-02T09:00:00Z"
    assert r["is_feature_release"] == 1
    assert r["slug"] == "payments"          # keyword match on impacted areas


def test_parse_cmrs_empty_db(db_conn, monkeypatch):
    monkeypatch.setattr(cr, "_load_project_keywords", lambda: [])
    assert cr.parse_cmrs(db_conn) == []

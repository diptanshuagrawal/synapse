"""ingest/jira.py — ingest_project main loop (fake client, no network).

ingest_project drives search_issues → normalize (issue_created + changelog
events) → store, plus per-issue comments and identity-signal capture. Driven
with a fake JiraClient serving one issue (with a status change + a comment)
into a temp DB. identity_signals table is initialised first (record_user_dict
writes to it).
"""

from __future__ import annotations

import logging

import pytest

from ingest import jira as jira_mod
from derive import identity_signals


ISSUE = {
    "key": "EX-100",
    "fields": {
        "summary": "fix payout rounding", "description": "", "issuetype": {"name": "Task"},
        "created": "2026-06-10T09:00:00.000+0000", "updated": "2026-06-10T10:00:00.000+0000",
        "creator": {"emailAddress": "alice@example.com", "displayName": "Alice"},
        "status": {"name": "To Do"},
    },
    "changelog": {"histories": [{
        "created": "2026-06-10T10:00:00.000+0000",
        "author": {"emailAddress": "alice@example.com", "displayName": "Alice"},
        "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}],
    }]},
}
COMMENT = {"id": "c1", "created": "2026-06-10T11:00:00.000+0000",
           "author": {"emailAddress": "alice@example.com"}, "body": "on it"}


class _FakeJira:
    def search_issues(self, jql, fields, expand):
        yield ISSUE

    def issue_comments(self, key):
        yield COMMENT


@pytest.fixture
def conn(db_conn):
    identity_signals.init(db_conn)   # record_user_dict target
    return db_conn


def test_ingest_project_stores_events(conn, patch_config):
    log = logging.getLogger("test-jira")
    new, dup, max_updated = jira_mod.ingest_project(
        _FakeJira(), "x.atlassian.net", "EX", None, conn, dry_run=False, log=log)
    assert new >= 3                     # issue_created + status_change + comment
    assert max_updated and max_updated.startswith("2026-06-10")
    types = {r[0] for r in conn.execute(
        "SELECT event_type FROM events WHERE subject='EX-100'")}
    assert {"issue_created", "status_change", "comment"} <= types


def test_ingest_project_records_identity_signals(conn, patch_config):
    log = logging.getLogger("test-jira")
    jira_mod.ingest_project(_FakeJira(), "x.atlassian.net", "EX", None, conn, False, log)
    n = conn.execute("SELECT COUNT(*) FROM identity_signals").fetchone()[0]
    assert n >= 1                       # alice's email↔name pair captured


def test_ingest_project_dedups_on_rerun(conn, patch_config):
    log = logging.getLogger("test-jira")
    jira_mod.ingest_project(_FakeJira(), "x.atlassian.net", "EX", None, conn, False, log)
    new, dup, _ = jira_mod.ingest_project(
        _FakeJira(), "x.atlassian.net", "EX", None, conn, False, log)
    assert new == 0 and dup >= 3        # all duplicates second time

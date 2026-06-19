"""ingest/confluence.py — ingest_pages main loop (fake client, no network).

ingest_pages walks the v2 pages feed, keeps only team-authored versions, and
emits page_created/page_updated. Driven with a fake ConfluenceClient serving
one team-authored page into a temp DB; covers the team filter + the
older-than-cursor break.
"""

from __future__ import annotations

import logging

import pytest

from ingest import confluence as cf

TEAM = {"acc-alice"}


def _page(author="acc-alice", ts="2026-06-10T09:00:00.000Z", num=1, pid="999"):
    return {
        "id": pid, "title": "Ledger design", "authorId": author, "spaceId": "S1",
        "createdAt": ts,
        "version": {"number": num, "authorId": author, "createdAt": ts},
        "body": {"storage": {"value": "ledger reconciliation notes"}},
    }


class _FakeConf:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, path, params=None):
        return self._pages

    def get_page_title(self, pid):
        return "Ledger design"


def test_ingest_pages_stores_team_authored(db_conn, patch_config):
    log = logging.getLogger("test-conf")
    new, dup = cf.ingest_pages(_FakeConf([_page()]), "x.atlassian.net", None,
                               TEAM, db_conn, dry_run=False, log=log)
    assert new == 1 and dup == 0
    row = db_conn.execute(
        "SELECT event_type, actor FROM events WHERE subject='page:999'").fetchone()
    assert row and row["event_type"] == "page_created" and row["actor"] == "acc-alice"


def test_ingest_pages_filters_non_team(db_conn, patch_config):
    log = logging.getLogger("test-conf")
    new, dup = cf.ingest_pages(_FakeConf([_page(author="acc-stranger")]),
                               "x.atlassian.net", None, TEAM, db_conn, False, log)
    assert new == 0 and dup == 0      # non-team author dropped


def test_ingest_pages_since_cursor_breaks(db_conn, patch_config):
    log = logging.getLogger("test-conf")
    # page older than cursor → loop breaks before storing.
    new, dup = cf.ingest_pages(_FakeConf([_page(ts="2026-01-01T00:00:00.000Z")]),
                               "x.atlassian.net", "2026-06-01T00:00:00Z", TEAM,
                               db_conn, False, log)
    assert new == 0


def test_ingest_pages_dedups(db_conn, patch_config):
    log = logging.getLogger("test-conf")
    cf.ingest_pages(_FakeConf([_page()]), "x.atlassian.net", None, TEAM, db_conn, False, log)
    new, dup = cf.ingest_pages(_FakeConf([_page()]), "x.atlassian.net", None, TEAM, db_conn, False, log)
    assert new == 0 and dup == 1

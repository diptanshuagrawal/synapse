"""ingest/github.py — ingest_repo main loop (fake client, no network).

ingest_repo is the fetch→normalize→store driver: paginate PRs, then per-PR
reviews/comments/commits + pr_meta (detail + CI checks). Driven with a
path-dispatching fake GitHubClient serving one open PR with empty sub-resources,
into a temp events.db — exercises the loop, pr_meta upsert, and CI-check fetch
without touching api.github.com.
"""

from __future__ import annotations

import logging

import pytest

from ingest import github as gh


class _FakeGH:
    """Dispatches GitHubClient.get/paginate by path. One open PR (#7)."""
    PR = {
        "number": 7, "state": "open", "title": "add payout retry", "body": "see EX-2301",
        "user": {"login": "alice-gh"}, "created_at": "2026-06-10T00:00:00Z",
        "updated_at": "2026-06-10T00:00:00Z", "html_url": "https://github.com/org/repo/pull/7",
        "head": {"sha": "abc123"}, "merged_at": None,
        "additions": 10, "deletions": 2, "changed_files": 1,
    }

    def get(self, path, params=None):
        if path.endswith("/pulls"):
            return [self.PR] if (params or {}).get("page", 1) == 1 else []
        if path.endswith("/pulls/7"):
            return self.PR
        if path.endswith("/check-runs"):
            return {"check_runs": []}
        return {}

    def paginate(self, path, params=None):
        return []   # no reviews / comments / commits


def test_ingest_repo_stores_pr_and_meta(db_conn, patch_config):
    log = logging.getLogger("test-gh")
    new, dup = gh.ingest_repo(_FakeGH(), "org/repo", since=None, conn=db_conn,
                              dry_run=False, include_diffs=False, log=log)
    assert new >= 1 and dup == 0
    # the PR event landed.
    row = db_conn.execute(
        "SELECT actor, title FROM events WHERE subject='org/repo#7' AND event_type='pr_opened'"
    ).fetchone()
    assert row and row["actor"] == "alice-gh"
    # pr_meta upserted from the detail fetch.
    meta = db_conn.execute("SELECT additions, files_changed FROM pr_meta WHERE subject='org/repo#7'").fetchone()
    assert meta and meta["additions"] == 10


def test_ingest_repo_dedups_on_rerun(db_conn, patch_config):
    log = logging.getLogger("test-gh")
    gh.ingest_repo(_FakeGH(), "org/repo", None, db_conn, False, False, log)
    new, dup = gh.ingest_repo(_FakeGH(), "org/repo", None, db_conn, False, False, log)
    assert new == 0 and dup >= 1     # second run is all duplicates


def test_ingest_repo_since_filter_excludes_old(db_conn, patch_config):
    log = logging.getLogger("test-gh")
    # since AFTER the PR's updated_at → the PR is filtered out, nothing stored.
    new, dup = gh.ingest_repo(_FakeGH(), "org/repo", "2026-07-01T00:00:00Z",
                              db_conn, False, False, log)
    assert new == 0 and dup == 0

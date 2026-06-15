"""Cursor + success-date state management.

Cursors are the ingest watermark — read_cursor/write_cursor round-tripping
correctly (and surviving an interleaved read-modify-write across sources) is
what stops ingest from re-fetching the world or skipping a window. The
success-date marker is the gate cron-status + run-*.sh use to decide a source
already succeeded today.
"""

from __future__ import annotations

from datetime import datetime

from ingest import common


def test_read_cursor_missing_returns_none(tmp_paths):
    assert common.read_cursor("jira") is None


def test_write_then_read_cursor(tmp_paths):
    common.write_cursor("jira", "2026-06-10T00:00:00Z")
    assert common.read_cursor("jira") == "2026-06-10T00:00:00Z"


def test_write_cursor_preserves_other_sources(tmp_paths):
    # The read-modify-write must not clobber a sibling source's cursor.
    common.write_cursor("jira", "J1")
    common.write_cursor("github", "G1")
    common.write_cursor("jira", "J2")
    assert common.read_cursor("jira") == "J2"
    assert common.read_cursor("github") == "G1"


def test_write_success_date_writes_today(tmp_paths):
    common.write_success_date("jira")
    marker = tmp_paths.state_path.parent / "last_jira_success.date"
    assert marker.read_text().strip() == datetime.now().strftime("%Y-%m-%d")

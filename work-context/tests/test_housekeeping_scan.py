"""derive/housekeeping_scan.py — the deterministic candidate emitter.

Builds a throwaway repo tree with one artifact per category and asserts the scan
surfaces each as a candidate, never proposes the live events.db, and skips the
run-once markers. Thresholds are monkeypatched low so the fixture stays tiny.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_DERIVE = Path(__file__).resolve().parent.parent
if str(_DERIVE) not in sys.path:
    sys.path.insert(0, str(_DERIVE))

from derive import housekeeping_scan as hks  # noqa: E402

OLD = 40 * 86400  # seconds — comfortably past the 30d stale thresholds


def _age(path: Path, secs_ago: float) -> None:
    t = time.time() - secs_ago
    os.utime(path, (t, t))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A repo root with work-context/ and one artifact per scan category."""
    monkeypatch.setattr(hks, "LOG_BIG_BYTES", 100)        # 100B is "oversized" here
    monkeypatch.setattr(hks, "UNTRACKED_BIG", 100)
    monkeypatch.setattr(hks, "LARGE_FILE_BYTES", 10_000_000)  # keep large_file out of the fixture

    wc = tmp_path / "work-context"
    for d in ("index", "logs", "derived", "state", "bin"):
        (wc / d).mkdir(parents=True)

    (wc / "index" / "events.db").write_text("LIVE DB")                 # must be SKIPPED
    bak = wc / "index" / "events.db.bak.20260101T000000"
    bak.write_text("x" * 500)                          # standard backup — 60d rule owns it (NOT carded)
    globmiss = wc / "index" / "events.db.pre-ownership-rollup.bak"
    globmiss.write_text("y" * 900)                     # non-standard name — glob-miss, MUST be carded

    log = wc / "logs" / "ingest.log"
    log.write_text("y" * 500)                                         # log (>100B)

    pyc = wc / "bin" / "__pycache__"
    pyc.mkdir()
    (pyc / "mod.cpython-313.pyc").write_text("bytecode")              # pycache

    dj = wc / "derived" / "old_report.json"
    dj.write_text("{}")
    _age(dj, OLD)                                                     # derived_stale

    sj = wc / "state" / "old_dump.json"
    sj.write_text("{}")
    _age(sj, OLD)                                                     # state_orphan
    keep1 = wc / "state" / "last_routine_x_success.date"             # skipped (marker)
    keep1.write_text("2026-01-01"); _age(keep1, OLD)
    keep2 = wc / "state" / "housekeeping_candidates.json"            # skipped (our own)
    keep2.write_text("{}"); _age(keep2, OLD)

    (wc / "bin" / "dashboard_v1_preview.html").write_text("<html>")  # preview_bloat
    return tmp_path


def test_scan_surfaces_every_category(tree):
    data = hks.scan(str(tree))
    cats = {c["category"] for c in data["candidates"]}
    assert {"db_backup", "log", "pycache", "derived_stale",
            "state_orphan", "preview_bloat"} <= cats


def test_live_db_is_never_a_candidate(tree):
    data = hks.scan(str(tree))
    paths = [c["path"] for c in data["candidates"]]
    assert not any(p.endswith("index/events.db") for p in paths)
    assert any("events.db" in s for s in data["skipped"])


def test_standard_backup_defers_to_60d_rule_glob_miss_is_carded(tree):
    """Standard events.db.bak* → NOT carded (60d prune owns it), surfaced as a pile-up note.
    Non-standard backup name (glob-miss) → carded, since the prune never matches it."""
    data = hks.scan(str(tree))
    paths = [c["path"] for c in data["candidates"]]
    # standard backup is deferred, not carded
    assert not any(p.endswith("events.db.bak.20260101T000000") for p in paths)
    assert any("standard events.db.bak* backup" in s for s in data["skipped"])
    # the glob-miss IS carded as a db_backup
    bks = [c for c in data["candidates"] if c["category"] == "db_backup"]
    assert any(p.endswith("events.db.pre-ownership-rollup.bak") for p in [c["path"] for c in bks])


def test_run_once_markers_are_skipped(tree):
    data = hks.scan(str(tree))
    orphans = [c["path"] for c in data["candidates"] if c["category"] == "state_orphan"]
    assert any(p.endswith("old_dump.json") for p in orphans)
    assert not any("last_routine" in p for p in orphans)
    assert not any("housekeeping_" in p for p in orphans)


def test_log_is_oversized_only(tree, monkeypatch):
    # a small log under the threshold should NOT appear
    monkeypatch.setattr(hks, "LOG_BIG_BYTES", 100_000)
    data = hks.scan(str(tree))
    assert not any(c["category"] == "log" for c in data["candidates"])


def test_summary_totals_match(tree):
    data = hks.scan(str(tree))
    assert data["summary"]["n"] == len(data["candidates"])
    assert data["summary"]["total_bytes"] == sum(c["size_bytes"] for c in data["candidates"])
    assert "run_id" in data and data["run_id"]

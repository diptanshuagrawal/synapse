"""atomic_write_* and append_raw — durability + raw JSONL line numbering.

atomic_write_text is the primitive behind every cursor / success-date /
validate-cache write; a partial write seen by a concurrent cron-status read is
the exact failure mode it exists to prevent. append_raw stamps the raw_path
back-reference (raw/<src>/YYYY/MM/DD.jsonl#N) that lets any event be traced to
its source line — the line number must be correct and collision-free.
"""

from __future__ import annotations

import json

import pytest

from ingest import common


# ── atomic_write_text / json ───────────────────────────────────────────────

def test_atomic_write_text_creates_and_replaces(tmp_path):
    p = tmp_path / "sub" / "f.txt"
    common.atomic_write_text(p, "v1")
    assert p.read_text() == "v1"
    common.atomic_write_text(p, "v2")
    assert p.read_text() == "v2"


def test_atomic_write_leaves_no_tempfiles(tmp_path):
    p = tmp_path / "f.txt"
    common.atomic_write_text(p, "hello")
    leftovers = [x.name for x in tmp_path.iterdir() if x.name != "f.txt"]
    assert leftovers == []


def test_atomic_write_json_round_trip(tmp_path):
    p = tmp_path / "d.json"
    common.atomic_write_json(p, {"b": 2, "a": 1}, sort_keys=True)
    assert json.loads(p.read_text()) == {"a": 1, "b": 2}


def test_atomic_write_failure_cleans_temp(tmp_path, monkeypatch):
    # Force the fdopen/write path to blow up; the sibling tempfile must not
    # be left behind, and the original file must be untouched.
    p = tmp_path / "f.txt"
    common.atomic_write_text(p, "original")

    real_replace = common.os.replace

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(common.os, "replace", boom)
    with pytest.raises(OSError):
        common.atomic_write_text(p, "new")
    monkeypatch.setattr(common.os, "replace", real_replace)

    assert p.read_text() == "original"
    leftovers = [x.name for x in tmp_path.iterdir() if x.name != "f.txt"]
    assert leftovers == []


# ── append_raw ─────────────────────────────────────────────────────────────

def test_append_raw_first_line_is_one(tmp_paths, make_event):
    ev = make_event(source="jira", ts="2026-06-10T08:00:00Z")
    rp = common.append_raw(ev)
    assert rp == "raw/jira/2026/06/10.jsonl#1"
    assert ev.raw_path == rp


def test_append_raw_increments_line_number(tmp_paths, make_event):
    a = make_event(source="jira", ts="2026-06-10T08:00:00Z")
    b = make_event(source="jira", ts="2026-06-10T09:00:00Z")
    common.append_raw(a)
    rp_b = common.append_raw(b)
    assert rp_b.endswith("10.jsonl#2")


def test_append_raw_buckets_by_source_and_day(tmp_paths, make_event):
    common.append_raw(make_event(source="jira", ts="2026-06-10T08:00:00Z"))
    common.append_raw(make_event(source="github", ts="2026-06-11T08:00:00Z"))
    jira_file = tmp_paths.raw_root / "jira" / "2026/06/10.jsonl"
    gh_file = tmp_paths.raw_root / "github" / "2026/06/11.jsonl"
    assert jira_file.exists() and gh_file.exists()
    # Each its own bucket → one line apiece.
    assert sum(1 for _ in jira_file.open()) == 1


def test_append_raw_payload_is_valid_jsonl(tmp_paths, make_event):
    ev = make_event(source="jira", ts="2026-06-10T08:00:00Z", title="hi")
    common.append_raw(ev)
    line = (tmp_paths.raw_root / "jira" / "2026/06/10.jsonl").read_text().strip()
    rec = json.loads(line)
    assert rec["title"] == "hi" and rec["raw_path"].endswith("#1")


def test_append_raw_dry_run_writes_no_file(tmp_paths, make_event):
    ev = make_event(source="jira", ts="2026-06-10T08:00:00Z")
    rp = common.append_raw(ev, dry_run=True)
    assert rp.endswith("#1")
    assert not (tmp_paths.raw_root / "jira").exists()

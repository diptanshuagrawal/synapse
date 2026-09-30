"""Planner cache write-back.

After a Jira write the on-disk initiatives caches must be updated, or a reload diffs the
plan against a pre-write number and shows an already-submitted row as pending again.

Also pins which files count as a cache: initiatives-in.json / initiatives-out.json share
the prefix but belong to the /plan sandbox and resolve-initiatives, with a different
schema, so a bare initiatives-*.json glob would corrupt them.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "derive"))

import sprint_server as ss


@pytest.fixture
def derived(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "DERIVED", str(tmp_path))
    return tmp_path


def _cache(path, budgets, epic="BOARD-1", init="OINT-1"):
    path.write_text(json.dumps({
        "generated": "2026-10-15", "v": 2, "pods": ["P"], "months": ["Oct", "Nov"],
        "initiatives": [{"key": init, "summary": "s", "status": "Open",
                         "epic": {"key": epic, "summary": "e"}, "budgets": budgets}]}))


def test_only_real_caches_are_touched(derived):
    _cache(derived / "initiatives-default.json", {"Oct": 0})
    _cache(derived / "initiatives-a1b2c3d4.json", {"Oct": 0})
    (derived / "initiatives-in.json").write_text('{"_instructions": "x", "initiatives": []}')
    (derived / "initiatives-out.json").write_text('{"_generated": "x", "initiatives": []}')
    names = sorted(os.path.basename(p) for p in ss._initiative_cache_files())
    assert names == ["initiatives-a1b2c3d4.json", "initiatives-default.json"]


def test_sandbox_files_survive_a_patch(derived):
    _cache(derived / "initiatives-default.json", {"Oct": 0})
    sandbox = derived / "initiatives-in.json"
    original = '{"_instructions": "keep me", "initiatives": [{"key": "OINT-1"}]}'
    sandbox.write_text(original)
    ss._patch_epic_budgets_cache({"diffs": [{"epic": "BOARD-1", "month": "Oct", "from": 0, "to": 30}],
                                  "applied": [{"epic": "BOARD-1", "ok": True}]})
    assert sandbox.read_text() == original, "the /plan sandbox file was modified"


def test_submit_writes_budgets_into_every_cache(derived):
    a, b = derived / "initiatives-default.json", derived / "initiatives-a1b2c3d4.json"
    _cache(a, {"Oct": 0, "Nov": 0})
    _cache(b, {"Oct": 25, "Nov": 0})          # a stale pod cache, as seen in the wild
    ss._patch_epic_budgets_cache({
        "diffs": [{"epic": "BOARD-1", "month": "Oct", "from": 25, "to": 30}],
        "applied": [{"epic": "BOARD-1", "ok": True}]})
    for f in (a, b):
        got = json.loads(f.read_text())["initiatives"][0]["budgets"]
        assert got["Oct"] == 30, f"{f.name} kept a stale budget: {got}"
        assert got["Nov"] == 0, "untouched months must not change"


def test_failed_epic_is_not_written_back(derived):
    a = derived / "initiatives-default.json"
    _cache(a, {"Oct": 25})
    ss._patch_epic_budgets_cache({
        "diffs": [{"epic": "BOARD-1", "month": "Oct", "from": 25, "to": 30}],
        "applied": [{"epic": "BOARD-1", "ok": False, "error": "403"}]})
    assert json.loads(a.read_text())["initiatives"][0]["budgets"]["Oct"] == 25, \
        "a failed write must not be cached as if it succeeded"


def test_dry_run_shape_is_a_noop(derived):
    a = derived / "initiatives-default.json"
    _cache(a, {"Oct": 25})
    ss._patch_epic_budgets_cache({"dryRun": True,
                                  "diffs": [{"epic": "BOARD-1", "month": "Oct", "from": 25, "to": 30}]})
    assert json.loads(a.read_text())["initiatives"][0]["budgets"]["Oct"] == 25


def test_unlink_clears_epic_and_budgets(derived):
    a = derived / "initiatives-default.json"
    _cache(a, {"Oct": 30})
    ss._clear_initiative_epic_cache("OINT-1")
    row = json.loads(a.read_text())["initiatives"][0]
    assert row["epic"] is None and row["budgets"] == {}


def test_column_patch_handles_a_cleared_epic(derived):
    """_clear_initiative_epic_cache sets epic to None; a later column patch must not crash."""
    a = derived / "initiatives-default.json"
    _cache(a, {"Oct": 30})
    ss._clear_initiative_epic_cache("OINT-1")
    ss._patch_initiative_cache_column("BOARD-1", "epicHealth", "At Risk", "h2")
    assert json.loads(a.read_text())["initiatives"][0]["epic"] is None

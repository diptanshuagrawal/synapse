"""bin/housekeeping_apply.py — the git-safe, idempotent apply.

Stands up a throwaway git repo (one tracked file, one ignored file) and asserts
the safety gates: tracked source is refused, paths outside the root are refused,
ignored artifacts delete/truncate, and a double-click is idempotent. Reject just
records a ledger entry. The apply module's path globals are monkeypatched onto
the temp repo so nothing touches the real tree.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import housekeeping_apply as hka  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo with work-context/, a tracked file, and an ignored artifact."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")

    wc = tmp_path / "work-context"
    (wc / "bin").mkdir(parents=True)
    (wc / "state").mkdir(parents=True)
    (wc / "index").mkdir(parents=True)
    (tmp_path / ".gitignore").write_text("work-context/state/\nwork-context/index/\n")
    tracked = wc / "bin" / "tracked.py"
    tracked.write_text("print('hi')\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "seed")

    ignored = wc / "state" / "junk.txt"
    ignored.write_text("z" * 50)

    monkeypatch.setattr(hka, "ROOT", str(tmp_path))
    monkeypatch.setattr(hka, "WC", str(wc))
    monkeypatch.setattr(hka, "STATE", str(wc / "state"))
    monkeypatch.setattr(hka, "NEVER", {
        os.path.abspath(str(tmp_path)), os.path.abspath(str(wc)),
        os.path.abspath(str(wc / "index" / "events.db")),
        os.path.abspath(str(tmp_path / ".git")),
    })

    class _R:
        pass
    r = _R()
    r.root = tmp_path
    r.tracked = tracked
    r.ignored = ignored
    return r


def test_refuses_tracked_file(repo):
    ok, why = hka._safe_target(str(repo.tracked))
    assert not ok and "git-tracked" in why


def test_refuses_outside_root(repo):
    ok, why = hka._safe_target("/etc/hosts")
    assert not ok and "outside the repo root" in why


def test_refuses_protected_paths(repo):
    ok, _ = hka._safe_target(str(repo.root))
    assert not ok
    ok, _ = hka._safe_target(str(repo.root / "work-context" / "index" / "events.db"))
    assert not ok


def test_ignored_artifact_is_deletable(repo):
    ok, why = hka._safe_target(str(repo.ignored))
    assert ok and why == ""


def test_delete_file_dry_then_live_then_idempotent(repo):
    sug = {"action": "delete_file", "abs_path": str(repo.ignored),
           "path": "work-context/state/junk.txt", "size_h": "50B"}
    ok, note = hka.do_apply(sug, dry=True)
    assert ok and "would delete" in note and repo.ignored.exists()

    ok, note = hka.do_apply(sug, dry=False)
    assert ok and not repo.ignored.exists()

    ok, note = hka.do_apply(sug, dry=False)            # double-click
    assert ok and "already gone" in note


def test_truncate_keeps_the_file(repo):
    log = repo.root / "work-context" / "state" / "big.log"
    log.write_text("x" * 1000)
    sug = {"action": "truncate", "abs_path": str(log),
           "path": "work-context/state/big.log", "size_h": "1K"}
    ok, note = hka.do_apply(sug, dry=False)
    assert ok and log.exists() and log.stat().st_size == 0


def test_worktree_remove_only_acts_on_registered_worktrees(repo):
    """worktree_remove must act ONLY on a path git reports as a real worktree of this
    repo — even one outside the root (allowed) — and refuse any other existing path."""
    wt = repo.root.parent / (repo.root.name + "_wt")     # sibling dir, OUTSIDE the repo root
    _git(repo.root, "worktree", "add", "-b", "hk-wt-test", str(wt))
    ok, note = hka.do_apply(
        {"action": "worktree_remove", "abs_path": str(wt), "path": "wt", "size_h": "1K"}, dry=True)
    assert ok and "would" in note                         # registered worktree → allowed

    notwt = repo.root / "work-context" / "state" / "notwt"
    notwt.mkdir()
    ok2, note2 = hka.do_apply(
        {"action": "worktree_remove", "abs_path": str(notwt),
         "path": "work-context/state/notwt", "size_h": "1K"}, dry=False)
    assert not ok2 and "not a registered worktree" in note2   # existing non-worktree → refused
    assert notwt.exists()
    _git(repo.root, "worktree", "remove", "--force", str(wt))


def test_delete_file_refuses_tracked_via_do_apply(repo):
    sug = {"action": "delete_file", "abs_path": str(repo.tracked),
           "path": "work-context/bin/tracked.py", "size_h": "1K"}
    ok, note = hka.do_apply(sug, dry=False)
    assert not ok and "git-tracked" in note
    assert repo.tracked.exists()


def test_main_reject_records_ledger(repo, monkeypatch):
    run_id = "20260624-1200"
    sp = Path(hka.suggestions_path(run_id))
    sp.write_text(json.dumps({"run_id": run_id, "suggestions": [
        {"key": "k1", "path": "work-context/state/junk.txt", "category": "state_orphan",
         "abs_path": str(repo.ignored), "action": "delete_file", "size_h": "50B"}]}))
    monkeypatch.setattr(sys, "argv",
                        ["prog", "--run-id", run_id, "--key", "k1", "--decision", "reject"])
    assert hka.main() == 0
    led = json.load(open(hka._ledger("rejected")))
    assert any(e["key"] == "k1" for e in led)
    assert repo.ignored.exists()  # reject never deletes


def test_main_approve_applies_and_logs(repo, monkeypatch):
    run_id = "20260624-1300"
    sp = Path(hka.suggestions_path(run_id))
    sp.write_text(json.dumps({"run_id": run_id, "suggestions": [
        {"key": "k2", "path": "work-context/state/junk.txt", "category": "state_orphan",
         "abs_path": str(repo.ignored), "action": "delete_file", "size_bytes": 50, "size_h": "50B"}]}))
    monkeypatch.setattr(sys, "argv",
                        ["prog", "--run-id", run_id, "--key", "k2", "--decision", "approve"])
    assert hka.main() == 0
    assert not repo.ignored.exists()
    led = json.load(open(hka._ledger("applied")))
    assert any(e["key"] == "k2" for e in led)

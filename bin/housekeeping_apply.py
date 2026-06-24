#!/usr/bin/env python3
"""housekeeping_apply.py — deterministic, git-safe apply for one housekeeping suggestion.

Invoked by the Relay bot when the owner clicks Approve/Reject on a housekeeping
card (bin/relay_bot.py, action_id `hk:<run-id>:<key>:approve|reject`). NEVER calls
an LLM. Acts on exactly ONE suggestion, looked up by key in
  work-context/state/housekeeping_suggestions_<run-id>.json
(written by the Claude classification layer).

  python3 bin/housekeeping_apply.py --run-id <id> --key <key> --decision approve|reject [--dry-run]

SAFETY (approve path re-validates before touching anything — the scan may be stale):
  • path must resolve INSIDE the repo root (no symlink escape, no '..')
  • the path must NOT be git-tracked  → tracked source is never deletable here
  • never the live events.db, the .git dir, or the repo / work-context root
  • idempotent: a target already gone counts as success (double-click safe)
Reject just records the key so re-scans/re-classification skip it; nothing is deleted.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WC = os.path.join(ROOT, "work-context")
STATE = os.path.join(WC, "state")

ACTIONS = {"delete_file", "delete_dir", "truncate", "worktree_remove"}
NEVER = {os.path.abspath(ROOT), os.path.abspath(WC),
         os.path.abspath(os.path.join(WC, "index", "events.db")),
         os.path.abspath(os.path.join(ROOT, ".git"))}


def suggestions_path(run_id: str) -> str:
    return os.path.join(STATE, f"housekeeping_suggestions_{run_id}.json")


def _ledger(name: str) -> str:
    return os.path.join(STATE, f"housekeeping_{name}.json")


def _append_ledger(name: str, entry: dict) -> None:
    p = _ledger(name)
    try:
        data = json.load(open(p)) if os.path.exists(p) else []
    except Exception:
        data = []
    data.append(entry)
    os.makedirs(STATE, exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f, indent=2)


def _registered_worktrees() -> set:
    """Realpaths of worktrees git reports for THIS repo (excluding the main checkout).
    worktree_remove only ever acts on a path in this set — never an arbitrary target."""
    out = set()
    try:
        r = subprocess.run(["git", "-C", ROOT, "worktree", "list", "--porcelain"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            main = os.path.realpath(ROOT)
            for line in r.stdout.splitlines():
                if line.startswith("worktree "):
                    p = os.path.realpath(line[len("worktree "):].strip())
                    if p != main:
                        out.add(p)
    except Exception:
        pass
    return out


def _is_tracked(rel: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", ROOT, "ls-files", "--error-unmatch", rel],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False  # if git can't answer, fall through to the other guards


def _safe_target(abs_path: str) -> tuple[bool, str]:
    """True + '' when abs_path is a deletable target; False + reason otherwise."""
    real = os.path.realpath(abs_path)
    root_real = os.path.realpath(ROOT)
    if os.path.commonpath([real, root_real]) != root_real:
        return False, f"refusing: {real} is outside the repo root"
    if real in NEVER or real == root_real:
        return False, f"refusing: {real} is a protected path"
    if real.startswith(os.path.join(root_real, ".git") + os.sep):
        return False, "refusing: path is inside .git"
    rel = os.path.relpath(real, root_real)
    if _is_tracked(rel):
        return False, f"refusing: {rel} is git-tracked (only ignored/untracked artifacts are deletable)"
    return True, ""


def do_apply(sug: dict, dry: bool) -> tuple[bool, str]:
    action = sug.get("action", "")
    abs_path = sug.get("abs_path") or os.path.join(ROOT, sug.get("path", ""))
    size_h = sug.get("size_h", "?")
    if action not in ACTIONS:
        return False, f"unknown action '{action}'"

    if action == "worktree_remove":
        # Worktrees legitimately live OUTSIDE the repo root, so _safe_target's
        # inside-root rule doesn't apply. Instead, only ever remove a path git
        # itself reports as a registered worktree of THIS repo (realpath-compared,
        # so a symlink can't smuggle in an arbitrary target).
        real = os.path.realpath(abs_path)
        if real in NEVER or real == os.path.realpath(ROOT):
            return False, "refusing: protected path"
        if not os.path.exists(abs_path):
            return True, f"worktree {sug.get('path')} already gone — nothing to do"
        if real not in _registered_worktrees():
            return False, f"refusing: {sug.get('path')} is not a registered worktree of this repo"
        if dry:
            return True, f"🧪 would `git worktree remove --force {sug.get('path')}`"
        r = subprocess.run(["git", "-C", ROOT, "worktree", "remove", "--force", abs_path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, (r.stderr.strip() or "worktree remove failed")
        return True, f"removed worktree {sug.get('path')} ({size_h} reclaimed)"

    ok, why = _safe_target(abs_path)
    if not ok:
        return False, why

    if not os.path.exists(abs_path):
        return True, f"{sug.get('path')} already gone — nothing to do"

    if action == "truncate":
        if dry:
            return True, f"🧪 would truncate {sug.get('path')} to 0 ({size_h})"
        open(abs_path, "w").close()
        return True, f"truncated {sug.get('path')} ({size_h} freed)"

    if action == "delete_dir":
        if not os.path.isdir(abs_path):
            return False, f"{sug.get('path')} is not a directory"
        if dry:
            return True, f"🧪 would delete dir {sug.get('path')} ({size_h})"
        shutil.rmtree(abs_path)
        return True, f"deleted dir {sug.get('path')} ({size_h} reclaimed)"

    # delete_file
    if os.path.isdir(abs_path):
        return False, f"{sug.get('path')} is a directory — refusing delete_file"
    if dry:
        return True, f"🧪 would delete {sug.get('path')} ({size_h})"
    os.remove(abs_path)
    return True, f"deleted {sug.get('path')} ({size_h} reclaimed)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--decision", required=True, choices=["approve", "reject"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    p = suggestions_path(args.run_id)
    if not os.path.exists(p):
        print(f"no suggestions file for run {args.run_id} ({p})", file=sys.stderr)
        return 2
    data = json.load(open(p))
    sug = next((s for s in data.get("suggestions", []) if s.get("key") == args.key), None)
    if sug is None:
        print(f"key {args.key} not found in run {args.run_id}", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    if args.decision == "reject":
        if not args.dry_run:
            _append_ledger("rejected", {"key": args.key, "path": sug.get("path"),
                                        "category": sug.get("category"), "run_id": args.run_id, "at": stamp})
        print(f"🗑️ rejected {sug.get('path')} — recorded; won't be re-proposed")
        return 0

    ok, note = do_apply(sug, args.dry_run)
    if ok and not args.dry_run:
        _append_ledger("applied", {"key": args.key, "path": sug.get("path"),
                                   "category": sug.get("category"), "action": sug.get("action"),
                                   "size_bytes": sug.get("size_bytes"), "run_id": args.run_id, "at": stamp})
    print(("✅ " if ok else "⚠️ ") + note)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

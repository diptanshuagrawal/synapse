"""derive/housekeeping_render.py — fuse scan facts + verdicts deterministically.

Covers the bits that protect the owner: a git-tracked path can never be carded
for deletion (downgraded to review), rejected-ledger keys are dropped, only
delete/truncate/worktree_remove become Relay payload entries, and the action is
inferred from the category when the verdict doesn't override it.
"""
from __future__ import annotations

import sys
from pathlib import Path

_DERIVE = Path(__file__).resolve().parent.parent
if str(_DERIVE) not in sys.path:
    sys.path.insert(0, str(_DERIVE))

from derive import housekeeping_render as hkr  # noqa: E402


def _cand(key, category, git="ignored", **kw):
    base = {"key": key, "category": category, "path": f"p/{key}", "abs_path": f"/r/p/{key}",
            "size_bytes": 1000, "size_h": "1.0K", "age_days": 40, "git": git, "detail": ""}
    base.update(kw)
    return base


def _candidates(*cands):
    return {"run_id": "rid", "root": "/r", "generated": "now",
            "summary": {"n": len(cands), "total_bytes": sum(c["size_bytes"] for c in cands),
                        "total_h": "x", "by_category": {}},
            "candidates": list(cands), "skipped": []}


def test_actionable_filter_and_action_inference():
    cands = _candidates(
        _cand("a", "db_backup"), _cand("b", "log"), _cand("c", "pycache"),
        _cand("d", "state_orphan"))
    verdicts = {
        "a": {"recommendation": "delete", "risk": "low", "reason": "old"},
        "b": {"recommendation": "truncate", "risk": "low", "reason": "big"},
        "c": {"recommendation": "delete", "risk": "low", "reason": "cache"},
        "d": {"recommendation": "investigate", "risk": "medium", "reason": "?"},
    }
    payload, md = hkr.render(cands, verdicts, set())
    by_key = {s["key"]: s for s in payload["suggestions"]}
    assert set(by_key) == {"a", "b", "c"}                 # investigate 'd' is NOT carded
    assert by_key["a"]["action"] == "delete_file"
    assert by_key["b"]["action"] == "truncate"
    assert by_key["c"]["action"] == "delete_dir"          # pycache → dir
    assert "To review" in md and "p/d" in md


def test_tracked_path_is_downgraded_not_deleted():
    cands = _candidates(_cand("t", "large_file", git="tracked"))
    verdicts = {"t": {"recommendation": "delete", "risk": "high", "reason": "big binary"}}
    payload, md = hkr.render(cands, verdicts, set())
    assert payload["suggestions"] == []                   # never carded
    assert any("git-tracked" in n for n in payload["notes"])


def test_rejected_keys_are_dropped():
    cands = _candidates(_cand("x", "pycache"), _cand("y", "pycache"))
    verdicts = {k: {"recommendation": "delete", "risk": "low", "reason": "cache"} for k in ("x", "y")}
    payload, _ = hkr.render(cands, verdicts, rejected_keys={"x"})
    assert [s["key"] for s in payload["suggestions"]] == ["y"]


def test_missing_verdict_defaults_to_review():
    cands = _candidates(_cand("u", "untracked_large"))
    payload, md = hkr.render(cands, {}, set())
    assert payload["suggestions"] == []                   # no verdict → not actionable
    assert "no verdict" in md

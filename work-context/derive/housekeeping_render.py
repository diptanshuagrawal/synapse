#!/usr/bin/env python3
"""housekeeping_render.py — fuse scan FACTS + Claude VERDICTS into the two outputs.

Deterministic renderer (no LLM). The classification layer
(.claude/shared/housekeeping-classify.md) reads state/housekeeping_candidates.json,
decides a verdict per candidate, and writes a small verdicts file:

  {"verdicts": {"<key>": {"recommendation": "delete|truncate|worktree_remove|keep|investigate",
                          "risk": "low|medium|high", "reason": "one sentence",
                          "action": "<optional override>"}}}

This script then emits, deterministically:
  • state/housekeeping_suggestions_<run-id>.json  → the Relay payload (ACTIONABLE only)
  • derived/housekeeping-suggestions.md           → the human report (everything)

Safety re-enforced here too: a candidate that is git-TRACKED can never be carded
for deletion (downgraded to 'investigate'), and any key in the rejected ledger is
dropped. Keeps the classifier honest even if a verdict is wrong.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

ACTIONABLE = {"delete", "truncate", "worktree_remove"}
CAT_EMOJI = {"db_backup": "💾", "log": "📜", "pycache": "🐍", "cache": "♻️", "derived_stale": "📊",
             "state_orphan": "🗂️", "preview_bloat": "🖼️", "untracked_large": "📦",
             "worktree": "🌳", "large_file": "🐘"}
RISK_DOT = {"low": "🟢", "medium": "🟡", "high": "🔴"}

# recommendation → concrete apply action, when the verdict doesn't override it.
DEFAULT_ACTION = {
    "truncate": "truncate",
    "worktree_remove": "worktree_remove",
}


def human(b: int) -> str:
    for unit, scale in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if b >= scale:
            return f"{b / scale:.1f}{unit}"
    return f"{b}B"


def _infer_action(cand: dict, rec: str) -> str:
    if rec in DEFAULT_ACTION:
        return DEFAULT_ACTION[rec]
    # rec == "delete": dir-shaped categories → delete_dir, else delete_file
    if cand["category"] in ("pycache", "cache") or os.path.isdir(cand.get("abs_path", "")):
        return "delete_dir"
    return "delete_file"


def render(candidates: dict, verdicts: dict, rejected_keys: set) -> tuple[dict, str]:
    run_id = candidates.get("run_id", "")
    root = candidates.get("root", "")
    cands = candidates.get("candidates", [])

    actionable: list[dict] = []
    review: list[dict] = []
    kept: list[dict] = []
    notes: list[str] = []

    for c in cands:
        key = c["key"]
        if key in rejected_keys:
            continue
        v = verdicts.get(key, {})
        rec = (v.get("recommendation") or "investigate").lower()
        risk = (v.get("risk") or "low").lower()
        reason = v.get("reason") or "(no verdict — review manually)"

        # Hard guard: never card a git-tracked path for deletion.
        if rec in ACTIONABLE and c.get("git") == "tracked":
            notes.append(f"downgraded {c['path']}: git-tracked, not deletable")
            rec, reason = "investigate", reason + " [git-tracked — not auto-deletable]"

        row = {**c, "recommendation": rec, "risk": risk, "reason": reason}
        if rec in ACTIONABLE:
            row["action"] = v.get("action") or _infer_action(c, rec)
            actionable.append(row)
        elif rec == "keep":
            kept.append(row)
        else:
            review.append(row)

    payload = {
        "run_id": run_id,
        "generated": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "root": root,
        "suggestions": [{k: r.get(k) for k in
                         ("key", "category", "path", "abs_path", "size_bytes", "size_h",
                          "age_days", "git", "action", "recommendation", "risk", "reason")}
                        for r in actionable],
        "notes": notes,
    }
    md = _markdown(run_id, candidates, actionable, review, kept)
    return payload, md


def _group_by_cat(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(r["category"], []).append(r)
    return out


def _line(r: dict) -> str:
    dot = RISK_DOT.get(r.get("risk", "low"), "🟢")
    age = r.get("age_days")
    age_md = f", {age}d" if isinstance(age, int) and age >= 0 else ""
    return f"- {dot} `{r['path']}` — {r['size_h']}{age_md} · git:{r.get('git','?')}\n  {r['reason']}"


def _markdown(run_id, candidates, actionable, review, kept) -> str:
    s = candidates.get("summary", {})
    reclaim = human(sum(int(r.get("size_bytes") or 0) for r in actionable))
    out = [f"# Housekeeping suggestions — {run_id}",
           f"_generated {candidates.get('generated','')} · "
           f"{s.get('n', 0)} candidates · {s.get('total_h','?')} scanned · "
           f"**{len(actionable)} proposed ({reclaim} reclaimable)** · "
           f"{len(review)} to review · {len(kept)} kept_", ""]

    if candidates.get("skipped"):
        out.append("> " + " · ".join(candidates["skipped"]))
        out.append("")

    out.append(f"## ✅ Proposed for cleanup — {len(actionable)} ({reclaim})")
    out.append("_Posted to #rollup with Approve/Reject. Approve → git-safe delete/truncate._\n")
    if actionable:
        for cat, rows in sorted(_group_by_cat(actionable).items()):
            sub = human(sum(int(r.get("size_bytes") or 0) for r in rows))
            out.append(f"### {CAT_EMOJI.get(cat,'🧹')} {cat} — {len(rows)} ({sub})")
            out += [_line(r) for r in sorted(rows, key=lambda x: -(x.get("size_bytes") or 0))]
            out.append("")
    else:
        out.append("_Nothing safe to auto-propose this run._\n")

    if review:
        out.append(f"## 🔍 To review — {len(review)}")
        out.append("_Needs a human eye (could be work-in-progress or meaningful state)._\n")
        for cat, rows in sorted(_group_by_cat(review).items()):
            out.append(f"### {CAT_EMOJI.get(cat,'🧹')} {cat} — {len(rows)}")
            out += [_line(r) for r in sorted(rows, key=lambda x: -(x.get("size_bytes") or 0))]
            out.append("")

    if kept:
        out.append(f"## 📌 Kept — {len(kept)}\n")
        for r in sorted(kept, key=lambda x: -(x.get("size_bytes") or 0)):
            out.append(_line(r))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Render housekeeping suggestions from scan + verdicts.")
    ap.add_argument("--root", default=os.environ.get("CONTEXT_ROOT", os.path.expanduser("~/context")))
    ap.add_argument("--candidates", default=None)
    ap.add_argument("--verdicts", required=True, help="JSON: {'verdicts': {key: {recommendation,risk,reason}}}")
    ap.add_argument("--rejected", default=None, help="rejected ledger (default state/housekeeping_rejected.json)")
    args = ap.parse_args()

    wc = os.path.join(args.root, "work-context")
    cand_p = args.candidates or os.path.join(wc, "state/housekeeping_candidates.json")
    rej_p = args.rejected or os.path.join(wc, "state/housekeeping_rejected.json")

    candidates = json.load(open(cand_p))
    vraw = json.load(open(args.verdicts))
    verdicts = vraw.get("verdicts", vraw) if isinstance(vraw, dict) else {}
    rejected_keys = set()
    if os.path.exists(rej_p):
        try:
            rejected_keys = {e.get("key") for e in json.load(open(rej_p))}
        except Exception:
            pass

    payload, md = render(candidates, verdicts, rejected_keys)

    run_id = candidates.get("run_id", "")
    sug_p = os.path.join(wc, f"state/housekeeping_suggestions_{run_id}.json")
    md_p = os.path.join(wc, "derived/housekeeping-suggestions.md")
    os.makedirs(os.path.dirname(sug_p), exist_ok=True)
    os.makedirs(os.path.dirname(md_p), exist_ok=True)
    with open(sug_p, "w") as f:
        json.dump(payload, f, indent=2)
    with open(md_p, "w") as f:
        f.write(md)

    print(f"rendered {len(payload['suggestions'])} actionable suggestion(s)")
    print(f"  payload → {sug_p}")
    print(f"  report  → {md_p}")
    print(f"  run_id  → {run_id}")
    if payload["notes"]:
        print("  notes: " + "; ".join(payload["notes"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

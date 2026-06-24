#!/usr/bin/env python3
"""housekeeping_scan.py — emit FACTS about further housekeeping candidates.

This is the deterministic "scan" half of the housekeeping job. It NEVER deletes
anything — it walks the repo and reports cleanup *candidates* (regenerable
artifacts, backups, caches, stale derived/state files, abandoned worktrees,
oversized logs, large untracked files) as structured JSON.

The judgement — which of these to actually clean — is made by the Claude
classification layer (.claude/shared/housekeeping-classify.md), and the action
is gated behind Slack Approve/Reject (bin/relay_bot.py --post-housekeeping →
bin/housekeeping_apply.py). Same split the rest of this codebase uses: scripts
produce facts, chat produces judgement, Relay gates the write.

Output schema (state/housekeeping_candidates.json):
  {
    "generated": "2026-06-24T11:00:00+05:30",
    "root": "/abs/repo/root",
    "run_id": "20260624-1100",
    "candidates": [
      {"key","category","path","abs_path","size_bytes","size_h",
       "age_days","git","detail"} , ...
    ],
    "summary": {"n", "total_bytes", "total_h", "by_category": {cat: {n,bytes}}},
    "skipped": ["events.db is the live DB — never a candidate", ...]
  }

git status per candidate is BEST-EFFORT ("tracked"/"untracked"/"ignored"/
"unknown"); the apply step re-validates definitively before touching anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

# ── thresholds (env-overridable; CLI flags win) ──────────────────────────────
DAYS_DERIVED_STALE = int(os.environ.get("HK_DAYS_DERIVED_STALE", "30"))
DAYS_STATE_STALE   = int(os.environ.get("HK_DAYS_STATE_STALE", "30"))
LOG_BIG_BYTES      = int(os.environ.get("HK_LOG_BIG_BYTES", str(5 * 1024 * 1024)))     # 5M
UNTRACKED_BIG      = int(os.environ.get("HK_UNTRACKED_BIG", str(1 * 1024 * 1024)))     # 1M
LARGE_FILE_BYTES   = int(os.environ.get("HK_LARGE_FILE_BYTES", str(50 * 1024 * 1024))) # 50M

# Directories we never descend into when hunting large files / stale artifacts.
WALK_PRUNE = {".git", ".venv", "node_modules", "venv"}

# Cache directories surfaced as ONE dir-level candidate (not per-file), then not
# descended into — keeps a cache of N files from becoming N noisy cards.
CACHE_DIRS = {"__pycache__": "pycache", ".diff_cache": "cache", ".mypy_cache": "cache",
              ".pytest_cache": "cache", ".ruff_cache": "cache"}


def human(b: int) -> str:
    for unit, scale in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if b >= scale:
            return f"{b / scale:.1f}{unit}"
    return f"{b}B"


def _key(category: str, relpath: str) -> str:
    return hashlib.sha1(f"{category}:{relpath}".encode()).hexdigest()[:12]


def _age_days(path: str, now: float) -> int:
    try:
        return int((now - os.path.getmtime(path)) // 86400)
    except OSError:
        return -1


def _size(path: str) -> int:
    try:
        if os.path.isdir(path):
            total = 0
            for dp, _dn, fn in os.walk(path):
                for f in fn:
                    try:
                        total += os.path.getsize(os.path.join(dp, f))
                    except OSError:
                        pass
            return total
        return os.path.getsize(path)
    except OSError:
        return 0


# ── git status (best-effort) ─────────────────────────────────────────────────
def _git_index(root: str) -> dict:
    """Return {'tracked': set(relpaths), 'ignored': set, 'untracked': set}.
    Empty/partial when `root` is not a git work-tree — callers tolerate that."""
    out = {"tracked": set(), "ignored": set(), "untracked": set()}
    try:
        tracked = subprocess.run(["git", "-C", root, "ls-files"],
                                 capture_output=True, text=True, timeout=30)
        if tracked.returncode == 0:
            out["tracked"] = {l for l in tracked.stdout.splitlines() if l}
        st = subprocess.run(["git", "-C", root, "status", "--porcelain", "--ignored"],
                            capture_output=True, text=True, timeout=30)
        if st.returncode == 0:
            for line in st.stdout.splitlines():
                if len(line) < 4:
                    continue
                code, path = line[:2], line[3:].strip().strip('"')
                if code == "!!":
                    out["ignored"].add(path)
                elif code == "??":
                    out["untracked"].add(path)
    except Exception:
        pass
    return out


def _git_status_for(rel: str, gi: dict) -> str:
    if rel in gi["tracked"]:
        return "tracked"
    # ignored/untracked entries may be a parent dir (trailing slash) of `rel`
    for cat in ("ignored", "untracked"):
        for entry in gi[cat]:
            e = entry.rstrip("/")
            if rel == e or rel.startswith(e + "/"):
                return cat
    return "unknown"


# ── candidate collectors ─────────────────────────────────────────────────────
def scan(root: str) -> dict:
    root = os.path.abspath(root)
    wc = os.path.join(root, "work-context")
    if not os.path.isdir(wc):           # allow callers to point straight at a work-context
        wc = root
    now = datetime.now().timestamp()
    gi = _git_index(root)
    cands: list[dict] = []
    skipped: list[str] = []

    def add(category: str, abs_path: str, *, detail: str = "", size: int | None = None):
        try:
            rel = os.path.relpath(abs_path, root)
        except ValueError:
            rel = abs_path
        sz = size if size is not None else _size(abs_path)
        cands.append({
            "key": _key(category, rel),
            "category": category,
            "path": rel,
            "abs_path": abs_path,
            "size_bytes": sz,
            "size_h": human(sz),
            "age_days": _age_days(abs_path, now),
            "git": _git_status_for(rel, gi),
            "detail": detail,
        })

    # 1. events.db backups. The deterministic prune (housekeeping.sh step 1) ALREADY owns
    #    standard `events.db.bak*` files (deletes them past DAYS_BAK=60). To avoid duplicating
    #    that rule, we DON'T card standard backups here — we only card backups the glob can't
    #    match (non-standard names like `events.db.pre-*.bak`, which the prune NEVER deletes),
    #    and emit ONE pile-up note for the standard ones the 60d rule will take on schedule.
    index_dir = os.path.join(wc, "index")
    live_db = os.path.join(index_dir, "events.db")
    if os.path.isfile(live_db):
        skipped.append(f"index/events.db ({human(_size(live_db))}) — live DB, never a candidate")
    if os.path.isdir(index_dir):
        owned_n, owned_b = 0, 0
        for f in sorted(os.listdir(index_dir)):
            if f == "events.db" or not f.startswith("events.db") or ".bak" not in f:
                continue  # live DB, its -wal/-shm, or not a backup at all
            p = os.path.join(index_dir, f)
            if f.startswith("events.db.bak"):
                owned_n += 1                       # matches the prune's `events.db.bak*` glob → it owns this
                owned_b += _size(p)
            else:
                add("db_backup", p,                # glob-miss: the 60d prune never matches this name
                    detail="non-standard backup name — the 60d `events.db.bak*` prune never matches it")
        if owned_n:
            skipped.append(
                f"{owned_n} standard events.db.bak* backup(s) ({human(owned_b)}) — the 60d prune "
                f"owns these (lower DAYS_BAK in housekeeping.sh to reclaim sooner)")

    # 2. oversized logs (truncate candidates)
    logs_dir = os.path.join(wc, "logs")
    if os.path.isdir(logs_dir):
        for f in sorted(os.listdir(logs_dir)):
            if f.endswith(".log"):
                p = os.path.join(logs_dir, f)
                sz = _size(p)
                if sz >= LOG_BIG_BYTES:
                    add("log", p, size=sz, detail=f"log {human(sz)} — truncate to 0 (keeps the file)")

    # 3. cache dirs (__pycache__, .diff_cache, …) — one candidate per dir, regenerated on next run
    for dp, dn, _fn in os.walk(root):
        dn[:] = [d for d in dn if d not in WALK_PRUNE]
        base = os.path.basename(dp)
        if base in CACHE_DIRS:
            kind = "bytecode cache" if base == "__pycache__" else f"`{base}` cache"
            add(CACHE_DIRS[base], dp, detail=f"{kind} — regenerated automatically")
            dn[:] = []  # don't descend — its files are not separate candidates

    # 4. stale derived/ artifacts (regenerable by the pipeline)
    derived_dir = os.path.join(wc, "derived")
    if os.path.isdir(derived_dir):
        for dp, dn, fn in os.walk(derived_dir):
            dn[:] = [d for d in dn if d not in WALK_PRUNE and d not in CACHE_DIRS]
            for f in fn:
                p = os.path.join(dp, f)
                age = _age_days(p, now)
                if age >= DAYS_DERIVED_STALE:
                    add("derived_stale", p, detail=f"derived artifact untouched {age}d — regenerable")

    # 5. stale state/*.json (excluding the last_* run-once markers)
    state_dir = os.path.join(wc, "state")
    if os.path.isdir(state_dir):
        for f in sorted(os.listdir(state_dir)):
            if not f.endswith(".json"):
                continue
            if f.startswith("last_") or f.startswith("housekeeping_"):
                continue
            p = os.path.join(state_dir, f)
            age = _age_days(p, now)
            if age >= DAYS_STATE_STALE:
                add("state_orphan", p, detail=f"state file untouched {age}d — likely a stale run artifact")

    # 6. dashboard preview bloat (gitignored, regenerated by bin/dashboard.py)
    bin_dir = os.path.join(wc, "bin")
    if os.path.isdir(bin_dir):
        for f in sorted(os.listdir(bin_dir)):
            if (f.startswith("dashboard_") and f.endswith("_preview.html")) or f.endswith("_preview.json"):
                add("preview_bloat", os.path.join(bin_dir, f), detail="dashboard preview — regenerated on next render")

    # 7. large UNTRACKED files (surface so the owner notices accidental drops)
    for entry in sorted(gi["untracked"]):
        ent = entry.rstrip("/")
        ap = os.path.join(root, ent)
        if not os.path.exists(ap):
            continue
        sz = _size(ap)
        if sz >= UNTRACKED_BIG:
            add("untracked_large", ap, size=sz, detail=f"untracked {human(sz)} — not in git; review before removing")

    # 8. git worktrees other than the main checkout (abandoned-branch candidates)
    try:
        wt = subprocess.run(["git", "-C", root, "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, timeout=30)
        if wt.returncode == 0:
            cur, first = {}, True
            blocks = []
            for line in wt.stdout.splitlines() + [""]:
                if not line.strip():
                    if cur:
                        blocks.append(cur); cur = {}
                    continue
                k, _, v = line.partition(" ")
                cur[k] = v
            for b in blocks:
                wpath = b.get("worktree", "")
                if not wpath or os.path.abspath(wpath) == root:
                    continue  # the main checkout
                br = b.get("branch", "(detached)").replace("refs/heads/", "")
                add("worktree", wpath, size=_size(wpath),
                    detail=f"git worktree on '{br}' — remove if the branch is merged/abandoned")
    except Exception:
        pass

    # 9. large files anywhere (excluding the dirs we prune + the live DB)
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in WALK_PRUNE and d not in CACHE_DIRS]
        for f in fn:
            p = os.path.join(dp, f)
            if p == live_db:
                continue
            if f.startswith("events.db.bak"):
                continue  # standard backups are owned by the 60d prune (see step 1) — never re-card
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if sz >= LARGE_FILE_BYTES:
                # de-dup: a db_backup / untracked_large may already cover it
                rel = os.path.relpath(p, root)
                if any(c["path"] == rel for c in cands):
                    continue
                add("large_file", p, size=sz, detail=f"large file {human(sz)} — review")

    # ── summary ──────────────────────────────────────────────────────────────
    by_cat: dict = {}
    for c in cands:
        b = by_cat.setdefault(c["category"], {"n": 0, "bytes": 0})
        b["n"] += 1
        b["bytes"] += c["size_bytes"]
    total = sum(c["size_bytes"] for c in cands)
    return {
        "generated": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "root": root,
        "run_id": datetime.now().strftime("%Y%m%d-%H%M"),
        "candidates": cands,
        "summary": {"n": len(cands), "total_bytes": total, "total_h": human(total),
                    "by_category": by_cat},
        "skipped": skipped,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Emit housekeeping cleanup candidates as JSON (no deletion).")
    ap.add_argument("--root", default=os.environ.get("CONTEXT_ROOT", os.path.expanduser("~/context")),
                    help="repo root (default $CONTEXT_ROOT or ~/context)")
    ap.add_argument("--out", default=None,
                    help="write JSON here (default: <root>/work-context/state/housekeeping_candidates.json)")
    ap.add_argument("--json", action="store_true", help="also print the JSON to stdout")
    args = ap.parse_args()

    data = scan(args.root)
    out = args.out or os.path.join(args.root, "work-context/state/housekeeping_candidates.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(data, f, indent=2)

    s = data["summary"]
    cats = ", ".join(f"{k}:{v['n']}" for k, v in sorted(s["by_category"].items())) or "none"
    print(f"housekeeping scan: {s['n']} candidate(s), {s['total_h']} reclaimable → {out}")
    print(f"  by category: {cats}")
    if args.json:
        print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

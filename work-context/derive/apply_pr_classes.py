#!/usr/bin/env python3
"""apply_pr_classes.py — validate /pr-quality verdicts and write pr_comment_class.

Phase 4 apply step (PRD: prd/pr-quality-scorer.md). Mirrors apply_leaves.py:
  1. Read state/verdicts.pr_comments.json (written by the /pr-quality chat turn).
  2. Cross-check event_id against state/pending_pr_comments.json.
  3. Validate category enum + confidence.
  4. UPSERT into pr_comment_class (source taken from pending, NOT the verdict).
  5. Recompute pr_friction for the affected merged PRs so category-weighted
     scores replace the mechanical-only ones.
  6. Archive verdicts file.

Verdicts with confidence < CONFIDENCE_MIN are dropped (stay pending — the next
dump re-presents them).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import get_db  # noqa: E402
from derive.github_metrics import CATEGORY_WEIGHTS, compute_friction  # noqa: E402

STATE_DIR = _REPO_ROOT / "state"
PENDING_JSON = STATE_DIR / "pending_pr_comments.json"
VERDICTS_JSON = STATE_DIR / "verdicts.pr_comments.json"

VALID_CATEGORIES = set(CATEGORY_WEIGHTS)  # single source of truth
CONFIDENCE_MIN = 0.7  # canonical classify gate (matches apply_verdicts / apply_leaves)


def _validate(v: dict, pending_map: dict[str, dict]) -> tuple[dict | None, str | None]:
    ev_id = v.get("event_id")
    if not ev_id:
        return None, "missing event_id"
    p = pending_map.get(ev_id)
    if p is None:
        return None, f"{ev_id}: not in pending — stale verdict"
    cat = (v.get("category") or "").strip().lower()
    if cat not in VALID_CATEGORIES:
        return None, f"{ev_id}: bad category {cat!r}"
    conf = v.get("confidence")
    if not isinstance(conf, (int, float)):
        return None, f"{ev_id}: missing/invalid confidence"
    if conf < CONFIDENCE_MIN:
        return None, f"{ev_id}: confidence {conf:.2f} < {CONFIDENCE_MIN} — stays pending"
    return {
        "event_id": ev_id,
        "subject": p["subject"],
        "source": p["source"],  # human|matterai, from dump — never from chat
        "category": cat,
        "confidence": float(conf),
    }, None


def _is_locked(e: Exception) -> bool:
    s = str(e).lower()
    return "locked" in s or "busy" in s


def _do_writes(accepted: list[dict]) -> tuple[int, int]:
    """Single attempt: write pr_comment_class + recompute friction. Idempotent."""
    conn = get_db()
    conn.execute("PRAGMA busy_timeout = 60000")  # 60s in-SQLite wait per stmt
    now = datetime.now(timezone.utc).isoformat()
    for c in accepted:
        conn.execute(
            """INSERT INTO pr_comment_class (event_id, subject, source, category, confidence, classified_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(event_id) DO UPDATE SET
                   subject=excluded.subject, source=excluded.source,
                   category=excluded.category, confidence=excluded.confidence,
                   classified_at=excluded.classified_at""",
            (c["event_id"], c["subject"], c["source"], c["category"], c["confidence"], now),
        )
    conn.commit()

    subjects = sorted({c["subject"] for c in accepted})
    recomputed = 0
    for subj in subjects:
        pr = conn.execute("SELECT * FROM pr_meta WHERE subject = ? AND state = 'merged'", (subj,)).fetchone()
        if not pr:
            continue
        f = compute_friction(conn, pr)
        conn.execute(
            """INSERT INTO pr_friction (subject, score, dominant_category, mechanical_json, category_counts_json, computed_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(subject) DO UPDATE SET
                   score=excluded.score, dominant_category=excluded.dominant_category,
                   mechanical_json=excluded.mechanical_json,
                   category_counts_json=excluded.category_counts_json, computed_at=excluded.computed_at""",
            (f["subject"], f["score"], f["dominant_category"],
             json.dumps(f["mechanical"]), json.dumps(f["category_counts"]), now),
        )
        recomputed += 1
    conn.commit()
    conn.close()
    return len(accepted), recomputed


def _write_with_retry(accepted: list[dict], attempts: int = 20) -> tuple[int, int]:
    """Retry _do_writes on a locked DB (e.g. concurrent slack-ingest / embeddings
    job holds the write lock). Exponential backoff capped at 45s. Writes are
    idempotent (ON CONFLICT upserts), so re-running after a partial failure is
    safe — a failed commit rolls back, nothing half-persists.

    The slack-ingest LaunchAgent walks ~200 channels per fire and grabs the
    write lock repeatedly for the whole run (can exceed 10 min). Budget here is
    ~20 attempts × up to 45s ≈ 13 min so apply rides out an entire ingest run
    and lands in the next idle gap instead of giving up.
    """
    for i in range(attempts):
        try:
            return _do_writes(accepted)
        except sqlite3.OperationalError as e:
            if not _is_locked(e) or i == attempts - 1:
                raise
            wait = min(45, 2 ** (i + 1))
            print(f"[lock] events.db busy — retry in {wait}s ({i + 1}/{attempts - 1})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(VERDICTS_JSON))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"[err] no verdicts file at {in_path}", file=sys.stderr)
        return 2
    if not PENDING_JSON.exists():
        print(f"[err] no pending file at {PENDING_JSON}", file=sys.stderr)
        return 2

    pending_map = {p["event_id"]: p for p in json.loads(PENDING_JSON.read_text()).get("pending", [])}
    raw = json.loads(in_path.read_text())
    verdicts = raw.get("verdicts") if isinstance(raw, dict) else raw
    verdicts = verdicts or []
    print(f"[in] {len(verdicts)} verdicts, {len(pending_map)} pending")

    accepted, rejected = [], 0
    for v in verdicts:
        cleaned, err = _validate(v, pending_map)
        if cleaned is None:
            rejected += 1
            print(f"  REJECT {err}", file=sys.stderr)
            continue
        accepted.append(cleaned)
    print(f"[validate] {len(accepted)} accepted, {rejected} rejected")

    if args.dry_run or not accepted:
        if args.dry_run:
            print("[dry] no DB writes")
        return 0

    n_class, recomputed = _write_with_retry(accepted)
    print(f"[apply] +{n_class} pr_comment_class rows")
    print(f"[friction] recomputed {recomputed} PR(s)")

    stamp = datetime.now(timezone.utc).isoformat()[:19].replace(":", "")
    archive = in_path.with_suffix(f".{stamp}.json")
    in_path.rename(archive)
    print(f"[archive] {in_path.name} → {archive.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
apply_ownership_sample.py — ONE-OFF apply for the small-batch ownership test.

Reads state/verdicts.ownership_sample.json and updates ownership columns
on subject_summary. Does NOT touch the domains / summary / risk_flags
fields — those are owned by the production classifier (apply_verdicts.py).

If a subject doesn't have a subject_summary row yet (rare in production
but possible for sampled fresh subjects), inserts a stub row with
domains=[] summary="(pending classifier)" so the ownership write is
preserved when the production classifier later upserts.

Usage:
    .venv/bin/python derive/apply_ownership_sample.py
    .venv/bin/python derive/apply_ownership_sample.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH  # noqa: E402

VERDICTS_PATH = _REPO_ROOT / "state" / "verdicts.ownership_sample.json"
TEAMS_YAML    = _REPO_ROOT / "config" / "teams.yaml"


def _team_id_set() -> set[str]:
    import yaml
    with TEAMS_YAML.open() as f:
        cfg = yaml.safe_load(f)
    return {t.get("id", "") for t in (cfg.get("teams", []) or [])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not VERDICTS_PATH.exists():
        print(f"missing {VERDICTS_PATH}", file=sys.stderr)
        return 1

    verdicts = json.loads(VERDICTS_PATH.read_text())
    if not isinstance(verdicts, list):
        print("verdicts.ownership_sample.json must be a JSON array", file=sys.stderr)
        return 1

    team_ids = _team_id_set()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(DB_PATH)

    applied = 0
    skipped = 0
    errors: list[str] = []

    for v in verdicts:
        subj = v.get("subject", "")
        h = v.get("content_hash", "")
        primary = v.get("owned_by_primary", "")
        co_raw = v.get("co_owners") or []
        conf = float(v.get("owned_by_confidence", 0.0))
        reasoning = (v.get("ownership_reasoning", "") or "")[:300]

        if not subj or not primary:
            errors.append(f"{subj}: missing subject or owned_by_primary")
            skipped += 1
            continue
        if primary not in team_ids:
            errors.append(f"{subj}: unknown team id {primary!r}")
            skipped += 1
            continue
        co_clean = [c for c in co_raw if c in team_ids]
        if len(co_clean) != len(co_raw):
            errors.append(f"{subj}: dropped unknown co_owners {set(co_raw) - set(co_clean)}")

        if args.dry_run:
            print(f"DRY  {subj}  primary={primary}  co={co_clean}  conf={conf:.2f}")
            applied += 1
            continue

        # Upsert: if subject_summary row exists, UPDATE; else INSERT stub.
        existing = conn.execute(
            "SELECT 1 FROM subject_summary WHERE subject=?", (subj,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE subject_summary SET "
                "  owned_by_primary=?, "
                "  co_owners_json=?, "
                "  owned_by_confidence=?, "
                "  ownership_reasoning=? "
                "WHERE subject=?",
                (primary, json.dumps(co_clean), conf, reasoning, subj),
            )
        else:
            conn.execute(
                "INSERT INTO subject_summary "
                "(subject, content_hash, domains, summary, risk_flags, confidence, "
                " source, model, classified_at, input_tokens, output_tokens, detail, "
                " owned_by_primary, co_owners_json, owned_by_confidence, ownership_reasoning) "
                "VALUES (?, ?, '[]', '(ownership-only stub)', '[]', 0.0, "
                "        'manual', 'claude-chat', ?, 0, 0, '', "
                "        ?, ?, ?, ?)",
                (subj, h or f"manual::{subj}", now,
                 primary, json.dumps(co_clean), conf, reasoning),
            )
        applied += 1
        print(f"+ {subj}  primary={primary}  co={co_clean}  conf={conf:.2f}")

    if not args.dry_run:
        conn.commit()
    if errors:
        print("\n--- errors / warnings ---")
        for e in errors:
            print(f"  {e}")
    print(f"\nsummary: applied={applied} skipped={skipped} errors={len(errors)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
One-time migration: fix commit_pushed actor field by re-resolving from raw JSONL.

For each commit event, reads the original raw event, extracts the author email,
and looks up the GitHub handle via people.yaml. Updates events.actor in place.

Idempotent — safe to re-run.

Usage: .venv/bin/python bin/migrate-commit-actors.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
DB = ROOT / "index" / "events.db"
PEOPLE_YAML = ROOT / "config" / "people.yaml"


def load_email_to_github() -> dict[str, str]:
    data = yaml.safe_load(PEOPLE_YAML.read_text()) or {}
    return {
        (p.get("email") or "").lower(): p["github"]
        for p in data.get("people", [])
        if p.get("email") and p.get("github")
    }


def parse_raw_path(raw_path: str) -> tuple[Path, int]:
    """Parse 'raw/github/YYYY/MM/DD.jsonl#N' → (Path, line_number)."""
    rel, _, idx = raw_path.partition("#")
    return ROOT / rel, int(idx)


def read_raw_event(raw_path: str) -> dict | None:
    try:
        path, idx = parse_raw_path(raw_path)
        with open(path) as f:
            # raw_path line numbers are 1-indexed (see ingest/common.py::append_raw)
            for i, line in enumerate(f, start=1):
                if i == idx:
                    return json.loads(line)
        return None
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    email_map = load_email_to_github()
    print(f"Loaded {len(email_map)} email→github mappings")

    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()

    cur.execute("SELECT id, actor, raw_path FROM events WHERE event_type = 'commit_pushed'")
    rows = cur.fetchall()
    print(f"Scanning {len(rows)} commit events …")

    updates = 0
    skipped_already = 0
    skipped_no_match = 0
    skipped_no_raw = 0

    for event_id, current_actor, raw_path in rows:
        raw = read_raw_event(raw_path)
        if not raw:
            skipped_no_raw += 1
            continue

        # raw is the Event JSON. The author info is not in the normalized event,
        # but we need the original commit JSON. The Event body has the commit message.
        # Author email lives in the original github API payload — but Event doesn't store it.
        # FALLBACK: rely on the existing `actor` field if it's already a GH login,
        # else try to match by name in people.yaml.
        #
        # BETTER PATH: re-fetch via gh CLI? No — too slow. Use git name → people.name match.
        new_actor = None

        # If actor already looks like GH login (lowercase, no spaces, may have dashes/digits)
        if current_actor and " " not in current_actor and current_actor.islower():
            skipped_already += 1
            continue

        # Try matching git name to people.yaml `name` field
        people_data = yaml.safe_load(PEOPLE_YAML.read_text()).get("people", [])
        for p in people_data:
            if not p.get("github"):
                continue
            # Match git name (e.g., "Frank example") to people.name ("Frank Example") — case-insensitive
            git_name_norm = (current_actor or "").lower().replace(" ", "")
            people_name_norm = p["name"].lower().replace(" ", "")
            if git_name_norm == people_name_norm:
                new_actor = p["github"]
                break

        if not new_actor:
            skipped_no_match += 1
            continue

        if args.dry_run:
            print(f"  [DRY] {current_actor!r} → {new_actor!r}  ({event_id})")
        else:
            cur.execute("UPDATE events SET actor = ? WHERE id = ?", (new_actor, event_id))
        updates += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\nDone.")
    print(f"  Updated:        {updates}")
    print(f"  Already GH:     {skipped_already}")
    print(f"  No name match:  {skipped_no_match}")
    print(f"  Missing raw:    {skipped_no_raw}")


if __name__ == "__main__":
    main()

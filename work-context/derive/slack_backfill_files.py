#!/usr/bin/env python3
"""slack_backfill_files.py — one-shot retro-fill of the `files_json` column
for slack rows whose body carries a `[files: …]` suffix but column is NULL.

Why: `files_json` column was added after ingest path was already populating
`[files: …]` body suffix. Existing rows have NULL until re-upserted (which
only happens on body-edit). This script targets them directly.

Strategy:
  1. SELECT id, ts from rows where body LIKE '%[files:%' AND files_json IS NULL.
  2. For each: derive epoch from id, call conversations.history with a narrow
     window (oldest=ts-0.001, latest=ts+0.001, inclusive=true) to fetch the
     exact msg + its `files[]`.
  3. Compute files_json via slack_api_client._files_to_struct.
  4. UPDATE events SET files_json=? WHERE id=?.

Idempotent. Re-runs only touch rows still NULL.

Rate-limit aware: SlackClient already throttles to ~45/min (Tier-3 safe).

Usage:
    python -m derive.slack_backfill_files               # all channels
    python -m derive.slack_backfill_files --channel C…  # one channel
    python -m derive.slack_backfill_files --dry-run
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH  # noqa: E402
from ingest.slack_api_client import SlackClient, _files_to_struct  # noqa: E402


def _parse_id(eid: str) -> tuple[str | None, str | None]:
    """Returns (parent_ts, msg_ts).

    Top-level id   `slack:<channel>:<ts>`           → (None, ts)
    Thread reply   `slack:<channel>:<parent>:<ts>`  → (parent, ts)
    """
    parts = eid.split(":")
    if len(parts) == 3:
        return None, parts[2]
    if len(parts) == 4:
        return parts[2], parts[3]
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", help="single channel id (default: all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)

    where = "source='slack' AND body LIKE '%[files:%' AND files_json IS NULL"
    params: tuple = ()
    if args.channel:
        where += " AND channel_id=?"
        params = (args.channel,)
    rows = conn.execute(
        f"SELECT id, channel_id FROM events WHERE {where}", params,
    ).fetchall()
    print(f"[scan] {len(rows)} candidate rows", flush=True)

    if not rows:
        return 0

    client = SlackClient()

    updated = no_match = no_files = errors = 0
    # Thread-replies cache: avoid re-fetching the same thread for multiple rows.
    thread_cache: dict[tuple[str, str], list] = {}

    with conn:
        for i, (eid, channel_id) in enumerate(rows, 1):
            parent_ts, slack_ts = _parse_id(eid)
            if not slack_ts:
                errors += 1
                continue

            match = None
            try:
                if parent_ts is None:
                    # Top-level: narrow-window history.
                    try:
                        f_ts = float(slack_ts)
                    except ValueError:
                        errors += 1
                        continue
                    oldest = f"{f_ts - 0.001:.6f}"
                    latest = f"{f_ts + 0.001:.6f}"
                    resp = client.history(channel_id, oldest=oldest, latest=latest, limit=5)
                    match = next(
                        (m for m in resp.get("messages", []) if m.get("ts") == slack_ts), None,
                    )
                else:
                    # Thread reply: fetch full thread (cached per parent).
                    key = (channel_id, parent_ts)
                    if key not in thread_cache:
                        thread_cache[key] = list(client.iter_replies(channel_id, parent_ts, limit=200))
                    match = next(
                        (m for m in thread_cache[key] if m.get("ts") == slack_ts), None,
                    )
            except Exception as e:
                print(f"  [err] {eid}: {e}", file=sys.stderr)
                errors += 1
                continue

            if not match:
                no_match += 1
                continue
            files = match.get("files") or []
            if not files:
                no_files += 1
                continue
            fjson = _files_to_struct(files)
            if not fjson:
                no_files += 1
                continue
            if not args.dry_run:
                conn.execute("UPDATE events SET files_json=? WHERE id=?", (fjson, eid))
            updated += 1
            if i % 20 == 0:
                print(f"  progress: {i}/{len(rows)}  updated={updated}", flush=True)

    print(json.dumps({
        "scanned": len(rows),
        "updated": updated,
        "no_match_in_api": no_match,
        "no_files_in_api": no_files,
        "errors": errors,
        "dry_run": args.dry_run,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

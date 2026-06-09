#!/usr/bin/env python3
"""
slack_expand_mentions.py — one-shot retro-expansion of `<@U…>` and
`<!subteam^S…>` tokens in body text for legacy slack rows.

Why: rows ingested before _expand_mentions existed (or via the old MCP path)
have raw `<@U…>` without the `|Name` suffix. event_refs.people is still
correctly populated (enrich_refs reads U-ids regardless of suffix), but the
body text reads poorly downstream (narrative quotes, rollup subjects).

This script:
  1. Hydrates users_cache + subteams_cache from Slack API (once).
  2. Walks `events` where source='slack' AND body contains a candidate token.
  3. Applies the same `_expand_mentions` / `_expand_subteams` transforms used
     at ingest time.
  4. UPDATEs body in-place when the result differs. edited_ts is NOT set —
     this is retro-resolution, not a Slack-side edit.

Idempotent: rerunning on already-expanded bodies is a no-op.

Usage:
    python -m derive.slack_expand_mentions                    # all channels
    python -m derive.slack_expand_mentions --channel C…       # one channel
    python -m derive.slack_expand_mentions --dry-run          # preview, no write
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH  # noqa: E402
from ingest.slack_api_client import (  # noqa: E402
    SlackClient,
    _expand_mentions,
    _expand_subteams,
    make_name_resolver,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", help="single channel id (default: all slack rows)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    client = SlackClient()
    print("[users] hydrating users.list…", flush=True)
    users_cache = client.build_users_cache()
    print(f"  cached {len(users_cache)} users", flush=True)
    name_resolver = make_name_resolver(client, users_cache)
    subteams_cache = client.build_subteams_cache()
    print(f"[subteams] cached {len(subteams_cache)}", flush=True)

    where = "source='slack' AND (body LIKE '%<@U%' OR body LIKE '%<!subteam%')"
    params: tuple = ()
    if args.channel:
        where += " AND channel_id=?"
        params = (args.channel,)

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(f"SELECT id, body FROM events WHERE {where}", params).fetchall()
    print(f"[scan] {len(rows)} candidate rows", flush=True)

    changed = 0
    unchanged = 0
    with conn:
        for eid, body in rows:
            if not body:
                continue
            new_body = _expand_subteams(
                _expand_mentions(body, users_cache, name_resolver),
                subteams_cache,
            )
            if new_body == body:
                unchanged += 1
                continue
            changed += 1
            if not args.dry_run:
                conn.execute(
                    "UPDATE events SET body=? WHERE id=?",
                    (new_body, eid),
                )
    print(f"[done] changed={changed}  unchanged={unchanged}  dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

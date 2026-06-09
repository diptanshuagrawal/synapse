"""
slack_reparse_empty.py — re-derive `events.body` for slack rows whose body is
empty due to the legacy parser ignoring `attachments[]` / `blocks[]`.

Background
----------
`ingest.slack_api_client.api_message_to_parsed` originally read body from
`msg["text"]` only. Bot integrations (Opsgenie, Grafana, Slack apps) leave
that field blank and ship semantic content via `attachments[].{text,title,
fallback,fields}` and `blocks[].text.text`. Those messages landed in
`events` with empty `body`, then embedded as empty strings, then clustered
as near-duplicates.

The parser is now patched (`_flatten_attachments_blocks`). This script
backfills the existing empty rows by re-fetching each parent via the Slack
Web API and re-parsing with the patched function.

What it touches
---------------
- READ:  `events` rows where source='slack' AND (body IS NULL OR trim(body)='')
- WRITE: `events.title`, `events.body` for those rows
- DOES NOT touch: refs (we keep ref extraction as a follow-up; URL-rewrite
  is mostly the same shape for opsgenie alerts so refs were already nil)
- DOES NOT touch: embedding table — caller re-embeds afterwards with
  `derive/embed_subjects.py --force`.

CLI
---
    .venv/bin/python derive/slack_reparse_empty.py --dry-run
    .venv/bin/python derive/slack_reparse_empty.py
    .venv/bin/python derive/slack_reparse_empty.py --subjects-file /tmp/empty.txt
    .venv/bin/python derive/slack_reparse_empty.py --limit 50
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db  # noqa: E402
from ingest.slack_api_client import (  # noqa: E402
    SlackClient,
    api_message_to_parsed,
)
from derive.slack_upsert import _title_from_body  # noqa: E402
from derive.slack_backfill_files import _parse_id as _parse_slack_event_id  # noqa: E402


def _find_empty(conn, source_subjects: list[str] | None, limit: int | None):
    """Return [(id, subject, channel_id, ts, event_type), ...] for slack rows
    with empty body. `channel_id`/`ts` parsed from the event id."""
    sql = """
        SELECT id, subject, event_type
        FROM events
        WHERE source = 'slack' AND (body IS NULL OR trim(body) = '')
    """
    rows = conn.execute(sql).fetchall()
    if source_subjects is not None:
        wanted = set(source_subjects)
        rows = [r for r in rows if r[1] in wanted]
    if limit is not None:
        rows = rows[:limit]
    out = []
    for eid, subj, et in rows:
        # event id shape:
        #   slack:<ch>:<ts>                  (top-level)
        #   slack:<ch>:<parent_ts>:<ts>      (thread reply)
        parts = eid.split(":")
        if len(parts) < 3:
            continue
        ch = parts[1]
        ts = parts[-1]
        out.append((eid, subj, ch, ts, et))
    return out


def reparse(
    subjects: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    conn = get_db()
    targets = _find_empty(conn, subjects, limit)
    if not targets:
        return {"empty_rows": 0, "fetched": 0, "updated": 0, "still_empty": 0, "errors": []}

    client = SlackClient()
    fetched = 0
    updated = 0
    still_empty = 0
    errors: list[str] = []
    # Group by channel for friendlier logging.
    by_ch: dict[str, list[tuple]] = {}
    for row in targets:
        by_ch.setdefault(row[2], []).append(row)

    for ch, rows in by_ch.items():
        print(f"\nchannel {ch}: {len(rows)} empty rows")
        for eid, subj, _, ts, et in rows:
            try:
                # `replies` returns the parent (msgs[0]) + any replies. For
                # both top-level subjects AND thread replies we need the
                # specific message identified by `ts`. Use replies(parent_ts)
                # which returns the whole thread; pick by ts.
                # Parse parent_ts from the *event id* (slack:<ch>:<parent>:<ts>
                # for replies, slack:<ch>:<ts> for top-level). Using the event
                # id instead of the subject avoids ambiguity when a subject
                # string ever drifts from its expected 3-segment form, and
                # keeps a single canonical helper (`_parse_id` in
                # `slack_backfill_files`) as the source of truth.
                if et == "thread_reply":
                    parent_ts, _msg_ts = _parse_slack_event_id(eid)
                    if not parent_ts:
                        errors.append(f"{eid}: malformed reply id (no parent_ts)")
                        continue
                    r = client.replies(channel_id=ch, ts=parent_ts)
                else:
                    r = client.replies(channel_id=ch, ts=ts)
                msgs = r.get("messages") or []
                fetched += 1
                target = next((m for m in msgs if m.get("ts") == ts), None)
                if target is None:
                    errors.append(f"{eid}: ts not in response")
                    continue
                parsed = api_message_to_parsed(target)
                new_body = parsed.body.strip()
                if not new_body:
                    still_empty += 1
                    print(f"  [still empty] {eid}  bot={parsed.actor_name}")
                    continue
                new_title = _title_from_body(new_body)
                preview = new_body[:80].replace("\n", " ")
                print(f"  [{len(new_body):5d}b] {eid}  {preview}")
                if not dry_run:
                    conn.execute(
                        "UPDATE events SET title = ?, body = ? WHERE id = ?",
                        (new_title, new_body, eid),
                    )
                updated += 1
            except Exception as e:
                errors.append(f"{eid}: {type(e).__name__}: {e}")
                print(f"  [ERROR] {eid}: {e}")
            time.sleep(0.05)  # gentle pacing

    if not dry_run:
        conn.commit()
    return {
        "empty_rows": len(targets),
        "fetched": fetched,
        "updated": updated,
        "still_empty": still_empty,
        "errors": errors,
        "dry_run": dry_run,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--subjects-file", help="Restrict to subjects listed in this file (one per line)")
    src.add_argument("--all", action="store_true", help="Process every empty-body slack row (default)")
    ap.add_argument("--limit", type=int, default=None, help="Cap rows processed (debug)")
    ap.add_argument("--dry-run", action="store_true", help="Fetch + parse but do not UPDATE")
    args = ap.parse_args()

    subjects = None
    if args.subjects_file:
        subjects = [ln.strip() for ln in Path(args.subjects_file).read_text().splitlines() if ln.strip()]
        print(f"restricting to {len(subjects)} subject(s) from {args.subjects_file}")

    stats = reparse(subjects=subjects, limit=args.limit, dry_run=args.dry_run)
    print()
    print(json.dumps(stats, indent=2, default=str))


if __name__ == "__main__":
    main()

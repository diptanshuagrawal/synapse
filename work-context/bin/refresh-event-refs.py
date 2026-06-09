#!/usr/bin/env python3
"""
refresh-event-refs.py — backfill event_refs on existing events.db rows.

Use cases:
  1. Person added late to config/people.yaml — historical events that
     mentioned them in text have raw actor in events.actor but no
     person ref. Re-running enrich_refs re-resolves canonicals.
  2. enrich_refs evolves (new regex / ref_type, e.g. pull_request +
     slack_thread added in Phase C1) — backfill old rows so cross-source
     story graph queries cover historical data.
  3. people.yaml updated with new alias (e.g. slack_id added for a
     teammate already in github) — re-resolve mentions.

Walks events table, reconstructs Event dataclass per row, calls
enrich_refs, writes results via INSERT OR IGNORE into event_refs.
NOT a DELETE-then-rewrite — only ADDS missing refs. Safe to re-run.

Usage:
    # Re-resolve every row (full walk).
    .venv/bin/python bin/refresh-event-refs.py --all

    # Restrict to one source.
    .venv/bin/python bin/refresh-event-refs.py --source jira

    # Restrict to a date window (ISO ts).
    .venv/bin/python bin/refresh-event-refs.py --since 2026-04-01T00:00:00Z

    # Only rows with zero existing person refs (fastest; targets the
    # canonical "person added late" case).
    .venv/bin/python bin/refresh-event-refs.py --missing-person

    # Combine filters.
    .venv/bin/python bin/refresh-event-refs.py --source slack --since 2026-05-13T00:00:00Z

    # Dry run — count refs that WOULD be added, no DB writes.
    .venv/bin/python bin/refresh-event-refs.py --all --dry-run

Output: structured summary at end.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db, enrich_refs, Event  # noqa: E402

# Per-source actor_field — must match what each ingest script passes.
ACTOR_FIELD_BY_SOURCE = {
    "github":     "github",
    "jira":       "email",
    "confluence": "jira_id",
    "slack":      "slack_id",
}


def _build_slack_users_cache() -> dict[str, str]:
    """Same as derive/slack_ingest_runner.py — keep consistent."""
    from ingest.common import _load_people
    cache: dict[str, str] = {}
    for p in _load_people():
        slack_id = p.get("slack_id")
        canonical = p.get("canonical")
        if slack_id and canonical:
            cache[slack_id] = canonical
    return cache


def _reconstruct_event(row: sqlite3.Row) -> Event:
    """Build an Event from a DB row for enrich_refs purposes.

    We only need fields enrich_refs reads (actor, title, body, refs).
    Other Event fields filled with row values for completeness.
    """
    return Event(
        id=row["id"],
        source=row["source"],
        event_type=row["event_type"],
        ts=row["ts"],
        actor=row["actor"],
        subject=row["subject"],
        title=row["title"],
        body=row["body"],
        url=row["url"],
        raw_path=row["raw_path"] or "",
    )


def _refresh_one(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    slack_users_cache: dict[str, str],
    dry_run: bool,
) -> tuple[int, Counter]:
    """Re-run enrich_refs for one row; write missing refs.

    Returns (refs_added_count, refs_added_by_type).
    """
    event = _reconstruct_event(row)
    actor_field = ACTOR_FIELD_BY_SOURCE.get(row["source"], "github")
    enrich_refs(
        event,
        actor_field=actor_field,
        slack_users_cache=slack_users_cache if row["source"] == "slack" else None,
    )

    # Read existing refs for this event.
    existing = {
        (rt, rv) for rt, rv in conn.execute(
            "SELECT ref_type, ref_value FROM event_refs WHERE event_id = ?",
            (row["id"],),
        )
    }

    # Build candidate refs.
    candidates: list[tuple[str, str]] = []
    candidates.extend(("person",       v) for v in event.refs.people)
    candidates.extend(("project",      v) for v in event.refs.projects)
    candidates.extend(("ticket",       v) for v in event.refs.tickets)
    candidates.extend(("page",         v) for v in event.refs.pages)
    candidates.extend(("pull_request", v) for v in event.refs.pull_requests)
    candidates.extend(("slack_thread", v) for v in event.refs.slack_threads)

    to_add = [(rt, rv) for rt, rv in candidates if (rt, rv) not in existing]
    if not to_add:
        return 0, Counter()

    by_type = Counter(rt for rt, _ in to_add)

    if not dry_run:
        conn.executemany(
            "INSERT OR IGNORE INTO event_refs (event_id, ref_type, ref_value) VALUES (?, ?, ?)",
            [(row["id"], rt, rv) for rt, rv in to_add],
        )

    return len(to_add), by_type


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true",
                        help="Walk every event row (default if no other filter).")
    parser.add_argument("--source", choices=list(ACTOR_FIELD_BY_SOURCE),
                        help="Restrict to one source.")
    parser.add_argument("--since", help="ISO ts; only rows with ts >= since.")
    parser.add_argument("--missing-person", action="store_true",
                        help="Only rows with zero existing person refs.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute additions, do not write.")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Commit every N rows (default 500).")
    args = parser.parse_args()

    conn = get_db()
    conn.row_factory = sqlite3.Row

    # Build SQL filter.
    where = ["1=1"]
    params: list = []
    if args.source:
        where.append("source = ?")
        params.append(args.source)
    if args.since:
        where.append("ts >= ?")
        params.append(args.since)
    if args.missing_person:
        where.append("""NOT EXISTS (
            SELECT 1 FROM event_refs r
             WHERE r.event_id = events.id AND r.ref_type = 'person'
        )""")

    sql = f"SELECT * FROM events WHERE {' AND '.join(where)}"

    slack_users_cache = _build_slack_users_cache()

    # Walk + write in batches.
    counts = {"rows_scanned": 0, "rows_with_new_refs": 0, "total_refs_added": 0}
    by_type_total: Counter = Counter()
    by_source_added: Counter = Counter()
    cur = conn.execute(sql, params)
    pending = 0

    for row in cur:
        counts["rows_scanned"] += 1
        added, by_type = _refresh_one(conn, row, slack_users_cache, args.dry_run)
        if added > 0:
            counts["rows_with_new_refs"] += 1
            counts["total_refs_added"] += added
            by_type_total.update(by_type)
            by_source_added[row["source"]] += added

        pending += 1
        if pending >= args.batch_size and not args.dry_run:
            conn.commit()
            pending = 0

    if not args.dry_run:
        conn.commit()

    summary = {
        **counts,
        "by_ref_type": dict(by_type_total),
        "by_source":   dict(by_source_added),
        "dry_run":     args.dry_run,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

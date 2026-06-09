#!/usr/bin/env python3
"""
build_thread_summary.py — materialise per-Slack-thread summary rows.

Reads events + event_refs for source='slack', groups by subject (= one thread),
writes one row per thread into thread_summary table.

Idempotent — uses INSERT OR REPLACE. Safe to run repeatedly. Called from the
/slack-ingest skill after each ingest fire.

Usage:
    .venv/bin/python derive/build_thread_summary.py             # incremental — only threads with new activity since last build
    .venv/bin/python derive/build_thread_summary.py --rebuild-all # full rebuild (use after people.yaml or channel-config change)
    .venv/bin/python derive/build_thread_summary.py --channel C0… # one channel only

Late-add safety:
    Snapshots `started_by_canonical` and `participants_json` at build time.
    If a person is added to people.yaml LATER, re-run with --rebuild-all
    to refresh those snapshots.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow imports from sibling modules.
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import yaml  # noqa: E402

from ingest.common import get_db, _load_people  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# OPS pattern regexes — mirrors derive/jira_metrics.py::OPS_PATTERNS in shape.
# Kept inline (not imported) to avoid circular dep at build time. Sync manually
# if jira_metrics OPS_PATTERNS evolves.
OPS_PATTERNS = [
    ("incident",  re.compile(r"\b(P[01]|sev[-_ ]?[01]|incident|war[-_ ]?room|outage|degrad)", re.I)),
    ("drill",     re.compile(r"\b(DR[ -]?drill|disaster recovery|fire[-_ ]?drill|gameday)", re.I)),
    ("rca",       re.compile(r"\b(RCA|root cause|post[-_ ]?mortem|postmortem)", re.I)),
    ("year_end",  re.compile(r"\b(year[-_ ]?end|fy[-_ ]?end|financial year|FY26|FY25|EOY)", re.I)),
    ("rollback",  re.compile(r"\b(roll[-_ ]?back|revert|hotfix)", re.I)),
]


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _detect_ops_pattern(text: str) -> str | None:
    """Return first OPS_PATTERNS label matching text, or None."""
    if not text:
        return None
    for label, pat in OPS_PATTERNS:
        if pat.search(text):
            return label
    return None


def _build_actor_lookup() -> dict[str, str]:
    """Build {raw-identifier → canonical} map from people.yaml.

    Slack ingest stores raw U-id in events.actor. Map it back to canonical
    via people.yaml::slack_id field.
    """
    lookup: dict[str, str] = {}
    for p in _load_people():
        canonical = p.get("canonical")
        if not canonical:
            continue
        # Map every alias field to canonical.
        for field in ("slack_id", "email", "github", "jira_id", "name", "slack_handle"):
            v = p.get(field)
            if v:
                lookup[v] = canonical
        # canonical itself is also an alias (so resolve canonical→canonical idempotently).
        lookup[canonical] = canonical
    return lookup


def _load_channel_meta() -> dict[str, dict]:
    """Read state/slack_channel_meta.json (written by /slack-discover).

    Returns {channel_id: {name, class, …}}.
    """
    path = ROOT / "state" / "slack_channel_meta.json"
    if not path.exists():
        return {}
    with open(path) as f:
        cache = json.load(f)
    return cache.get("channels", {})


def _load_channel_classes() -> dict[str, str]:
    """Read config/slack_channels.yaml. Returns {channel_id: class}."""
    path = ROOT / "config" / "slack_channels.yaml"
    if not path.exists():
        return {}
    with open(path) as f:
        cfg = yaml.safe_load(f)
    return {c["id"]: c.get("class") for c in (cfg or {}).get("channels", []) if c.get("id") and c["id"] != "TODO"}


def _gather_thread_refs(conn: sqlite3.Connection, subject: str) -> dict[str, list[str]]:
    """Aggregate event_refs across every message in the thread.

    Returns dict with keys: tickets, pages, prs, threads — each sorted list.
    """
    cur = conn.execute(
        """SELECT DISTINCT r.ref_type, r.ref_value
             FROM event_refs r
             JOIN events e ON e.id = r.event_id
            WHERE e.subject = ?
              AND e.source = 'slack'
              AND e.deleted_ts IS NULL""",
        (subject,),
    )
    buckets: dict[str, set[str]] = {
        "tickets": set(), "pages": set(), "prs": set(), "threads": set(),
    }
    for rt, rv in cur.fetchall():
        if rt == "ticket":
            buckets["tickets"].add(rv)
        elif rt == "page":
            buckets["pages"].add(rv)
        elif rt == "pull_request":
            buckets["prs"].add(rv)
        elif rt == "slack_thread":
            # Drop self-references (thread mentioning its own permalink).
            if rv != subject:
                buckets["threads"].add(rv)
    return {k: sorted(v) for k, v in buckets.items()}


def build_thread_summary(
    conn: sqlite3.Connection,
    rebuild_all: bool = False,
    only_channel: str | None = None,
) -> dict:
    """Refresh thread_summary table from events + event_refs.

    rebuild_all: ignore last_ts cursor and rebuild every thread.
    only_channel: restrict to this channel_id.
    Returns counts: {threads, inserted, updated, skipped}.
    """
    actor_lookup = _build_actor_lookup()
    channel_meta = _load_channel_meta()
    channel_classes = _load_channel_classes()

    counts = {"threads": 0, "inserted": 0, "updated": 0, "skipped": 0}

    # Find every thread subject (= every distinct subject for source='slack').
    where = ["source = 'slack'", "deleted_ts IS NULL"]
    params: list = []
    if only_channel:
        where.append("channel_id = ?")
        params.append(only_channel)
    if not rebuild_all:
        # Incremental: only threads with messages newer than the current
        # thread_summary.computed_at for that subject. Threads with no row yet
        # always rebuild.
        # Implementation: subquery returns subjects where MAX(events.ts) >
        # COALESCE(thread_summary.computed_at, '').
        pass  # We just iterate all distinct subjects; per-thread short-circuit below.

    cur = conn.execute(
        f"""SELECT DISTINCT subject FROM events
             WHERE {' AND '.join(where)}
               AND subject IS NOT NULL""",
        params,
    )
    subjects = [r[0] for r in cur.fetchall()]

    for subject in subjects:
        counts["threads"] += 1

        # Pull all messages in thread (parent + replies), in chronological order.
        msgs = conn.execute(
            """SELECT id, actor, body, ts, thread_ts, channel_id
                 FROM events
                WHERE subject = ?
                  AND source = 'slack'
                  AND deleted_ts IS NULL
                ORDER BY ts ASC""",
            (subject,),
        ).fetchall()
        if not msgs:
            counts["skipped"] += 1
            continue

        first = msgs[0]
        last_ts = msgs[-1][3]  # ts column
        msg_count = len(msgs)

        # Incremental short-circuit: skip if stored row covers all current events.
        # Compare msg_count (catches new replies — events.ts is the Slack ts, not
        # an insertion-ts, so a 2025 reply ingested today still has ts < computed_at).
        if not rebuild_all:
            existing = conn.execute(
                "SELECT computed_at, msg_count FROM thread_summary WHERE subject = ?", (subject,),
            ).fetchone()
            if existing and existing[0] >= last_ts and existing[1] == msg_count:
                counts["skipped"] += 1
                continue

        # Channel metadata.
        channel_id = first[5]
        meta = channel_meta.get(channel_id, {})
        channel_name = meta.get("name")
        channel_class = channel_classes.get(channel_id)

        # Started-by canonical (parent message actor → canonical via lookup).
        started_by_canonical = actor_lookup.get(first[1])

        # Participants — every distinct actor across thread, resolved to canonical.
        participants: set[str] = set()
        for m in msgs:
            actor = m[1]
            canonical = actor_lookup.get(actor)
            if canonical:
                participants.add(canonical)
            # Unresolved actors silently dropped — picked up after --rebuild-all
            # when people.yaml updates.

        # Aggregate refs across the thread.
        refs = _gather_thread_refs(conn, subject)

        # Ops-pattern detection on first message body (title is body[:200]).
        first_body = first[2] or ""
        ops_label = _detect_ops_pattern(first_body)

        # First ts = parent.
        first_ts = first[3]

        # Build row.
        row = (
            subject, channel_id, channel_name, channel_class,
            started_by_canonical,
            json.dumps(sorted(participants)),
            first_ts, last_ts,
            msg_count, max(0, msg_count - 1),
            json.dumps(refs["tickets"]) if refs["tickets"] else None,
            json.dumps(refs["pages"]) if refs["pages"] else None,
            json.dumps(refs["prs"]) if refs["prs"] else None,
            json.dumps(refs["threads"]) if refs["threads"] else None,
            ops_label,
            None,  # digest — lazy at compaction
            _now_iso(),
        )

        # UPSERT.
        existing = conn.execute(
            "SELECT 1 FROM thread_summary WHERE subject = ?", (subject,),
        ).fetchone()

        conn.execute(
            """INSERT OR REPLACE INTO thread_summary
                 (subject, channel_id, channel_name, channel_class,
                  started_by_canonical, participants_json,
                  first_ts, last_ts, msg_count, reply_count,
                  referenced_tickets, referenced_pages, referenced_prs, referenced_threads,
                  ops_pattern_match, digest, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            row,
        )
        if existing:
            counts["updated"] += 1
        else:
            counts["inserted"] += 1

    conn.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild-all", action="store_true",
                        help="Rebuild every thread row regardless of computed_at cursor.")
    parser.add_argument("--channel", help="Restrict to one channel_id.")
    args = parser.parse_args()

    conn = get_db()
    counts = build_thread_summary(
        conn, rebuild_all=args.rebuild_all, only_channel=args.channel,
    )
    print("thread_summary build complete:")
    for k, v in counts.items():
        print(f"  {k:10s} {v}")


if __name__ == "__main__":
    main()

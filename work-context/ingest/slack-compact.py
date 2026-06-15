#!/usr/bin/env python3
"""
slack-compact.py — Slack thread compaction (chat-driven, no LLM in script).

Mirrors the /rollup pattern (manual-rollup.sh + dump_pending.py + apply_verdicts.py)
so this script never calls Anthropic / OpenAI / any LLM directly. The chat session
running /slack-compact does the digestion.

Two subcommands:

  dump        Scan events.db for Slack threads older than the compaction window
              (default 365 days) that aren't already in subject_summary AND aren't
              in a `compaction_policy: never` channel. Dump thread parent + every
              reply (with deleted tombstones excluded) to
              state/slack_compact_pending.json. The /slack-compact chat skill reads
              this file, produces 1-line digests, writes
              state/slack_compact_verdicts.json.

  apply       Read state/slack_compact_verdicts.json. For each verdict:
                INSERT INTO subject_summary (subject, source='slack', summary, ...)
                DELETE FROM event_refs WHERE event_id IN (thread's event ids)
                DELETE FROM events WHERE subject = <thread subject>
              Archive verdicts file with timestamp.

Usage:
    .venv/bin/python ingest/slack-compact.py dump [--days 365] [--limit 200]
    .venv/bin/python ingest/slack-compact.py apply

Hard rules:
- NEVER call any LLM library/API from this file. Compaction digests are produced
  by the chat session running /slack-compact.
- Skips `compaction_policy: never` channels regardless of age.
- Skips threads whose subject already has a slack-source subject_summary row.
- Preserves raw JSONL (raw/slack/YYYY/MM/DD.jsonl). Cold-storage replayable.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db, delete_events  # noqa: E402

ROOT = _PKG_ROOT
PENDING_PATH = ROOT / "state" / "slack_compact_pending.json"
VERDICTS_PATH = ROOT / "state" / "slack_compact_verdicts.json"
CHANNELS_YAML = ROOT / "config" / "slack_channels.yaml"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_channels() -> list[dict]:
    if not CHANNELS_YAML.exists():
        return []
    with open(CHANNELS_YAML) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("channels", [])


def _never_compact_channel_ids() -> set[str]:
    """Channels with compaction_policy: never. Their threads are preserved
    forever — see opsgenie-prod-service-c-alerts."""
    return {
        c["id"] for c in _load_channels()
        if c.get("id") and c["id"] != "TODO" and c.get("compaction_policy") == "never"
    }


def cmd_dump(args: argparse.Namespace) -> None:
    """Dump compaction candidates to state/slack_compact_pending.json."""
    conn = get_db()
    conn.row_factory = sqlite3.Row

    cutoff_iso = (datetime.now(tz=timezone.utc).timestamp() - args.days * 86400)
    # Convert to ISO for SQL comparison against events.ts.
    cutoff_iso = datetime.fromtimestamp(cutoff_iso, tz=timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")

    never_compact = _never_compact_channel_ids()
    never_compact_clause = (
        f"AND channel_id NOT IN ({','.join('?' * len(never_compact))})"
        if never_compact else ""
    )

    # Find candidate thread subjects:
    #  - source='slack'
    #  - latest message in thread is older than cutoff
    #  - not in a never-compact channel
    #  - not already in subject_summary for source='slack'
    sql = f"""
    WITH thread_age AS (
        SELECT subject, MAX(ts) AS last_ts, MIN(ts) AS first_ts,
               COUNT(*) AS msg_count, channel_id
          FROM events
         WHERE source = 'slack'
           AND deleted_ts IS NULL
           AND subject IS NOT NULL
         GROUP BY subject
        HAVING last_ts < ?
    )
    SELECT t.subject, t.channel_id, t.first_ts, t.last_ts, t.msg_count
      FROM thread_age t
     WHERE NOT EXISTS (
         SELECT 1 FROM subject_summary s
          WHERE s.subject = t.subject AND s.source = 'slack'
     )
     {never_compact_clause}
     ORDER BY t.last_ts ASC
     LIMIT ?
    """
    params: list = [cutoff_iso]
    if never_compact:
        params.extend(sorted(never_compact))
    params.append(args.limit)

    cur = conn.execute(sql, params)
    candidates = [dict(r) for r in cur.fetchall()]

    if not candidates:
        # Match rollup output convention.
        PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_PATH.write_text("[]")
        print(json.dumps({"dumped": 0, "message": "no threads to compact"}, indent=2))
        return

    # For each candidate, pull every (non-deleted) message in the thread.
    dump: list[dict] = []
    for c in candidates:
        msgs = conn.execute(
            """SELECT id, actor, body, ts, edited_ts, thread_ts
                 FROM events
                WHERE subject = ?
                  AND source = 'slack'
                  AND deleted_ts IS NULL
                ORDER BY ts ASC""",
            (c["subject"],),
        ).fetchall()
        # Aggregate denormed refs from event_refs across the whole thread.
        ref_rows = conn.execute(
            """SELECT DISTINCT r.ref_type, r.ref_value
                 FROM event_refs r
                 JOIN events e ON e.id = r.event_id
                WHERE e.subject = ? AND e.source = 'slack'""",
            (c["subject"],),
        ).fetchall()
        refs_by_type: dict[str, list[str]] = {}
        for rt, rv in ref_rows:
            refs_by_type.setdefault(rt, []).append(rv)
        for rt in refs_by_type:
            refs_by_type[rt].sort()

        dump.append({
            "subject": c["subject"],
            "channel_id": c["channel_id"],
            "first_ts": c["first_ts"],
            "last_ts": c["last_ts"],
            "msg_count": c["msg_count"],
            "messages": [
                {
                    "actor": m["actor"],
                    "ts": m["ts"],
                    "body": m["body"],
                    "edited_ts": m["edited_ts"],
                    "thread_ts": m["thread_ts"],
                } for m in msgs
            ],
            "refs": refs_by_type,
        })

    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(dump, indent=2, default=str))

    print(json.dumps({
        "dumped": len(dump),
        "path": str(PENDING_PATH),
        "cutoff": cutoff_iso,
        "next_step": "Run /slack-compact in chat — produces "
                     "state/slack_compact_verdicts.json — then `slack-compact.py apply`.",
    }, indent=2))


def cmd_apply(args: argparse.Namespace) -> None:
    """Read verdicts, write to subject_summary, delete raw events."""
    if not VERDICTS_PATH.exists():
        print(json.dumps({"error": "verdicts_missing",
                          "path": str(VERDICTS_PATH),
                          "hint": "Run /slack-compact in chat first"}), file=sys.stderr)
        sys.exit(2)

    verdicts = json.loads(VERDICTS_PATH.read_text())
    if not isinstance(verdicts, list) or not verdicts:
        print(json.dumps({"applied": 0, "message": "verdicts file empty"}))
        return

    conn = get_db()
    counts = {"applied": 0, "skipped_low_conf": 0, "errors": []}

    for v in verdicts:
        subject = v.get("subject")
        digest = v.get("digest") or v.get("summary")
        confidence = float(v.get("confidence", 0))
        if not subject or not digest:
            counts["errors"].append(f"missing-fields: {v}")
            continue
        # Reject low-confidence digests — leave thread in events for next compaction round.
        if confidence < 0.7:
            counts["skipped_low_conf"] += 1
            continue

        try:
            with conn:  # transaction
                # Insert digest into subject_summary.
                conn.execute(
                    """INSERT OR REPLACE INTO subject_summary
                       (subject, content_hash, domains, summary, risk_flags, confidence,
                        source, model, classified_at)
                       VALUES (?, '', '[]', ?, '[]', ?, 'slack', 'chat', ?)""",
                    (subject, digest[:500], confidence, _now_iso()),
                )
                # Delete events + event_refs + events_fts for the thread
                # (preserves raw JSONL). Shared deleter cascades refs/fts so the
                # thread's rows can't leak orphan refs once the parent is gone.
                event_ids = [r[0] for r in conn.execute(
                    "SELECT id FROM events WHERE subject = ? AND source = 'slack'",
                    (subject,),
                ).fetchall()]
                if event_ids:
                    delete_events(conn, event_ids, commit=False)
                # thread_summary row also stale — drop it; if owner queries the
                # subject later, subject_summary serves the digest.
                conn.execute("DELETE FROM thread_summary WHERE subject = ?", (subject,))
                counts["applied"] += 1
        except sqlite3.Error as e:
            counts["errors"].append(f"{subject}: {e}")

    # Archive verdicts file with timestamp.
    archive_path = VERDICTS_PATH.with_name(
        f"slack_compact_verdicts.{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    )
    VERDICTS_PATH.rename(archive_path)
    # Clear pending file too — it's been processed.
    if PENDING_PATH.exists():
        PENDING_PATH.unlink()

    counts["archived_to"] = str(archive_path)
    print(json.dumps(counts, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dump = sub.add_parser("dump", help="Scan events.db, dump compaction candidates.")
    p_dump.add_argument("--days", type=int, default=365,
                        help="Compact threads older than N days (default 365).")
    p_dump.add_argument("--limit", type=int, default=200,
                        help="Max threads per dump (default 200, keeps chat context bounded).")
    p_dump.set_defaults(func=cmd_dump)

    p_apply = sub.add_parser("apply", help="Read verdicts, write subject_summary, delete events.")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

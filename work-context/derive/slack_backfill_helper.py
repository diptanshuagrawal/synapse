#!/usr/bin/env python3
"""
slack_backfill_helper.py — operational glue for the `/slack-backfill` skill.

Replaces ~30 model turns of drain-loop + pending-recompute boilerplate with
three commands the skill invokes via Bash:

    drain-channel <channel-id>     # drain all cached slack_read_channel pages
    drain-threads <channel-id>     # drain all cached slack_read_thread responses
    pending-threads <channel-id>   # list thread_parent_ts still needing fetch
    status <channel-id>            # one-line counters: top, replies, missing, ready_to_drain

The skill itself still fires `slack_read_thread` MCP calls — those can't be
made from this script because the MCP transport is tied to the Claude harness.

Workflow (skill calls):
    Phase 2b. Channel pages → fire slack_read_channel; hook persists; call
              `drain-channel C…` after each batch to flush stubs to DB.
    Phase 2c. Threads → run `pending-threads C…` to get parent_ts list, fire
              slack_read_thread (batch-8) for each, then `drain-threads C…`
              every ~50 fetches to bound cache dir size.
    Phase 2e. build_thread_summary (existing tool).

Idempotent — re-running `drain-*` on already-processed cache is a no-op
(processed files have `.processed` suffix and are skipped).

Schema requirement: `reply_count` column on events (migration in
ingest/common.py). Pending list is derived from rows where
event_type='thread_started' AND reply_count > 0 AND no thread_reply children
yet in events.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Filename: <channel>_<ms>_thread_<parent_ts>[_c<cursor-hash>].txt  (see bin/slack-mcp-persist.sh)
# Cursor-pagination suffix `_c<hash>` must be stripped before using parent_ts.
# Slack pagination cursors are opaque tokens that can be base64 or
# URL-safe-base64 — they contain `+`, `/`, `=`, `-`, `_` and mixed case in
# addition to hex. A hex-only regex silently failed to strip those suffixes,
# leaving `parent_ts` polluted with the cursor tail (and the helper then
# called `replies(ts=<garbage>)` which 404'd).
_THREAD_FNAME_PARENT_RE = re.compile(
    r"_thread_(\d+\.\d+)(?:_c[0-9A-Za-z+/=_\-]+)?$"
)

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import get_db, DB_PATH  # noqa: E402

CACHE_DIR = Path("/tmp/slack_mcp_cache")
RUNNER = _REPO_ROOT / ".venv" / "bin" / "python", str(_REPO_ROOT / "derive" / "slack_ingest_runner.py")


def _iter_unprocessed(channel_id: str, kind: str) -> list[Path]:
    """Return cached MCP responses for this channel of the given kind.

    kind='channel'  → `<ch>_<epoch>[_<hash>].txt`         (no `_thread_` infix)
    kind='thread'   → `<ch>_<epoch>_thread_<parent_ts>.txt`
    Already-drained files have a `.processed` suffix and are excluded.
    """
    if not CACHE_DIR.exists():
        return []
    out: list[Path] = []
    for p in sorted(CACHE_DIR.glob(f"{channel_id}_*.txt")):
        is_thread = "_thread_" in p.name
        if kind == "thread" and not is_thread:
            continue
        if kind == "channel" and is_thread:
            continue
        out.append(p)
    return out


def _drain_one(path: Path, channel_id: str, thread_parent_ts: str | None) -> dict:
    """Run slack_ingest_runner.py upsert on one cache file. Returns parsed JSON."""
    cmd = [
        *RUNNER, "upsert",
        "--channel-id", channel_id,
        "--response-file", str(path),
    ]
    if thread_parent_ts:
        cmd += ["--thread-parent-ts", thread_parent_ts]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return {"error": "non_json_stdout", "stdout": proc.stdout[:200], "stderr": proc.stderr[:200]}


def cmd_drain_channel(args: argparse.Namespace) -> None:
    files = _iter_unprocessed(args.channel_id, "channel")
    inserted = updated = 0
    errors: list[str] = []
    for f in files:
        r = _drain_one(f, args.channel_id, None)
        inserted += int(r.get("inserted", 0) or 0)
        updated += int(r.get("updated", 0) or 0)
        if r.get("error"):
            errors.append(f"{f.name}: {r['error']}")
        else:
            f.rename(f.with_suffix(f.suffix + ".processed"))
    print(json.dumps({
        "channel_id": args.channel_id,
        "phase": "drain-channel",
        "drained": len(files),
        "inserted": inserted,
        "updated": updated,
        "errors": errors,
    }))


def _clamp_parent_after_drain(conn: sqlite3.Connection, channel_id: str,
                              parent_ts_epoch: str) -> None:
    """Post-drain: set parent.reply_count = actual thread_reply count, and
    stamp drain_attempted_at = now. Closes the chronic-stale loop where MCP's
    thread-fetch can't return as many replies as Slack's channel-page metadata
    declared (pagination cap or deleted-reply skew)."""
    try:
        dt = datetime.fromtimestamp(float(parent_ts_epoch), tz=timezone.utc)
    except (ValueError, OSError):
        return
    parent_iso = dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
    actual = conn.execute(
        """SELECT COUNT(*) FROM events
            WHERE source='slack' AND channel_id=?
              AND event_type='thread_reply'
              AND thread_ts=?""",
        (channel_id, parent_ts_epoch),
    ).fetchone()[0]
    now_iso = datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    with conn:
        conn.execute(
            """UPDATE events
                  SET reply_count = ?,
                      drain_attempted_at = ?
                WHERE source='slack' AND channel_id=?
                  AND event_type='thread_started'
                  AND ts = ?""",
            (actual, now_iso, channel_id, parent_iso),
        )


def cmd_drain_threads(args: argparse.Namespace) -> None:
    files = _iter_unprocessed(args.channel_id, "thread")
    inserted = updated = 0
    clamped = 0
    errors: list[str] = []
    conn = get_db()
    for f in files:
        # Filename: <ch>_<ms>_thread_<parent_ts>[_c<8hex>].txt
        m = _THREAD_FNAME_PARENT_RE.search(f.stem)
        if not m:
            errors.append(f"{f.name}: malformed_filename")
            continue
        parent_ts = m.group(1)
        r = _drain_one(f, args.channel_id, parent_ts)
        inserted += int(r.get("inserted", 0) or 0)
        updated += int(r.get("updated", 0) or 0)
        if r.get("error"):
            errors.append(f"{f.name}: {r['error']}")
        else:
            f.rename(f.with_suffix(f.suffix + ".processed"))
            _clamp_parent_after_drain(conn, args.channel_id, parent_ts)
            clamped += 1
    print(json.dumps({
        "channel_id": args.channel_id,
        "phase": "drain-threads",
        "drained": len(files),
        "parents_clamped": clamped,
        "inserted": inserted,
        "updated": updated,
        "errors": errors,
    }))


def _pending_thread_parents(conn: sqlite3.Connection, channel_id: str) -> list[str]:
    """Slack ts (epoch-string) for every top-level msg with replies that we
    haven't yet pulled any thread_reply row for. Caller fires slack_read_thread
    on each."""
    # Step 1: thread_starteds in this channel with reply_count > 0.
    candidates = {
        row[0] for row in conn.execute(
            """SELECT ts FROM events
                WHERE source='slack' AND channel_id=?
                  AND event_type='thread_started'
                  AND reply_count > 0""",
            (channel_id,),
        )
        # `ts` here is ISO; convert to epoch-string to match thread_ts format.
    }
    # ts column is ISO; thread_ts column is epoch float string. Convert.
    iso_to_epoch: dict[str, str] = {}
    for iso in candidates:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            iso_to_epoch[iso] = f"{dt.timestamp():.6f}"
        except ValueError:
            continue

    # Step 2: which of those have ≥1 thread_reply row already?
    done = {
        row[0] for row in conn.execute(
            """SELECT DISTINCT thread_ts FROM events
                WHERE source='slack' AND channel_id=?
                  AND event_type='thread_reply'""",
            (channel_id,),
        )
    }

    pending_epoch = [ep for iso, ep in iso_to_epoch.items() if ep not in done]
    pending_epoch.sort(key=lambda s: float(s))
    return pending_epoch


def cmd_pending_threads(args: argparse.Namespace) -> None:
    conn = get_db()
    pending = _pending_thread_parents(conn, args.channel_id)
    for ts in pending:
        print(ts)


def cmd_seed_reply_count(args: argparse.Namespace) -> None:
    """One-shot migration helper for channels ingested BEFORE reply_count
    column existed. Counts thread_reply children per thread_ts (Slack-format
    epoch) and writes the count back onto the matching thread_started row.

    Idempotent — over-writes any existing reply_count. Safe to re-run.
    """
    conn = get_db()
    # Aggregate replies per thread_ts.
    rows = conn.execute(
        """SELECT thread_ts, COUNT(*) FROM events
            WHERE source='slack' AND channel_id=?
              AND event_type='thread_reply'
              AND thread_ts IS NOT NULL
            GROUP BY thread_ts""",
        (args.channel_id,),
    ).fetchall()

    updated = 0
    not_found = 0
    with conn:
        for thread_ts_epoch, count in rows:
            # Convert epoch-string back to ISO and update.
            try:
                dt = datetime.fromtimestamp(float(thread_ts_epoch), tz=timezone.utc)
                iso = dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
            except ValueError:
                not_found += 1
                continue
            cur = conn.execute(
                """UPDATE events SET reply_count=?
                    WHERE source='slack' AND channel_id=?
                      AND event_type='thread_started'
                      AND ts=?""",
                (count, args.channel_id, iso),
            )
            if cur.rowcount:
                updated += 1
            else:
                not_found += 1

    print(json.dumps({
        "channel_id": args.channel_id,
        "phase": "seed-reply-count",
        "thread_groups_found": len(rows),
        "parents_updated": updated,
        "parents_not_found": not_found,
    }))


_STALE_COOLDOWN_HOURS = 24


def _stale_thread_parents(conn: sqlite3.Connection, channel_id: str) -> list[tuple[str, int, int]]:
    """Parents where stored reply_count > thread_reply rows actually in DB.

    Returns list of (parent_ts_epoch, stored_reply_count, replies_in_db).
    Used by /slack-ingest's optional reconcile pass to refetch threads that
    have grown since their last fetch, and by /slack-backfill to surface
    incomplete ingest after a partial fire.

    Excludes parents drained in the last `_STALE_COOLDOWN_HOURS` hours — those
    were already retried recently and (per the MCP thread-pagination cap) won't
    yield more replies until Slack's channel-page metadata next bumps. Without
    this filter the same long threads (declared 200+, MCP returns 100/page)
    flap stale every fire and waste a thread-fetch round-trip.
    """
    from datetime import timedelta
    cutoff_iso = (
        datetime.now(tz=timezone.utc) - timedelta(hours=_STALE_COOLDOWN_HOURS)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    # All parents with declared reply_count > 0, ignoring those drained recently.
    parents = conn.execute(
        """SELECT ts, reply_count FROM events
            WHERE source='slack' AND channel_id=?
              AND event_type='thread_started'
              AND reply_count > 0
              AND (drain_attempted_at IS NULL OR drain_attempted_at < ?)""",
        (channel_id, cutoff_iso),
    ).fetchall()

    # Replies indexed by epoch-format thread_ts.
    reply_counts: dict[str, int] = {
        row[0]: row[1]
        for row in conn.execute(
            """SELECT thread_ts, COUNT(*) FROM events
                WHERE source='slack' AND channel_id=?
                  AND event_type='thread_reply'
                  AND thread_ts IS NOT NULL
                GROUP BY thread_ts""",
            (channel_id,),
        )
    }

    stale: list[tuple[str, int, int]] = []
    for iso_ts, stored_rc in parents:
        try:
            dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
            epoch = f"{dt.timestamp():.6f}"
        except ValueError:
            continue
        actual = reply_counts.get(epoch, 0)
        if stored_rc > actual:
            stale.append((epoch, stored_rc, actual))
    stale.sort(key=lambda x: float(x[0]))
    return stale


def cmd_stale_threads(args: argparse.Namespace) -> None:
    stale = _stale_thread_parents(get_db(), args.channel_id)
    if args.verbose:
        for ts, stored, actual in stale:
            print(f"{ts}\t{stored}\t{actual}")
    else:
        for ts, _, _ in stale:
            print(ts)


_ACTIVE_THREAD_DAYS = 90  # default activity window for late-reply re-drain


def _active_thread_parents(
    conn: sqlite3.Connection, channel_id: str, days: int = _ACTIVE_THREAD_DAYS
) -> list[str]:
    """Parents (reply_count>0) whose newest *known* reply is within `days`, and
    that weren't drained in the last `_STALE_COOLDOWN_HOURS`. Epoch parent_ts,
    hottest first.

    Closes the late-reply blind spot: when a reply lands on a thread whose
    parent has already scrolled below the channel cursor, cursor-bound
    incremental history never re-surfaces the old parent, and the 24h reconcile
    pass gates on *parent age* — so neither re-drains it, and the stale detector
    can't fire because the stored reply_count is frozen at last drain. Gating on
    *activity* (newest reply we already have) re-polls still-active old threads
    via iter_replies, which pulls the missing late replies and re-clamps
    reply_count. The drain cooldown throttles each thread to ~once/day.
    """
    from datetime import timedelta
    now = datetime.now(tz=timezone.utc)
    drain_cutoff_iso = (now - timedelta(hours=_STALE_COOLDOWN_HOURS)).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    active_since_iso = (now - timedelta(days=days)).isoformat(
        timespec="seconds").replace("+00:00", "Z")

    parents = conn.execute(
        """SELECT ts FROM events
            WHERE source='slack' AND channel_id=?
              AND event_type='thread_started'
              AND reply_count > 0
              AND (drain_attempted_at IS NULL OR drain_attempted_at < ?)""",
        (channel_id, drain_cutoff_iso),
    ).fetchall()

    # newest reply ts (ISO) per thread, keyed by epoch thread_ts
    newest_reply: dict[str, str] = {
        row[0]: row[1]
        for row in conn.execute(
            """SELECT thread_ts, MAX(ts) FROM events
                WHERE source='slack' AND channel_id=?
                  AND event_type='thread_reply'
                  AND thread_ts IS NOT NULL
                GROUP BY thread_ts""",
            (channel_id,),
        )
    }

    active: list[tuple[str, str]] = []  # (epoch_parent_ts, newest_activity_iso)
    for (iso_ts,) in parents:
        try:
            dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
            epoch = f"{dt.timestamp():.6f}"
        except ValueError:
            continue
        activity_iso = newest_reply.get(epoch, iso_ts)  # fall back to parent ts
        if activity_iso >= active_since_iso:
            active.append((epoch, activity_iso))
    active.sort(key=lambda x: x[1], reverse=True)  # hottest first
    return [epoch for epoch, _ in active]


def cmd_active_threads(args: argparse.Namespace) -> None:
    for ts in _active_thread_parents(get_db(), args.channel_id, args.days):
        print(ts)


# ── Pending reply-check queue (team_involved starvation backlog) ──────────────
#
# These operate on the `slack_pending_reply_check` table (schema in
# ingest/common.py). They take an open conn rather than shelling out, because
# the caller (ingest/slack_ingest_app.py team_involved path) already holds one
# and we want the enqueue/dequeue inside the same fire transactionally.
#
# Lifecycle:
#   enqueue  — a bot-rooted thread with replies was starved of reply-walk
#              budget this fire; record it so a later fire can walk it.
#   pop      — pull up to `cap` oldest-first queued parents to drain.
#   dequeue  — the parent was resolved (kept or confirmed no-team); remove it.
#   bump_attempts — the walk errored; leave queued but count the attempt so a
#              permanently-broken parent eventually hits the abandon ceiling.


def enqueue_pending_reply_check(
    conn: sqlite3.Connection, channel_id: str, parent_ts: str,
    reply_count: int | None,
) -> None:
    """Record a budget-starved bot-rooted parent for a later reply walk.

    Idempotent: re-enqueuing an already-queued parent refreshes its declared
    reply_count (it may have grown) but preserves first_seen + attempts.
    """
    now_iso = datetime.now(tz=timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    with conn:
        conn.execute(
            """INSERT INTO slack_pending_reply_check
                   (channel_id, parent_ts, reply_count, first_seen, attempts)
               VALUES (?, ?, ?, ?, 0)
               ON CONFLICT(channel_id, parent_ts) DO UPDATE SET
                   reply_count = excluded.reply_count""",
            (channel_id, parent_ts, reply_count, now_iso),
        )


def pop_pending_reply_checks(
    conn: sqlite3.Connection, channel_id: str, cap: int,
    max_attempts: int,
) -> list[tuple[str, int | None, int]]:
    """Return up to `cap` oldest queued parents for this channel to drain.

    Skips (and removes) parents that have already exhausted `max_attempts`
    drain tries — those are abandoned to avoid looping forever on a parent
    whose replies endpoint keeps failing. Returns
    [(parent_ts, reply_count, attempts), ...], oldest-first.
    """
    # Abandon parents past the attempt ceiling first (best-effort cleanup).
    with conn:
        conn.execute(
            """DELETE FROM slack_pending_reply_check
                WHERE channel_id=? AND attempts >= ?""",
            (channel_id, max_attempts),
        )
    rows = conn.execute(
        """SELECT parent_ts, reply_count, attempts
             FROM slack_pending_reply_check
            WHERE channel_id=?
            ORDER BY first_seen ASC, parent_ts ASC
            LIMIT ?""",
        (channel_id, cap),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def dequeue_pending_reply_check(
    conn: sqlite3.Connection, channel_id: str, parent_ts: str,
) -> None:
    """Remove a resolved parent from the queue."""
    with conn:
        conn.execute(
            """DELETE FROM slack_pending_reply_check
                WHERE channel_id=? AND parent_ts=?""",
            (channel_id, parent_ts),
        )


def bump_pending_reply_attempt(
    conn: sqlite3.Connection, channel_id: str, parent_ts: str,
) -> None:
    """Increment attempts for a parent whose drain errored (kept for retry)."""
    with conn:
        conn.execute(
            """UPDATE slack_pending_reply_check
                  SET attempts = attempts + 1
                WHERE channel_id=? AND parent_ts=?""",
            (channel_id, parent_ts),
        )


def count_pending_reply_checks(conn: sqlite3.Connection, channel_id: str) -> int:
    """Current queue depth for a channel (for fire summaries / cron-status)."""
    return conn.execute(
        "SELECT COUNT(*) FROM slack_pending_reply_check WHERE channel_id=?",
        (channel_id,),
    ).fetchone()[0]


def cmd_stale_threads_all(args: argparse.Namespace) -> None:
    """Emit {channel_id: [stale_parent_ts, ...]} for every configured channel.

    Replaces the shell `for ID …; do stale-threads $ID; done` loop in
    /slack-ingest so the skill avoids shell variable expansion (gated by harness
    even with broad allow-rules)."""
    cfg_path = _REPO_ROOT / "config" / "slack_channels.yaml"
    with cfg_path.open() as f:
        cfg = yaml.safe_load(f)
    conn = get_db()
    out: dict = {}
    for c in cfg.get("channels", []):
        cid = c.get("id")
        if not cid or cid == "TODO":
            continue
        stale = _stale_thread_parents(conn, cid)
        out[cid] = [ts for ts, _, _ in stale]
    print(json.dumps(out, indent=2))


def cmd_status(args: argparse.Namespace) -> None:
    conn = get_db()
    row = conn.execute(
        """SELECT
              SUM(CASE WHEN event_type='thread_started' THEN 1 ELSE 0 END) AS top_level,
              SUM(CASE WHEN event_type='thread_started' AND reply_count > 0 THEN 1 ELSE 0 END) AS parents_with_replies,
              SUM(CASE WHEN event_type='thread_reply' THEN 1 ELSE 0 END) AS replies,
              SUM(reply_count) AS declared_reply_total
             FROM events
            WHERE source='slack' AND channel_id=?""",
        (args.channel_id,),
    ).fetchone()
    pending = _pending_thread_parents(conn, args.channel_id)
    stale = _stale_thread_parents(conn, args.channel_id)
    ready_to_drain_channel = len(_iter_unprocessed(args.channel_id, "channel"))
    ready_to_drain_threads = len(_iter_unprocessed(args.channel_id, "thread"))
    print(json.dumps({
        "channel_id": args.channel_id,
        "top_level": row[0] or 0,
        "parents_with_replies": row[1] or 0,
        "replies_ingested": row[2] or 0,
        "declared_reply_total": row[3] or 0,
        "pending_thread_fetches": len(pending),
        "stale_thread_parents": len(stale),
        "cache_ready_to_drain": {
            "channel_pages": ready_to_drain_channel,
            "threads": ready_to_drain_threads,
        },
    }, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("drain-channel", help="Upsert all cached slack_read_channel pages")
    p.add_argument("channel_id")
    p.set_defaults(func=cmd_drain_channel)

    p = sub.add_parser("drain-threads", help="Upsert all cached slack_read_thread responses")
    p.add_argument("channel_id")
    p.set_defaults(func=cmd_drain_threads)

    p = sub.add_parser("pending-threads", help="List thread_parent_ts still needing fetch")
    p.add_argument("channel_id")
    p.set_defaults(func=cmd_pending_threads)

    p = sub.add_parser("status", help="One-shot counters + cache state")
    p.add_argument("channel_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("seed-reply-count",
                       help="Backfill reply_count for legacy channels ingested before the column existed")
    p.add_argument("channel_id")
    p.set_defaults(func=cmd_seed_reply_count)

    p = sub.add_parser("stale-threads",
                       help="List parents where stored reply_count > replies actually in DB "
                            "(threads that gained new replies after initial ingest)")
    p.add_argument("channel_id")
    p.add_argument("--verbose", action="store_true",
                   help="Also print stored and actual counts (tab-separated)")
    p.set_defaults(func=cmd_stale_threads)

    p = sub.add_parser("stale-threads-all",
                       help="Emit {channel_id: [stale_parent_ts, ...]} for every channel "
                            "in config/slack_channels.yaml (no shell loop needed)")
    p.set_defaults(func=cmd_stale_threads_all)

    p = sub.add_parser("active-threads",
                       help="List parents whose newest reply is within --days "
                            "(re-drains late replies to old threads the cursor + "
                            "24h reconcile both miss)")
    p.add_argument("channel_id")
    p.add_argument("--days", type=int, default=_ACTIVE_THREAD_DAYS)
    p.set_defaults(func=cmd_active_threads)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

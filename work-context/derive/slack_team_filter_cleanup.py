#!/usr/bin/env python3
"""
slack_team_filter_cleanup.py — one-shot retro-cleanup of slack rows in
channels now flagged `ingest_mode: team_involved`.

Drops historical threads (parent + replies + thread_summary rows) where
NO team member participated. Mirrors the ingest-time filter in
slack_ingest_app.fetch_history_team_filtered.

Rules per thread (parent_ts):
  KEEP if ANY of:
    - parent.actor canonical resolves to a team slack-id
    - any reply.actor on team
    - parent body @-mentions a team slack-id (`<@U…>` form)
    - parent body @-mentions a team subteam handle (`<!subteam^S…>` form)
    - any reply body @-mentions a team slack-id
    - any reply body @-mentions a team subteam handle

DELETE otherwise: parent row + reply rows + event_refs + thread_summary row.

Dry-run by default. --apply commits the deletes inside a transaction.

Usage:
    python -m derive.slack_team_filter_cleanup                # dry, all channels with team_involved
    python -m derive.slack_team_filter_cleanup --apply
    python -m derive.slack_team_filter_cleanup --channel C0EXAMPLE  # one channel
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH  # noqa: E402
from derive.slack_team import load_team_slack_ids, load_team_subteam_ids  # noqa: E402

CHANNELS_YAML = _REPO_ROOT / "config" / "slack_channels.yaml"
PEOPLE_YAML = _REPO_ROOT / "config" / "people.yaml"


def _team_involved_channels() -> list[dict]:
    with CHANNELS_YAML.open() as f:
        cfg = yaml.safe_load(f)
    return [c for c in cfg.get("channels", [])
            if str(c.get("ingest_mode", "")).lower() == "team_involved"]


def _team_canonicals() -> set[str]:
    """Set of canonical names for team members.

    DB rows store `actor` as JSON `{id, name}` or as the canonical name
    directly (depends on ingest path). We match on canonical for safety.
    """
    team_slack_ids = load_team_slack_ids()
    return set(team_slack_ids.values())


def _is_team_actor(actor_field: str | None, team_canonicals: set[str],
                   team_slack_ids: set[str]) -> bool:
    """`actor` column may be:
       - canonical string ("bob-example")
       - slack U-id ("U0EXAMPLE")
       - JSON dict {id, name}
    Cover all three shapes."""
    if not actor_field:
        return False
    if actor_field in team_canonicals:
        return True
    if actor_field in team_slack_ids:
        return True
    try:
        obj = json.loads(actor_field)
        if isinstance(obj, dict):
            if obj.get("id") in team_slack_ids:
                return True
            if obj.get("name") in team_canonicals:
                return True
    except (json.JSONDecodeError, TypeError):
        pass
    return False


def _body_mentions_team(body: str | None, team_slack_ids: set[str],
                        team_subteam_ids: set[str] | None = None) -> bool:
    """Mentions checked: individual @UID (`<@U…>`) + team subteam handle
    (`<!subteam^S…>`). Mirrors `derive.slack_team.is_team_involved`."""
    if not body:
        return False
    for uid in team_slack_ids:
        if f"<@{uid}" in body:
            return True
    if team_subteam_ids:
        for sid in team_subteam_ids:
            if f"<!subteam^{sid}" in body:
                return True
    return False


def _scan_channel(conn: sqlite3.Connection, channel_id: str,
                  team_canonicals: set[str], team_slack_ids: set[str],
                  team_subteam_ids: set[str] | None = None) -> dict:
    """Walk all top-level msgs in channel. For each, decide keep/drop.
    Returns {keep_parents, drop_parents, drop_replies, ids_to_delete}."""
    parents = conn.execute(
        """SELECT id, ts, actor, body FROM events
           WHERE source='slack' AND channel_id=? AND event_type='thread_started'""",
        (channel_id,),
    ).fetchall()

    keep_parents = 0
    drop_parents = 0
    drop_replies = 0
    ids_to_delete: list[str] = []
    parents_to_delete: list[str] = []  # parent slack-ts for thread_summary delete

    for parent_id, parent_iso, parent_actor, parent_body in parents:
        # Slack-epoch from id
        try:
            parent_ts_epoch = parent_id.rsplit(":", 1)[1]
        except IndexError:
            continue
        # Check parent
        team = _is_team_actor(parent_actor, team_canonicals, team_slack_ids) \
            or _body_mentions_team(parent_body, team_slack_ids, team_subteam_ids)
        if not team:
            # Check replies
            replies = conn.execute(
                """SELECT id, actor, body FROM events
                   WHERE source='slack' AND channel_id=? AND event_type='thread_reply'
                     AND thread_ts=?""",
                (channel_id, parent_ts_epoch),
            ).fetchall()
            for _, r_actor, r_body in replies:
                if _is_team_actor(r_actor, team_canonicals, team_slack_ids) \
                        or _body_mentions_team(r_body, team_slack_ids, team_subteam_ids):
                    team = True
                    break
            if not team:
                # Mark for deletion
                drop_parents += 1
                ids_to_delete.append(parent_id)
                parents_to_delete.append(parent_ts_epoch)
                for r_id, _, _ in replies:
                    ids_to_delete.append(r_id)
                    drop_replies += 1
                continue
        keep_parents += 1

    return {
        "channel_id": channel_id,
        "total_parents": len(parents),
        "keep_parents": keep_parents,
        "drop_parents": drop_parents,
        "drop_replies": drop_replies,
        "ids_to_delete": ids_to_delete,
        "parent_ts_to_delete": parents_to_delete,
    }


def _delete_batch(conn: sqlite3.Connection, channel_id: str,
                  ids: list[str], parent_ts_list: list[str]) -> dict:
    """Delete events + event_refs + thread_summary in one transaction.
    Returns counts."""
    deleted_events = deleted_refs = deleted_summaries = 0
    BATCH = 500
    with conn:
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            placeholders = ",".join("?" * len(chunk))
            cur = conn.execute(
                f"DELETE FROM event_refs WHERE event_id IN ({placeholders})", chunk,
            )
            deleted_refs += cur.rowcount
            cur = conn.execute(
                f"DELETE FROM events WHERE id IN ({placeholders})", chunk,
            )
            deleted_events += cur.rowcount
        # thread_summary keyed by (channel_id, parent_ts)
        for i in range(0, len(parent_ts_list), BATCH):
            chunk = parent_ts_list[i:i + BATCH]
            placeholders = ",".join("?" * len(chunk))
            try:
                cur = conn.execute(
                    f"DELETE FROM thread_summary WHERE channel_id=? AND parent_ts IN ({placeholders})",
                    [channel_id, *chunk],
                )
                deleted_summaries += cur.rowcount
            except sqlite3.OperationalError:
                # thread_summary table may not exist on legacy DBs
                pass
    return {"events": deleted_events, "refs": deleted_refs, "summaries": deleted_summaries}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", help="single channel id (default: all team_involved channels)")
    ap.add_argument("--apply", action="store_true",
                    help="commit deletes; default is dry-run")
    args = ap.parse_args()

    channels = _team_involved_channels()
    if args.channel:
        channels = [c for c in channels if c.get("id") == args.channel]
        if not channels:
            print(f"channel {args.channel} not in team_involved set", file=sys.stderr)
            return 1
    if not channels:
        print("no channels flagged ingest_mode: team_involved in yaml")
        return 0

    team_canonicals = _team_canonicals()
    team_slack_ids = set(load_team_slack_ids().keys())
    team_subteam_ids = load_team_subteam_ids()
    print(f"[team] {len(team_slack_ids)} slack-ids · {len(team_canonicals)} canonicals · "
          f"{len(team_subteam_ids)} subteam-ids\n", flush=True)

    conn = sqlite3.connect(DB_PATH)

    overall = {"events": 0, "refs": 0, "summaries": 0}
    print(f"{'channel':<35}  {'total':>7}  {'keep':>7}  {'drop_p':>7}  {'drop_r':>7}")
    print("-" * 75)
    for ch in channels:
        cid = ch["id"]
        cname = ch.get("name", cid)
        plan = _scan_channel(conn, cid, team_canonicals, team_slack_ids, team_subteam_ids)
        print(f"{cname[:35]:<35}  {plan['total_parents']:>7}  {plan['keep_parents']:>7}  "
              f"{plan['drop_parents']:>7}  {plan['drop_replies']:>7}")

        if not args.apply or not plan["ids_to_delete"]:
            continue

        # Backup row IDs to a file for emergency restore reference.
        ts_now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = _REPO_ROOT / "state" / f"slack_team_cleanup_{cid}_{ts_now}.txt"
        backup_path.write_text("\n".join(plan["ids_to_delete"]) + "\n")
        print(f"  backup: wrote {len(plan['ids_to_delete'])} ids to {backup_path}")

        counts = _delete_batch(conn, cid, plan["ids_to_delete"], plan["parent_ts_to_delete"])
        overall["events"] += counts["events"]
        overall["refs"] += counts["refs"]
        overall["summaries"] += counts["summaries"]
        print(f"  deleted: events={counts['events']}  refs={counts['refs']}  summaries={counts['summaries']}")

    conn.close()

    print(f"\n[summary] mode={'APPLY' if args.apply else 'DRY-RUN'}")
    if args.apply:
        print(f"[summary] total deleted: events={overall['events']}  "
              f"refs={overall['refs']}  summaries={overall['summaries']}")
        print("[summary] backups in state/slack_team_cleanup_*.txt")
    else:
        print("[summary] re-run with --apply to commit deletes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

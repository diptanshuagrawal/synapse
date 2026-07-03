#!/usr/bin/env python3
"""slack_alert_thread_reconcile.py — daily team-reply capture on no_threads channels.

`no_threads: true` channels (bot alert firehoses) skip per-fire reply reconcile
to keep ingest fast. But their threads DO carry occasional team triage (who
picked up an alert, "false positive", escalations) — measured ~1,325 team
replies across the 14 such channels.

This runs ONCE A DAY (gated by the wrapper). For each no_threads channel it
scans LIVE channel history (Slack API) for thread parents whose alert fired in
the last LOOKBACK_DAYS, fetches the replies, and upserts ONLY the team-involved
ones (author ∈ team OR body @-mentions a team member OR pings a team subteam).
When a thread has team replies, the parent alert is upserted too, so the thread
root resolves in events.db for every downstream consumer (standup/ask/retro).
Bot-ack/status replies are dropped — keeps the signal, not the noise.

Parents MUST come from the Slack API, not events.db: full-mode ingest skips
bot messages (keep_bot_messages=false on all no_threads channels), so alert
parents never land in the DB — a DB-sourced parent query matches nothing.

Cheap because: bounded to recent alert threads, runs 1×/day not 48×.

Usage:
    python derive/slack_alert_thread_reconcile.py            # all no_threads channels
    python derive/slack_alert_thread_reconcile.py --days 3
    python derive/slack_alert_thread_reconcile.py --channel example-recon --dry-run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import get_db  # noqa: E402
from ingest.slack_api_client import SlackClient, api_message_to_parsed  # noqa: E402
from derive.slack_upsert import upsert_event  # noqa: E402
from derive.slack_team import (  # noqa: E402
    load_team_slack_ids, load_team_subteam_ids, is_team_involved,
)

CHANNELS_YAML = _REPO_ROOT / "config" / "slack_channels.yaml"
LOOKBACK_DAYS = 2          # parent-alert activity window (slight overlap for safety)
PARENT_CAP = 300           # max alert threads scanned per channel per day


def _no_threads_channels() -> list[tuple[str, str]]:
    cfg = yaml.safe_load(CHANNELS_YAML.read_text())
    out = []
    for c in cfg.get("channels", []):
        if str(c.get("no_threads", "false")).lower() == "true":
            out.append((c["id"], c.get("name", c["id"])))
    return out


def _recent_alert_parents(client: SlackClient, channel_id: str,
                          oldest_epoch: str) -> list[dict]:
    """Thread parents (reply_count > 0) from live channel history since oldest.

    Returns full API message dicts so the parent can be upserted alongside its
    team replies. Includes human-authored parents too — idempotent upsert makes
    re-adding already-ingested ones a no-op, and their replies are otherwise
    just as invisible on no_threads channels.
    """
    parents = []
    for msg in client.iter_history(channel_id, oldest=oldest_epoch):
        ts = msg.get("ts")
        if msg.get("thread_ts") and msg["thread_ts"] != ts:
            continue  # a reply surfaced in history — not a parent
        if not msg.get("reply_count"):
            continue
        parents.append(msg)
        if len(parents) >= PARENT_CAP:
            print(f"  [warn] {channel_id}: PARENT_CAP={PARENT_CAP} hit — "
                  f"older threads in window not scanned", file=sys.stderr)
            break
    return parents


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    ap.add_argument("--channel", help="single channel name/id; default = all no_threads")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    team_ids = set(load_team_slack_ids().keys())
    team_subteam_ids = load_team_subteam_ids()
    if not team_ids:
        print("[err] no team slack_ids resolved", file=sys.stderr)
        return 2

    channels = _no_threads_channels()
    if args.channel:
        channels = [(cid, nm) for cid, nm in channels
                    if args.channel in (cid, nm)]
        if not channels:
            print(f"[err] {args.channel} not a no_threads channel", file=sys.stderr)
            return 2

    print(f"[alert-threads] {len(channels)} no_threads channels · "
          f"team={len(team_ids)} subteams={len(team_subteam_ids)} · "
          f"window={args.days}d dry_run={args.dry_run}", flush=True)

    client = SlackClient()
    users_cache = client.build_users_cache()
    name_resolver = getattr(client, "name_resolver", None)
    subteams_cache = client.build_subteams_cache()

    conn = get_db()
    oldest_epoch = f"{(datetime.now(tz=timezone.utc) - timedelta(days=args.days)).timestamp():.6f}"

    grand_team = grand_scanned = grand_parents = 0
    for cid, name in channels:
        try:
            parents = _recent_alert_parents(client, cid, oldest_epoch)
        except RuntimeError as e:
            print(f"  {name[:32]:32} history ERR {e}", file=sys.stderr)
            continue
        team_kept = scanned = inserted = 0
        for pmsg in parents:
            pts = pmsg.get("ts")
            try:
                team_replies = []
                for msg in client.iter_replies(cid, pts):
                    # iter_replies yields the parent first — skip it (ts == pts)
                    if msg.get("ts") == pts:
                        continue
                    scanned += 1
                    author = msg.get("user") or msg.get("bot_id")
                    text = msg.get("text", "")
                    if is_team_involved(author, text, team_ids, team_subteam_ids):
                        team_replies.append(msg)
                if not team_replies:
                    continue
                if args.dry_run:
                    team_kept += len(team_replies)
                    continue
                # Parent first: the thread root must exist for subject
                # resolution (root = MIN(ts) per slack:<ch>:<thread_ts>).
                ppm = api_message_to_parsed(pmsg, users_cache, name_resolver,
                                            subteams_cache)
                if upsert_event(conn, ppm, cid, thread_parent_ts=None,
                                slack_users_cache=users_cache) == "inserted":
                    inserted += 1
                for msg in team_replies:
                    pm = api_message_to_parsed(msg, users_cache, name_resolver,
                                               subteams_cache)
                    outcome = upsert_event(conn, pm, cid, thread_parent_ts=pts,
                                           slack_users_cache=users_cache)
                    if outcome in ("inserted", "updated"):
                        team_kept += 1
                        inserted += 1
            except RuntimeError as e:
                print(f"  {name[:32]:32} parent {pts} ERR {e}", file=sys.stderr)
        if not args.dry_run:
            conn.commit()
            if inserted:
                # Refresh thread_summary for the touched channel (cheap,
                # idempotent) — mirrors the team_involved ingest path.
                subprocess.run(
                    [str(_REPO_ROOT / ".venv" / "bin" / "python"),
                     "derive/build_thread_summary.py", "--channel", cid],
                    cwd=str(_REPO_ROOT), check=False, capture_output=True)
        grand_team += team_kept
        grand_scanned += scanned
        grand_parents += len(parents)
        if parents:
            print(f"  {name[:34]:34} parents={len(parents):>3} "
                  f"scanned_replies={scanned:>5} team_kept={team_kept:>4}", flush=True)

    print(f"\n[done] {grand_parents} alert threads · {grand_scanned} replies scanned "
          f"· {grand_team} team replies {'(dry)' if args.dry_run else 'upserted'}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

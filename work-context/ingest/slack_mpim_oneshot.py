#!/usr/bin/env python3
"""slack_mpim_oneshot.py — explicit-consent one-shot ingest of an MPIM or
ad-hoc private channel that is NOT in config/slack_channels.yaml.

Bypasses two invariants vs slack_backfill_app.py:
  1. yaml lookup (channel id not required to be configured)
  2. is_mpim hard-skip (PRD §12)

Does NOT bypass:
  - is_im hard-skip (1:1 DMs remain refused)
  - membership requirement (Slack returns not_in_channel otherwise)

Does NOT write a cursor → the regular cron (slack_ingest_app) will not pick
up this channel on next fire. To refresh later, re-run this script.

Usage:
    python -m ingest.slack_mpim_oneshot C0EXAMPLE --confirm-mpim
    python -m ingest.slack_mpim_oneshot C…       --days 30 --confirm-mpim
    python -m ingest.slack_mpim_oneshot C…       --dry-run --confirm-mpim

Owner-invoked. Use sparingly — once ingested, messages from the channel's
named members surface in downstream skills (narrative/rollup/retro) tied to
their profiles.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.slack_api_client import (  # noqa: E402
    SlackClient,
    api_message_to_parsed,
    make_name_resolver,
    _load_env,
    _assert_auth_clean,
)
from ingest.slack_backfill_app import DB_PATH, write_cursor  # noqa: E402
from derive.slack_upsert import upsert_messages  # noqa: E402

LIMIT = 200


def fetch_history(client, cid, oldest, users_cache, name_resolver, subteams_cache, keep_bot):
    msgs: list = []
    cursor = None
    pages = 0
    bot_skipped = 0
    while True:
        page = client.history(cid, oldest=oldest, limit=LIMIT, cursor=cursor)
        for raw in page.get("messages", []):
            pm = api_message_to_parsed(
                raw, users_cache, name_resolver=name_resolver, subteams_cache=subteams_cache,
            )
            if pm.is_bot and not keep_bot:
                bot_skipped += 1
                continue
            msgs.append((pm, raw.get("ts")))
        pages += 1
        cursor = page.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break
    return msgs, pages, bot_skipped


def fetch_threads(client, cid, parents, users_cache, name_resolver, subteams_cache,
                  keep_bot, dry_run):
    """Returns (inserted_total, bot_skipped, errors)."""
    conn = sqlite3.connect(DB_PATH) if not dry_run else None
    inserted_total = 0
    bot_skipped = 0
    errors: list[str] = []
    try:
        for i, pts in enumerate(parents, 1):
            try:
                batch = []
                for raw in client.iter_replies(cid, pts, limit=LIMIT):
                    if raw.get("ts") == pts:
                        continue
                    pm = api_message_to_parsed(
                        raw, users_cache, name_resolver=name_resolver, subteams_cache=subteams_cache,
                    )
                    if pm.is_bot and not keep_bot:
                        bot_skipped += 1
                        continue
                    batch.append(pm)
                if batch:
                    if not dry_run:
                        res = upsert_messages(conn, batch, cid, thread_parent_ts=pts)
                        inserted_total += res.inserted
                    else:
                        inserted_total += len(batch)
            except Exception as e:
                errors.append(f"{pts}: {e}")
            if i % 25 == 0:
                print(f"  threads: {i}/{len(parents)}", flush=True)
    finally:
        if conn:
            conn.close()
    return inserted_total, bot_skipped, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("channel_id", help="Slack channel id (C…); yaml not consulted")
    ap.add_argument("--days", type=int, default=None,
                    help="lookback window in days (default: full history since channel.created)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--confirm-mpim", action="store_true",
                    help="REQUIRED if conversations.info reports is_mpim=true. "
                         "Explicit owner-consent flag; not honoured for is_im.")
    ap.add_argument("--no-threads", action="store_true")
    ap.add_argument("--keep-bot-messages", action="store_true")
    ap.add_argument("--persist-cursor", action="store_true",
                    help="Write newest_ts to state/slack_cursors.json so the regular cron "
                         "(slack_ingest_app) picks up the channel going forward. "
                         "Pair with a yaml row (allow_mpim: true) for full recurring ingest.")
    args = ap.parse_args()

    env = _load_env()
    try:
        token = _assert_auth_clean(env)
    except RuntimeError as e:
        print(f"FAIL auth: {e}", file=sys.stderr)
        return 1

    client = SlackClient(token=token)

    # Probe channel + enforce guards
    try:
        info = client.conversations_info(args.channel_id)
    except RuntimeError as e:
        print(f"FAIL conversations.info({args.channel_id}): {e}", file=sys.stderr)
        return 2
    meta = info.get("channel", {})
    cname = meta.get("name", args.channel_id)
    is_mpim = bool(meta.get("is_mpim"))
    is_im = bool(meta.get("is_im"))
    is_private = bool(meta.get("is_private"))
    is_archived = bool(meta.get("is_archived"))
    created = int(meta.get("created") or 0)

    print(json.dumps({
        "channel_id": args.channel_id, "name": cname,
        "is_mpim": is_mpim, "is_im": is_im, "is_private": is_private,
        "is_archived": is_archived, "created_epoch": created,
        "days": args.days, "dry_run": args.dry_run,
    }, indent=2))

    if is_im:
        print("REFUSE: is_im=true (1:1 DM). Not supported by this script.", file=sys.stderr)
        return 1
    if is_mpim and not args.confirm_mpim:
        print("REFUSE: is_mpim=true. Pass --confirm-mpim to acknowledge.", file=sys.stderr)
        return 1

    # Window
    if args.days:
        oldest_dt = datetime.now(tz=timezone.utc) - timedelta(days=args.days)
        oldest_epoch = f"{oldest_dt.timestamp():.6f}"
    elif created:
        oldest_epoch = f"{created}.000000"
    else:
        oldest_epoch = "0"
    print(f"\noldest_ts: {oldest_epoch}")

    # Caches
    t0 = time.monotonic()
    print("[users] hydrating users.list cache...", flush=True)
    users_cache = client.build_users_cache()
    name_resolver = make_name_resolver(client, users_cache)
    print(f"  cached {len(users_cache)} users in {time.monotonic() - t0:.1f}s")
    subteams_cache = client.build_subteams_cache()
    print(f"[subteams] cached {len(subteams_cache)}")

    # Phase 1 — top-level history
    t1 = time.monotonic()
    msgs, pages, hist_bot = fetch_history(
        client, args.channel_id, oldest_epoch,
        users_cache, name_resolver, subteams_cache,
        args.keep_bot_messages,
    )
    print(f"\n[history] fetched {len(msgs)} msgs across {pages} pages "
          f"(bot_skipped={hist_bot}) in {time.monotonic() - t1:.1f}s")

    top_inserted = 0
    if msgs:
        if not args.dry_run:
            conn = sqlite3.connect(DB_PATH)
            batch = [pm for pm, _ in msgs]
            res = upsert_messages(conn, batch, args.channel_id, thread_parent_ts=None)
            top_inserted = res.inserted
            conn.close()
        else:
            top_inserted = len(msgs)
    print(f"[history] inserted={top_inserted}")

    # Phase 2 — thread replies
    parents = [ts for pm, ts in msgs if pm.reply_count and pm.reply_count > 0]
    print(f"\n[threads] parents-with-replies: {len(parents)}")
    repl_inserted = repl_bot = 0
    repl_errors: list[str] = []
    if parents and not args.no_threads:
        t2 = time.monotonic()
        repl_inserted, repl_bot, repl_errors = fetch_threads(
            client, args.channel_id, parents,
            users_cache, name_resolver, subteams_cache,
            args.keep_bot_messages, args.dry_run,
        )
        print(f"[threads] inserted={repl_inserted} bot_skipped={repl_bot} "
              f"errors={len(repl_errors)} in {time.monotonic() - t2:.1f}s")

    # Newest ts across all fetched top-level msgs (Slack-epoch string).
    newest_ts = max((ts for _, ts in msgs), default=None) if msgs else None

    cursor_written = False
    if args.persist_cursor and newest_ts and not args.dry_run:
        write_cursor(args.channel_id, newest_ts)
        cursor_written = True

    # Final summary
    print("\n--- summary ---")
    print(json.dumps({
        "channel": cname, "id": args.channel_id,
        "top_pages": pages, "top_inserted": top_inserted,
        "replies_inserted": repl_inserted,
        "thread_parents": len(parents),
        "bot_skipped": hist_bot + repl_bot,
        "errors": repl_errors[:10],
        "dry_run": args.dry_run,
        "newest_ts": newest_ts,
        "cursor_written": cursor_written,
    }, indent=2))
    if cursor_written:
        print(f"\nNOTE: cursor written → cron will pick up channel on next fire. "
              f"Add yaml row with allow_mpim: true to make it permanent.")
    else:
        print("\nNOTE: no cursor written. Cron will not auto-ingest this channel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

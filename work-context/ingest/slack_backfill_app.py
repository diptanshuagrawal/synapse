"""
slack_backfill_app.py — direct-API backfill driver.

Replaces the Phase 2b + 2c MCP loop in /slack-backfill with a headless script.
Same DB schema, same upsert path — only fetch transport changes.

Usage:
    python -m ingest.slack_backfill_app <channel-name|channel-id>
        [--days N | --days all] [--dry-run] [--no-threads]
        [--cursor-mode resume|fresh|force]

Modes:
    --dry-run        Fetch + parse only; print row counts. No DB writes.
    --no-threads     Skip Phase 2c (thread replies). Use for fast top-level smoke.
    --cursor-mode    Default `resume`. `fresh` refuses if cursor set.
                     `force` re-fetches full window regardless of cursor.

Exit codes:
    0  success
    1  env / auth / config error
    2  channel not found / not a member
    3  partial — some threads failed, see stderr
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

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
from ingest.common import DB_PATH, atomic_write_json  # noqa: E402
from derive.slack_upsert import upsert_messages  # noqa: E402
from derive.slack_team import load_team_slack_ids, load_team_subteam_ids, is_team_involved  # noqa: E402

CURSORS_PATH = _PKG_ROOT / "state" / "slack_cursors.json"


# ── Channel resolution ────────────────────────────────────────────────────


def _load_channels_yaml() -> list[dict]:
    """Parse config/slack_channels.yaml without a YAML dep (single-file format)."""
    yaml_path = _PKG_ROOT / "config" / "slack_channels.yaml"
    if not yaml_path.exists():
        raise RuntimeError(f"missing {yaml_path}")
    text = yaml_path.read_text()
    # Minimal parser: split on `  - id:` block boundaries.
    channels: list[dict] = []
    current: dict = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - id:"):
            if current:
                channels.append(current)
            current = {"id": line.split(":", 1)[1].strip()}
            continue
        if line.startswith("    "):
            stripped = line.strip()
            if ":" in stripped:
                k, _, v = stripped.partition(":")
                v = v.strip()
                # Strip inline `#` comment (unquoted) — opsgenie row uses these.
                if v and not v.startswith(('"', "'")) and "#" in v:
                    v = v.split("#", 1)[0].strip()
                current[k.strip()] = v.strip('"').strip("'")
    if current:
        channels.append(current)
    return channels


def resolve_channel(spec: str) -> dict:
    """spec: either channel-id (C…) or channel-name. Returns yaml row."""
    rows = _load_channels_yaml()
    if spec.startswith("C") and len(spec) >= 9:
        for r in rows:
            if r.get("id") == spec:
                return r
    for r in rows:
        if r.get("name") == spec:
            return r
    raise RuntimeError(
        f"channel {spec!r} not in config/slack_channels.yaml. "
        f"Known: {[r.get('name') for r in rows]}"
    )


# ── Cursor / window ───────────────────────────────────────────────────────


def _read_cursors() -> dict:
    if not CURSORS_PATH.exists():
        return {}
    try:
        return json.loads(CURSORS_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def _write_cursors(cursors: dict) -> None:
    # Atomic write via tempfile + os.replace so cron-status / concurrent
    # readers never observe a half-written cursors file.
    atomic_write_json(CURSORS_PATH, cursors, sort_keys=True)


def read_cursor(channel_id: str) -> Optional[str]:
    return _read_cursors().get(channel_id)


def write_cursor(channel_id: str, newest_ts: str) -> None:
    cursors = _read_cursors()
    existing = cursors.get(channel_id, "0")
    try:
        existing_val = float(existing) if existing else 0.0
        if float(newest_ts) <= existing_val:
            return  # never go backwards
    except (ValueError, TypeError):
        pass
    cursors[channel_id] = newest_ts
    _write_cursors(cursors)


def compute_oldest_ts(days: str, created_at: Optional[str] = None) -> Optional[str]:
    if days == "all":
        return None  # API treats no oldest as "from start"; or pass created_at if known
    try:
        n = int(days)
    except ValueError:
        raise RuntimeError(f"--days must be 'all' or integer, got {days!r}")
    secs = int(time.time() - n * 86400)
    return str(secs)


# ── Phase 2b: history ─────────────────────────────────────────────────────


def fetch_history(
    client: SlackClient,
    channel_id: str,
    oldest: Optional[str],
    dry_run: bool,
    users_cache: dict[str, str],
    keep_bot_messages: bool,
    name_resolver=None,
    subteams_cache: Optional[dict[str, str]] = None,
    team_slack_ids: Optional[set[str]] = None,
    team_subteam_ids: Optional[set[str]] = None,
    ingest_mode: str = "full",
) -> tuple[int, int, Optional[str], int, int]:
    """Returns (top_inserted, replies_inserted, newest_ts, bot_skipped, dropped).

    `replies_inserted` is non-zero only for ingest_mode='team_involved' (inline
    reply fetch on non-team parents to determine team-involvement). For 'full'
    mode, replies handled by Phase 2c.

    `dropped` is non-zero only for team_involved — parents filtered out.
    """
    team_filter = (ingest_mode == "team_involved")
    if team_filter and not team_slack_ids:
        raise RuntimeError("ingest_mode=team_involved requires team_slack_ids")

    conn = sqlite3.connect(DB_PATH)
    newest_ts: Optional[str] = None
    inserted_total = 0
    replies_inserted = 0
    bot_skipped = 0
    dropped = 0
    deferred_checks = 0
    try:
        if not team_filter:
            # Original full-ingest path.
            batch: list = []
            for msg in client.iter_history(channel_id, oldest=oldest, limit=200):
                ts = msg.get("ts")
                if ts and (newest_ts is None or ts > newest_ts):
                    newest_ts = ts
                pm = api_message_to_parsed(msg, users_cache, name_resolver=name_resolver, subteams_cache=subteams_cache)
                if pm.is_bot and not keep_bot_messages:
                    bot_skipped += 1
                    continue
                batch.append(pm)
                if len(batch) >= 500:
                    if not dry_run:
                        res = upsert_messages(conn, batch, channel_id, thread_parent_ts=None)
                        inserted_total += res.inserted
                    else:
                        inserted_total += len(batch)
                    batch.clear()
            if batch:
                if not dry_run:
                    res = upsert_messages(conn, batch, channel_id, thread_parent_ts=None)
                    inserted_total += res.inserted
                else:
                    inserted_total += len(batch)
        else:
            # team_involved: inline reply-check for non-team parents.
            # Inline upsert in chunks for memory bound (channels can be 10k+ msgs).
            top_batch: list = []
            reply_batches: list[tuple[str, list]] = []
            for msg in client.iter_history(channel_id, oldest=oldest, limit=200):
                ts = msg.get("ts")
                if ts and (newest_ts is None or ts > newest_ts):
                    newest_ts = ts
                pm = api_message_to_parsed(
                    msg, users_cache, name_resolver=name_resolver, subteams_cache=subteams_cache,
                )
                # NOTE: in team_involved mode we defer the bot-skip until AFTER
                # the reply walk. Bot-rooted incident threads (Dweep / PagerDuty
                # / OpsGenie "Alert Incident Commander" headers) need their
                # replies inspected because the team typically triages in the
                # replies, not the bot-authored root. Dropping the root early
                # would orphan a high-signal incident thread.

                root_team_involved = is_team_involved(
                    pm.actor_id, pm.body, team_slack_ids, team_subteam_ids
                )
                if root_team_involved:
                    # Root mentions team directly. Honour keep_bot_messages only
                    # when the channel author opted out AND the body itself
                    # already names the team (rare — usually means the bot template
                    # @-mentions a team member explicitly).
                    if pm.is_bot and not keep_bot_messages:
                        bot_skipped += 1
                        continue
                    top_batch.append(pm)
                    continue

                reply_count = msg.get("reply_count") or 0
                if reply_count <= 0:
                    # No replies + root not team-involved → drop.
                    if pm.is_bot and not keep_bot_messages:
                        bot_skipped += 1
                    else:
                        dropped += 1
                    continue

                # Walk replies; if any team-involved, keep the root (even bot).
                deferred_checks += 1
                try:
                    replies = []
                    team_in_thread = False
                    for raw in client.iter_replies(channel_id, ts, limit=200):
                        if raw.get("ts") == ts:
                            continue
                        rpm = api_message_to_parsed(
                            raw, users_cache, name_resolver=name_resolver,
                            subteams_cache=subteams_cache,
                        )
                        if rpm.is_bot and not keep_bot_messages:
                            bot_skipped += 1
                            continue
                        replies.append(rpm)
                        if is_team_involved(rpm.actor_id, rpm.body, team_slack_ids, team_subteam_ids):
                            team_in_thread = True
                    if team_in_thread:
                        # Keep root regardless of bot flag — it's the incident
                        # header that gives the replies meaning.
                        top_batch.append(pm)
                        reply_batches.append((ts, replies))
                    elif pm.is_bot and not keep_bot_messages:
                        bot_skipped += 1
                    else:
                        dropped += 1
                except RuntimeError:
                    if pm.is_bot and not keep_bot_messages:
                        bot_skipped += 1
                    else:
                        dropped += 1

                # Flush in chunks
                if len(top_batch) >= 200:
                    if not dry_run:
                        res = upsert_messages(conn, top_batch, channel_id, thread_parent_ts=None)
                        inserted_total += res.inserted
                        for pts, replies in reply_batches:
                            if replies:
                                rres = upsert_messages(conn, replies, channel_id, thread_parent_ts=pts)
                                replies_inserted += rres.inserted
                    else:
                        inserted_total += len(top_batch)
                        replies_inserted += sum(len(r) for _, r in reply_batches)
                    top_batch.clear()
                    reply_batches.clear()

            # Final flush
            if top_batch:
                if not dry_run:
                    res = upsert_messages(conn, top_batch, channel_id, thread_parent_ts=None)
                    inserted_total += res.inserted
                    for pts, replies in reply_batches:
                        if replies:
                            rres = upsert_messages(conn, replies, channel_id, thread_parent_ts=pts)
                            replies_inserted += rres.inserted
                else:
                    inserted_total += len(top_batch)
                    replies_inserted += sum(len(r) for _, r in reply_batches)

            print(f"  [team-filter] deferred_checks={deferred_checks} dropped={dropped}",
                  flush=True)
    finally:
        conn.close()
    return inserted_total, replies_inserted, newest_ts, bot_skipped, dropped


# ── Phase 2c: thread replies ──────────────────────────────────────────────


def pending_thread_parents(channel_id: str) -> list[str]:
    """Slack-epoch parent_ts for thread parents missing replies in DB.

    Delegates to derive/slack_backfill_helper.py to share SQL logic (the
    helper handles the ISO→epoch ts conversion that events.ts/events.thread_ts
    require — see helper docstring). Keeps one source of truth.
    """
    from subprocess import run, PIPE
    r = run(
        [".venv/bin/python", "derive/slack_backfill_helper.py",
         "pending-threads", channel_id],
        cwd=str(_PKG_ROOT), stdout=PIPE, stderr=PIPE, text=True, check=True,
    )
    return [line for line in r.stdout.splitlines() if line.strip()]


def stale_thread_parents(channel_id: str) -> list[str]:
    """Slack-epoch parent_ts where stored reply_count > replies actually ingested."""
    from subprocess import run, PIPE
    r = run(
        [".venv/bin/python", "derive/slack_backfill_helper.py",
         "stale-threads", channel_id],
        cwd=str(_PKG_ROOT), stdout=PIPE, stderr=PIPE, text=True, check=True,
    )
    return [line for line in r.stdout.splitlines() if line.strip()]


def active_thread_parents(channel_id: str, days: int = 90,
                          ignore_cooldown: bool = False) -> list[str]:
    """Slack-epoch parent_ts for threads whose newest reply is within `days`.

    Re-drains late replies to old threads that the cursor-bound incremental
    history + 24h reconcile both miss (parent scrolled below the cursor). The
    helper's drain cooldown throttles each thread to ~once/day; pass
    ignore_cooldown=True (pre-standup sweep) to bypass it so a previous-evening
    reply lands before the morning digest."""
    from subprocess import run, PIPE
    cmd = [".venv/bin/python", "derive/slack_backfill_helper.py",
           "active-threads", channel_id, "--days", str(days)]
    if ignore_cooldown:
        cmd.append("--ignore-cooldown")
    r = run(cmd, cwd=str(_PKG_ROOT), stdout=PIPE, stderr=PIPE, text=True, check=True)
    return [line for line in r.stdout.splitlines() if line.strip()]


def fetch_threads(
    client: SlackClient,
    channel_id: str,
    parents: list[str],
    dry_run: bool,
    users_cache: dict[str, str],
    keep_bot_messages: bool,
    name_resolver=None,
    subteams_cache: Optional[dict[str, str]] = None,
) -> tuple[int, int, list[str]]:
    """Returns (replies_count, bot_skipped, errors)."""
    conn = sqlite3.connect(DB_PATH)
    errors: list[str] = []
    total = 0
    bot_skipped = 0
    try:
        for i, parent_ts in enumerate(parents, 1):
            try:
                batch = []
                for msg in client.iter_replies(channel_id, parent_ts, limit=200):
                    if msg.get("ts") == parent_ts:
                        continue
                    pm = api_message_to_parsed(msg, users_cache, name_resolver=name_resolver, subteams_cache=subteams_cache)
                    if pm.is_bot and not keep_bot_messages:
                        bot_skipped += 1
                        continue
                    batch.append(pm)
                if batch:
                    if not dry_run:
                        res = upsert_messages(conn, batch, channel_id, thread_parent_ts=parent_ts)
                        total += res.inserted
                    else:
                        total += len(batch)
            except Exception as e:
                errors.append(f"{parent_ts}: {e}")
            if i % 50 == 0:
                print(f"  threads: {i}/{len(parents)}", flush=True)
    finally:
        conn.close()
    return total, bot_skipped, errors


# ── Users cache ───────────────────────────────────────────────────────────


def hydrate_users_cache(client: SlackClient, user_ids: set[str]) -> dict[str, str]:
    """Cheap U… → display_name map. Misses fall back to user-<id>."""
    cache: dict[str, str] = {}
    for uid in user_ids:
        if not uid or not uid.startswith("U"):
            continue
        try:
            r = client.users_info(uid)
            u = r.get("user", {})
            cache[uid] = u.get("profile", {}).get("display_name") or u.get("real_name") or uid
        except Exception:
            cache[uid] = uid
    return cache


# ── Main ──────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("channel", help="channel name or id")
    ap.add_argument("--days", default="365", help="window in days, or 'all'")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-threads", action="store_true")
    ap.add_argument(
        "--cursor-mode",
        choices=("resume", "fresh", "force"),
        default="resume",
    )
    args = ap.parse_args()

    env = _load_env()
    try:
        token = _assert_auth_clean(env)
    except RuntimeError as e:
        print(f"FAIL auth: {e}", file=sys.stderr)
        return 1

    try:
        ch = resolve_channel(args.channel)
    except RuntimeError as e:
        print(f"FAIL channel resolve: {e}", file=sys.stderr)
        return 2

    cid = ch["id"]
    cname = ch.get("name", cid)
    client = SlackClient(token=token)

    # Membership / DM hard-skip
    allow_mpim = str(ch.get("allow_mpim", "false")).lower() == "true"
    try:
        info = client.conversations_info(cid)
        meta = info.get("channel", {})
        if meta.get("is_im"):
            print(f"SKIP {cname}: 1:1 DM hard-skip (is_im)", file=sys.stderr)
            return 2
        if meta.get("is_mpim") and not allow_mpim:
            print(f"SKIP {cname}: MPIM hard-skip — set allow_mpim: true in yaml to override",
                  file=sys.stderr)
            return 2
    except RuntimeError as e:
        print(f"FAIL conversations.info({cid}): {e}", file=sys.stderr)
        return 2

    # Cursor mode
    existing_cursor = read_cursor(cid)
    if existing_cursor and args.cursor_mode == "fresh":
        print(
            f"REFUSE {cname}: cursor already set ({existing_cursor}). "
            f"Use --cursor-mode resume or force.",
            file=sys.stderr,
        )
        return 1

    oldest = compute_oldest_ts(args.days)
    keep_bot_messages = ch.get("keep_bot_messages", "false").lower() == "true"
    ingest_mode = str(ch.get("ingest_mode", "full")).lower()
    team_slack_ids: Optional[set[str]] = None
    team_subteam_ids: Optional[set[str]] = None
    if ingest_mode == "team_involved":
        team_slack_ids = set(load_team_slack_ids().keys())
        if not team_slack_ids:
            print(f"FAIL {cname}: ingest_mode=team_involved but no team slack-ids "
                  "resolved from team.md", file=sys.stderr)
            return 1
        team_subteam_ids = load_team_subteam_ids()
    print(json.dumps({
        "channel": cname, "id": cid, "days": args.days, "oldest_ts": oldest,
        "cursor_mode": args.cursor_mode, "existing_cursor": existing_cursor,
        "dry_run": args.dry_run, "keep_bot_messages": keep_bot_messages,
        "ingest_mode": ingest_mode,
        "team_slack_ids": len(team_slack_ids) if team_slack_ids else 0,
        "team_subteam_ids": len(team_subteam_ids) if team_subteam_ids else 0,
    }, indent=2))

    # Hydrate user cache once (matches MCP-text display names).
    t0 = time.monotonic()
    print("\n[users] hydrating users.list cache...", flush=True)
    users_cache = client.build_users_cache()
    name_resolver = make_name_resolver(client, users_cache)
    print(f"  cached {len(users_cache)} users in {time.monotonic()-t0:.1f}s")

    # Subteams cache (handles `<!subteam^S…>` expansion). Optional — 403 ok.
    t0 = time.monotonic()
    subteams_cache = client.build_subteams_cache()
    print(f"[subteams] cached {len(subteams_cache)} in {time.monotonic()-t0:.1f}s "
          f"{'(usergroups:read scope absent — skipping expansion)' if not subteams_cache else ''}")

    # Phase 2b: history
    t0 = time.monotonic()
    print(f"\n[2b] fetch history (oldest={oldest}, ingest_mode={ingest_mode})...", flush=True)
    top_count, repl_inline, newest_ts, hist_bot_skipped, dropped = fetch_history(
        client, cid, oldest, args.dry_run, users_cache, keep_bot_messages,
        name_resolver=name_resolver, subteams_cache=subteams_cache,
        team_slack_ids=team_slack_ids, team_subteam_ids=team_subteam_ids,
        ingest_mode=ingest_mode,
    )
    print(f"  top-level fetched: {top_count}, newest_ts={newest_ts}, "
          f"bot_skipped={hist_bot_skipped}, dropped={dropped}, "
          f"replies_inline={repl_inline}")
    print(f"  elapsed: {time.monotonic()-t0:.1f}s")

    if args.no_threads:
        print("\n[2c] skipped (--no-threads)")
        if newest_ts and not args.dry_run and args.cursor_mode != "fresh":
            write_cursor(cid, newest_ts)
            print(f"  cursor advanced to {newest_ts}")
        return 0

    # Phase 2c: threads
    print("\n[2c] derive pending thread parents...")
    pending = pending_thread_parents(cid)
    stale = stale_thread_parents(cid)
    parents = sorted(set(pending) | set(stale))
    print(f"  pending: {len(pending)}  stale: {len(stale)}  union: {len(parents)}")
    if parents:
        replies_count, thread_bot_skipped, errors = fetch_threads(
            client, cid, parents, args.dry_run, users_cache, keep_bot_messages,
            name_resolver=name_resolver, subteams_cache=subteams_cache,
        )
        print(f"  replies fetched: {replies_count}, bot_skipped: {thread_bot_skipped}, errors: {len(errors)}")
        for e in errors[:10]:
            print(f"    err: {e}", file=sys.stderr)
    else:
        print("  none — channel is clean")

    # Build thread_summary
    if not args.dry_run:
        print("\n[2e] build_thread_summary...")
        from subprocess import run
        run([
            ".venv/bin/python",
            "derive/build_thread_summary.py",
            "--channel", cid,
        ], cwd=str(_PKG_ROOT), check=False)

    # Cursor advance
    if newest_ts and not args.dry_run and args.cursor_mode != "fresh":
        write_cursor(cid, newest_ts)
        print(f"\ncursor advanced to {newest_ts}")

    print(f"\nelapsed total: {time.monotonic()-t0:.1f}s")
    return 0 if not parents else (0 if not args.dry_run else 0)


if __name__ == "__main__":
    sys.exit(main())

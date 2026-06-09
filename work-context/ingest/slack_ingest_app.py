"""
slack_ingest_app.py — steady-state Slack ingest via direct Web API.

Replaces /slack-ingest skill's MCP fetch loop. Reads every channel in
config/slack_channels.yaml since its last cursor, writes to events.db,
runs stale-thread reconcile, refreshes thread_summary, advances cursors,
records success.

Differences vs slack_backfill_app:
  - Cursor-bound (oldest = stored cursor_ts), no `--days` window
  - Pagination cap: 10 pages/channel/fire (~2000 msgs); spillover next fire
  - Stale-thread reconcile cap: 50/channel/fire
  - Loops all channels by default; single-channel via positional arg
  - Skips channels with null cursor (recommends /slack-backfill seed first)
  - Writes state/last_slack_success.date on any successful channel

Usage:
    python -m ingest.slack_ingest_app                  # all channels
    python -m ingest.slack_ingest_app service-c-public # one channel
    python -m ingest.slack_ingest_app --dry-run        # fetch+parse, no write

Exit codes:
    0  success (≥1 channel ingested OR all up-to-date)
    1  env/auth/config error
    2  no channel succeeded (all errored)
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
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
from ingest.slack_backfill_app import (  # noqa: E402
    _load_channels_yaml,
    resolve_channel,
    read_cursor,
    write_cursor,
    pending_thread_parents,
    stale_thread_parents,
    active_thread_parents,
    DB_PATH,
)
from derive.slack_upsert import upsert_messages, reconcile_window, upsert_event  # noqa: E402
from derive.slack_backfill_helper import _clamp_parent_after_drain  # noqa: E402
from derive.slack_team import load_team_slack_ids, load_team_subteam_ids, is_team_involved  # noqa: E402

SUCCESS_PATH = _PKG_ROOT / "state" / "last_slack_success.date"
# Per-channel "last successfully polled" timestamps. Distinguishes a quiet
# channel (old cursor, freshly checked → up-to-date) from a lagging one
# (old cursor, stale check → not being polled). Consumed by cron-status +
# dashboard to compute lag = now − checked_ts.
CHECKED_PATH = _PKG_ROOT / "state" / "slack_channel_checked.json"
PAGE_CAP = 10               # max pages per channel per fire
STALE_CAP = 50              # max stale-thread fetches per channel per fire
LIMIT = 200                 # API page size
RECONCILE_LOOKBACK_HOURS = 24    # trailing-window edit/delete reconcile
RECONCILE_PAGE_CAP = 10          # ~2000 msgs/24h — enough headroom for alert channels
RECONCILE_THREADS_CAP = 25       # max thread-reply edit-check fetches per fire
TEAM_REPLY_CHECK_CAP = 40        # max non-team parents whose replies we fetch per fire to check team involvement
BOOTSTRAP_LOOKBACK_DAYS = 365    # if channel has null cursor, start fetching from now-N days
ACTIVE_THREAD_DAYS = 90          # re-drain threads with a reply in the last N days (catches late replies to old parents)


# Team-involvement check (for ingest_mode: team_involved channels) — moved
# to derive/slack_team.is_team_involved (shared with backfill_app).


# ── Per-channel fetch ────────────────────────────────────────────────────


def fetch_history_capped(
    client: SlackClient,
    channel_id: str,
    oldest: str,
    dry_run: bool,
    users_cache: dict[str, str],
    keep_bot_messages: bool,
    name_resolver,
    subteams_cache: dict[str, str],
) -> tuple[int, Optional[str], int, bool]:
    """Returns (inserted, newest_ts, bot_skipped, hit_cap).

    hit_cap = True if we stopped at PAGE_CAP with more cursor remaining.
    """
    conn = sqlite3.connect(DB_PATH)
    inserted_total = 0
    bot_skipped = 0
    newest_ts: Optional[str] = None
    cursor: Optional[str] = None
    pages = 0
    hit_cap = False
    try:
        batch = []
        while True:
            page = client.history(channel_id, oldest=oldest, limit=LIMIT, cursor=cursor)
            for msg in page.get("messages", []):
                ts = msg.get("ts")
                if ts and (newest_ts is None or ts > newest_ts):
                    newest_ts = ts
                pm = api_message_to_parsed(msg, users_cache, name_resolver=name_resolver, subteams_cache=subteams_cache)
                if pm.is_bot and not keep_bot_messages:
                    bot_skipped += 1
                    continue
                batch.append(pm)
            pages += 1
            cursor = page.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
            if pages >= PAGE_CAP:
                hit_cap = True
                break
        if batch:
            if not dry_run:
                res = upsert_messages(conn, batch, channel_id, thread_parent_ts=None)
                inserted_total = res.inserted
            else:
                inserted_total = len(batch)
    finally:
        conn.close()
    return inserted_total, newest_ts, bot_skipped, hit_cap


def fetch_history_team_filtered(
    client: SlackClient,
    channel_id: str,
    oldest: str,
    dry_run: bool,
    users_cache: dict[str, str],
    keep_bot_messages: bool,
    name_resolver,
    subteams_cache: dict[str, str],
    team_slack_ids: set[str],
    team_subteam_ids: Optional[set[str]] = None,
) -> tuple[int, int, Optional[str], int, int, int, bool]:
    """Cursor-bound history fetch, FILTERED to threads where team participates.

    Decision per top-level msg:
      • Parent author in team OR parent body mentions team → KEEP (upsert)
      • Otherwise + reply_count > 0 + budget available → fetch replies, check.
        If any reply by team OR body mentions team → KEEP parent + replies.
        Else → DROP (don't upsert anything).
      • Otherwise + reply_count == 0 → DROP.

    Inline-fetches replies for tentative parents; skips Phase 2.5/2.7 for
    team_involved channels (replies handled here).

    Returns (top_inserted, replies_inserted, newest_ts, bot_skipped,
             dropped_parents, deferred_checks, hit_cap).
    """
    conn = sqlite3.connect(DB_PATH)
    inserted_total = 0
    replies_inserted = 0
    bot_skipped = 0
    dropped = 0
    deferred_checks = 0
    newest_ts: Optional[str] = None
    cursor: Optional[str] = None
    pages = 0
    hit_cap = False

    try:
        keep_top: list = []
        keep_replies: list[tuple[str, list]] = []
        while True:
            page = client.history(channel_id, oldest=oldest, limit=LIMIT, cursor=cursor)
            for msg in page.get("messages", []):
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
                # replies, not the bot-authored root.

                root_team_involved = is_team_involved(
                    pm.actor_id, pm.body, team_slack_ids, team_subteam_ids
                )
                if root_team_involved:
                    if pm.is_bot and not keep_bot_messages:
                        bot_skipped += 1
                        continue
                    keep_top.append(pm)
                    continue

                reply_count = msg.get("reply_count") or 0
                if reply_count <= 0 or deferred_checks >= TEAM_REPLY_CHECK_CAP:
                    if pm.is_bot and not keep_bot_messages:
                        bot_skipped += 1
                    else:
                        dropped += 1
                    continue

                deferred_checks += 1
                try:
                    replies = []
                    team_in_thread = False
                    for raw in client.iter_replies(channel_id, ts, limit=LIMIT):
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
                        keep_top.append(pm)
                        keep_replies.append((ts, replies))
                    elif pm.is_bot and not keep_bot_messages:
                        bot_skipped += 1
                    else:
                        dropped += 1
                except RuntimeError:
                    # If replies fetch fails, default to drop (conservative).
                    if pm.is_bot and not keep_bot_messages:
                        bot_skipped += 1
                    else:
                        dropped += 1
            pages += 1
            cursor = page.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
            if pages >= PAGE_CAP:
                hit_cap = True
                break

        if not dry_run:
            if keep_top:
                res = upsert_messages(conn, keep_top, channel_id, thread_parent_ts=None)
                inserted_total = res.inserted
            for parent_ts, replies in keep_replies:
                if replies:
                    res = upsert_messages(conn, replies, channel_id, thread_parent_ts=parent_ts)
                    replies_inserted += res.inserted
        else:
            inserted_total = len(keep_top)
            replies_inserted = sum(len(r) for _, r in keep_replies)
    finally:
        conn.close()

    return (inserted_total, replies_inserted, newest_ts, bot_skipped,
            dropped, deferred_checks, hit_cap)


def fetch_threads_capped(
    client: SlackClient,
    channel_id: str,
    parents: list[str],
    dry_run: bool,
    users_cache: dict[str, str],
    keep_bot_messages: bool,
    name_resolver,
    subteams_cache: dict[str, str],
) -> tuple[int, int, list[str], bool]:
    """Returns (replies, bot_skipped, errors, hit_cap).

    Caps at STALE_CAP parents; remainder deferred to next fire.
    """
    conn = sqlite3.connect(DB_PATH)
    total = 0
    bot_skipped = 0
    errors: list[str] = []
    truncated = parents[:STALE_CAP]
    hit_cap = len(parents) > STALE_CAP
    try:
        for parent_ts in truncated:
            try:
                batch = []
                for msg in client.iter_replies(channel_id, parent_ts, limit=LIMIT):
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
                # Stamp drain_attempted_at + re-clamp reply_count so the stale
                # detector stays accurate AND the active-thread cooldown engages
                # (throttles each thread to ~once/day). Runs whether or not new
                # replies landed — we attempted a drain.
                if not dry_run:
                    _clamp_parent_after_drain(conn, channel_id, parent_ts)
            except Exception as e:
                errors.append(f"{parent_ts}: {e}")
    finally:
        conn.close()
    return total, bot_skipped, errors, hit_cap


# ── Trailing-window reconcile (edits + deletes) ──────────────────────────


def reconcile_window_capped(
    client: SlackClient,
    channel_id: str,
    dry_run: bool,
    users_cache: dict[str, str],
    keep_bot_messages: bool,
    name_resolver,
    subteams_cache: dict[str, str],
) -> dict:
    """Fetch last RECONCILE_LOOKBACK_HOURS via API, reconcile against DB.

    Two sub-phases:
      (a) top-level: conversations.history in window → upsert (catches edits)
          + tombstone (rows in DB-window not in API set = deleted)
      (b) thread replies: for parents in window with reply_count > 0,
          iter_replies → upsert (catches reply body-edits that Phase 2.5
          stale-thread pass would miss because that pass only fires when
          declared_reply_count > replies_in_db, not on body-only edits).
          Capped at RECONCILE_THREADS_CAP most-recent parents per fire.

    If pagination cap is hit (a), narrow tombstone window to MIN(fetched ts)
    so we never falsely-tombstone rows we just didn't reach. Reply-edit pass
    has no tombstone phase — reply deletes still handled by Phase 2.5 drift.

    Returns {edits, deletes, late_inserts, thread_edits, pages, hit_cap, errors}.
    """
    oldest_dt = datetime.now(tz=timezone.utc) - timedelta(hours=RECONCILE_LOOKBACK_HOURS)
    oldest_epoch = f"{oldest_dt.timestamp():.6f}"

    api_msgs = []
    api_ts_set: set[str] = set()
    oldest_fetched_epoch: Optional[str] = None
    cursor: Optional[str] = None
    pages = 0
    hit_cap = False
    errors: list[str] = []

    try:
        while True:
            page = client.history(channel_id, oldest=oldest_epoch, limit=LIMIT, cursor=cursor)
            for msg in page.get("messages", []):
                ts = msg.get("ts")
                if not ts:
                    continue
                if oldest_fetched_epoch is None or ts < oldest_fetched_epoch:
                    oldest_fetched_epoch = ts
                api_ts_set.add(ts)
                pm = api_message_to_parsed(
                    msg, users_cache, name_resolver=name_resolver, subteams_cache=subteams_cache,
                )
                if pm.is_bot and not keep_bot_messages:
                    continue
                api_msgs.append(pm)
            pages += 1
            cursor = page.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
            if pages >= RECONCILE_PAGE_CAP:
                hit_cap = True
                break
    except RuntimeError as e:
        errors.append(f"history: {e}")
        return {"edits": 0, "deletes": 0, "late_inserts": 0,
                "pages": pages, "hit_cap": hit_cap, "errors": errors}

    if dry_run or not api_msgs:
        return {"edits": 0, "deletes": 0, "late_inserts": 0, "thread_edits": 0,
                "pages": pages, "hit_cap": hit_cap, "errors": errors, "dry_run": dry_run}

    # When cap hit, only reconcile what we actually fetched — narrow window to
    # oldest fetched ts so we don't tombstone rows older than the API gave us.
    window_start_epoch = oldest_fetched_epoch if hit_cap else oldest_epoch
    window_start_iso = (
        datetime.fromtimestamp(float(window_start_epoch), tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )

    conn = sqlite3.connect(DB_PATH)
    thread_edits = 0
    try:
        counts = reconcile_window(
            conn, channel_id, window_start_iso, api_msgs,
            thread_parent_ts_map=None,
            slack_users_cache=users_cache,
        )

        # ── Phase 2.7b: reply-edit reconcile for parents active in window ──
        # Pull parents that exist in DB within window AND have replies.
        # Cap at RECONCILE_THREADS_CAP most-recent so opsgenie-style churn
        # doesn't blow up the fire.
        parents = conn.execute(
            """SELECT ts FROM events
               WHERE source='slack' AND channel_id=?
                 AND event_type='thread_started'
                 AND ts >= ? AND reply_count > 0
               ORDER BY ts DESC LIMIT ?""",
            (channel_id, window_start_iso, RECONCILE_THREADS_CAP),
        ).fetchall()

        for (parent_iso,) in parents:
            try:
                parent_dt = datetime.fromisoformat(parent_iso.replace("Z", "+00:00"))
                parent_epoch = f"{parent_dt.timestamp():.6f}"
            except ValueError:
                continue
            try:
                with conn:
                    for raw in client.iter_replies(channel_id, parent_epoch, limit=LIMIT):
                        if raw.get("ts") == parent_epoch:
                            continue
                        pm = api_message_to_parsed(
                            raw, users_cache, name_resolver=name_resolver,
                            subteams_cache=subteams_cache,
                        )
                        if pm.is_bot and not keep_bot_messages:
                            continue
                        outcome = upsert_event(
                            conn, pm, channel_id, parent_epoch, users_cache,
                        )
                        if outcome == "updated":
                            thread_edits += 1
            except Exception as e:
                errors.append(f"thread-edit {parent_epoch}: {e}")
    finally:
        conn.close()

    return {
        "edits": counts.get("updated", 0),
        "deletes": counts.get("tombstoned", 0),
        "late_inserts": counts.get("inserted", 0),
        "thread_edits": thread_edits,
        "pages": pages,
        "hit_cap": hit_cap,
        "errors": errors + counts.get("errors", []),
    }


# ── Per-channel orchestration ────────────────────────────────────────────


def ingest_channel(
    client: SlackClient,
    ch: dict,
    dry_run: bool,
    users_cache: dict[str, str],
    name_resolver,
    subteams_cache: dict[str, str],
    team_slack_ids: Optional[set[str]] = None,
    team_subteam_ids: Optional[set[str]] = None,
) -> dict:
    """One channel's full ingest pass. Returns summary dict."""
    cid = ch["id"]
    cname = ch.get("name", cid)
    keep_bot_messages = str(ch.get("keep_bot_messages", "false")).lower() == "true"
    allow_mpim = str(ch.get("allow_mpim", "false")).lower() == "true"
    ingest_mode = str(ch.get("ingest_mode", "full")).lower()
    # no_threads: skip the per-fire stale-thread reply reconcile (Phase 2.5).
    # For bot alert firehoses the reply threads are acks/status noise, and
    # re-fetching hundreds of them every fire dominates wall-time. Top-level
    # alerts + edit/delete reconcile still run. Set per channel in yaml.
    no_threads = str(ch.get("no_threads", "false")).lower() == "true"

    existing_cursor = read_cursor(cid)
    summary = {
        "channel": cname, "id": cid, "cursor_in": existing_cursor,
        "skipped": False, "errors": [], "dry_run": dry_run,
    }

    # Auto-bootstrap: channels without a cursor get a synthetic oldest =
    # now - BOOTSTRAP_LOOKBACK_DAYS. Multi-fire catch-up under PAGE_CAP.
    if not existing_cursor:
        bootstrap_dt = datetime.now(tz=timezone.utc) - timedelta(days=BOOTSTRAP_LOOKBACK_DAYS)
        existing_cursor = f"{bootstrap_dt.timestamp():.6f}"
        summary["bootstrap"] = True
        summary["bootstrap_oldest_ts"] = existing_cursor
        summary["bootstrap_days"] = BOOTSTRAP_LOOKBACK_DAYS

    # Membership / DM hard-skip
    try:
        info = client.conversations_info(cid)
        meta = info.get("channel", {})
        if meta.get("is_im"):
            summary["skipped"] = True
            summary["reason"] = "1:1 DM hard-skip (is_im)"
            return summary
        if meta.get("is_mpim") and not allow_mpim:
            summary["skipped"] = True
            summary["reason"] = "MPIM hard-skip — set allow_mpim: true in yaml to override"
            return summary
    except RuntimeError as e:
        summary["errors"].append(f"conversations.info: {e}")
        return summary

    summary["ingest_mode"] = ingest_mode

    if ingest_mode == "team_involved":
        if not team_slack_ids:
            summary["errors"].append("ingest_mode=team_involved but team_slack_ids empty")
            return summary
        # Phase 2b — team-filtered top-level history (inline reply-check for
        # non-team parents). Team-authored parents are upserted bare; their
        # replies are fetched by Phase 2.5 stale-thread reconcile below.
        (inserted, repl_inserted_inline, newest_ts, hist_bot,
         dropped, deferred, hit_hist_cap) = fetch_history_team_filtered(
            client, cid, existing_cursor, dry_run, users_cache, keep_bot_messages,
            name_resolver, subteams_cache, team_slack_ids, team_subteam_ids,
        )
        summary.update({
            "top_inserted": inserted, "newest_ts": newest_ts,
            "hist_bot_skipped": hist_bot, "hist_hit_cap": hit_hist_cap,
            "team_filter_dropped_parents": dropped,
            "team_filter_deferred_checks": deferred,
        })

        # Phase 2.5 — stale-thread reconcile (fetches replies for any DB parent
        # with declared reply_count > replies-in-db). Safely operates only on
        # parents that passed the team filter (others aren't in DB).
        pending = pending_thread_parents(cid)
        stale = stale_thread_parents(cid)
        active = active_thread_parents(cid, ACTIVE_THREAD_DAYS)
        parents = sorted(set(pending) | set(stale) | set(active))
        repl_late, thread_bot, thread_errs, thread_hit_cap = fetch_threads_capped(
            client, cid, parents, dry_run, users_cache, keep_bot_messages,
            name_resolver, subteams_cache,
        )
        summary.update({
            "thread_parents_seen": len(parents),
            "thread_parents_fetched": min(len(parents), STALE_CAP),
            "replies_inserted": repl_inserted_inline + repl_late,
            "thread_bot_skipped": thread_bot,
            "thread_hit_cap": thread_hit_cap,
            # Phase 2.7 skipped — reconcile_window's upsert phase would re-add
            # non-team parents we just filtered out. Trade: no top-level
            # edit/delete tracking on team_involved channels.
            "rec_edits": 0, "rec_deletes": 0, "rec_late_inserts": 0,
            "rec_thread_edits": 0, "rec_pages": 0, "rec_hit_cap": False,
        })
        summary["errors"].extend(thread_errs)

        # Phase 3 — refresh thread_summary for affected threads (cheap, idempotent).
        if not dry_run and (inserted > 0 or repl_inserted_inline > 0 or repl_late > 0):
            from subprocess import run
            run([".venv/bin/python", "derive/build_thread_summary.py", "--channel", cid],
                cwd=str(_PKG_ROOT), check=False, capture_output=True)

        if newest_ts and not dry_run:
            write_cursor(cid, newest_ts)
            summary["cursor_out"] = newest_ts
        return summary

    # ── ingest_mode = "full" (default) — original three-phase flow ──

    # Phase 2b — top-level since cursor
    inserted, newest_ts, hist_bot, hit_hist_cap = fetch_history_capped(
        client, cid, existing_cursor, dry_run, users_cache, keep_bot_messages,
        name_resolver, subteams_cache,
    )
    summary.update({
        "top_inserted": inserted, "newest_ts": newest_ts,
        "hist_bot_skipped": hist_bot, "hist_hit_cap": hit_hist_cap,
    })

    # Phase 2.5 — stale-thread reconcile (skipped when no_threads set)
    if no_threads:
        repl_inserted = 0
        summary.update({
            "thread_parents_seen": 0, "thread_parents_fetched": 0,
            "replies_inserted": 0, "thread_bot_skipped": 0,
            "thread_hit_cap": False, "no_threads": True,
        })
    else:
        pending = pending_thread_parents(cid)
        stale = stale_thread_parents(cid)
        active = active_thread_parents(cid, ACTIVE_THREAD_DAYS)
        parents = sorted(set(pending) | set(stale) | set(active))
        repl_inserted, thread_bot, thread_errs, thread_hit_cap = fetch_threads_capped(
            client, cid, parents, dry_run, users_cache, keep_bot_messages,
            name_resolver, subteams_cache,
        )
        summary.update({
            "thread_parents_seen": len(parents),
            "thread_parents_fetched": min(len(parents), STALE_CAP),
            "replies_inserted": repl_inserted, "thread_bot_skipped": thread_bot,
            "thread_hit_cap": thread_hit_cap,
        })
        summary["errors"].extend(thread_errs)

    # Phase 2.7 — trailing-window reconcile (edits + tombstones, top-level only)
    rec = reconcile_window_capped(
        client, cid, dry_run, users_cache, keep_bot_messages,
        name_resolver, subteams_cache,
    )
    summary.update({
        "rec_edits": rec["edits"],
        "rec_deletes": rec["deletes"],
        "rec_late_inserts": rec["late_inserts"],
        "rec_thread_edits": rec.get("thread_edits", 0),
        "rec_pages": rec["pages"],
        "rec_hit_cap": rec["hit_cap"],
    })
    summary["errors"].extend(rec["errors"])

    # Phase 3 — refresh thread_summary (idempotent; cheap)
    if not dry_run and (inserted > 0 or repl_inserted > 0 or rec["edits"] > 0):
        from subprocess import run
        run([".venv/bin/python", "derive/build_thread_summary.py", "--channel", cid],
            cwd=str(_PKG_ROOT), check=False, capture_output=True)

    # Advance cursor to newest_ts (only if we got messages back)
    if newest_ts and not dry_run:
        write_cursor(cid, newest_ts)
        summary["cursor_out"] = newest_ts

    return summary


def write_success_marker() -> None:
    SUCCESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUCCESS_PATH.write_text(date.today().isoformat() + "\n")


def write_checked_marks(checked_ids: list[str]) -> None:
    """Merge {channel_id: now_iso} into state/slack_channel_checked.json.

    Records every channel that was actually polled this fire (not skipped),
    regardless of whether new messages were found. Merge-preserves channels
    not in this fire's set (single-channel runs don't wipe others).
    """
    if not checked_ids:
        return
    CHECKED_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if CHECKED_PATH.exists():
        try:
            existing = json.loads(CHECKED_PATH.read_text())
        except Exception:
            existing = {}
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    for cid in checked_ids:
        existing[cid] = now_iso
    tmp = CHECKED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2))
    tmp.replace(CHECKED_PATH)


# ── Main ────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("channel", nargs="?", help="optional single channel name/id; default = all")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # Sentinel logging — parsed by bin/cron-status.sh (parse_runs).
    # Matches jira/github/confluence format: "<ts> INFO <msg>".
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    log = logging.getLogger("slack-ingest")

    env = _load_env()
    try:
        token = _assert_auth_clean(env)
    except RuntimeError as e:
        print(f"FAIL auth: {e}", file=sys.stderr)
        return 1

    client = SlackClient(token=token)

    if args.channel:
        try:
            channels = [resolve_channel(args.channel)]
        except RuntimeError as e:
            print(f"FAIL channel: {e}", file=sys.stderr)
            return 1
    else:
        channels = _load_channels_yaml()

    # Caches once for entire fire
    t0 = time.monotonic()
    print(f"[users] hydrating users.list cache...", flush=True)
    users_cache = client.build_users_cache()
    name_resolver = make_name_resolver(client, users_cache)
    print(f"  cached {len(users_cache)} users in {time.monotonic()-t0:.1f}s")
    subteams_cache = client.build_subteams_cache()
    print(f"[subteams] cached {len(subteams_cache)}")

    team_slack_ids = set(load_team_slack_ids().keys())
    print(f"[team] {len(team_slack_ids)} team slack-ids loaded (from team.md)")
    team_subteam_ids = load_team_subteam_ids()
    print(f"[team] {len(team_subteam_ids)} team subteam-ids loaded (from team_subteams.yaml)")

    log.info("Slack ingest starting. channels=%d dry_run=%s", len(channels), args.dry_run)
    print(f"\ningesting {len(channels)} channel(s)...\n")
    summaries: list[dict] = []
    any_success = False
    checked_ids: list[str] = []
    for ch in channels:
        cname = ch.get("name", ch["id"])
        t = time.monotonic()
        s = ingest_channel(client, ch, args.dry_run, users_cache, name_resolver,
                           subteams_cache, team_slack_ids=team_slack_ids,
                           team_subteam_ids=team_subteam_ids)
        s["elapsed_s"] = round(time.monotonic() - t, 2)
        summaries.append(s)
        if s.get("skipped"):
            print(f"  {cname:35s} SKIP — {s.get('reason', '?')}")
        elif s.get("errors"):
            print(f"  {cname:35s} ERR  — {s['errors'][0]}")
        else:
            # Polled successfully (new msgs or not) → record check time NOW.
            # Incremental (per-channel) write so a long/killed/overlapped fire
            # still records every channel it actually reached. Batching at the
            # loop end meant 98-channel rate-limited fires that got killed
            # mid-run recorded nothing → permanent "? unpolled".
            cid = s.get("id", ch["id"])
            checked_ids.append(cid)
            if not args.dry_run:
                write_checked_marks([cid])
            print(f"  {cname:35s} ok   "
                  f"top+{s.get('top_inserted',0):3d} "
                  f"repl+{s.get('replies_inserted',0):3d} "
                  f"edit~{s.get('rec_edits',0):2d}/{s.get('rec_thread_edits',0):2d} "
                  f"del~{s.get('rec_deletes',0):2d} "
                  f"({s['elapsed_s']}s)")
            any_success = True

    if any_success and not args.dry_run:
        write_success_marker()
        print(f"\n[success] wrote {SUCCESS_PATH}")

    if checked_ids and not args.dry_run:
        # Already written incrementally per-channel above; this is just the tally.
        print(f"[checked] recorded poll time for {len(checked_ids)} channel(s)")

    # Done sentinel — totals aggregated across channels for parse_runs.
    total_new = sum((s.get("top_inserted", 0) or 0)
                    + (s.get("replies_inserted", 0) or 0)
                    + (s.get("rec_late_inserts", 0) or 0)
                    for s in summaries)
    total_edits = sum(s.get("rec_edits", 0) or 0 for s in summaries)
    total_dels = sum(s.get("rec_deletes", 0) or 0 for s in summaries)
    log.info("Done. source=slack total_new=%d total_dup=0 edits=%d deletes=%d",
             total_new, total_edits, total_dels)

    print("\n--- summary ---")
    print(json.dumps(summaries, indent=2))

    return 0 if any_success or all(s.get("skipped") for s in summaries) else 2


if __name__ == "__main__":
    sys.exit(main())

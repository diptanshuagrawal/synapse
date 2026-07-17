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
    python -m ingest.slack_ingest_app --threads-sweep  # pre-standup late-reply sweep + search net
    python -m ingest.slack_ingest_app --search-net     # team-search safety net only

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
import urllib.parse
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
from derive.slack_backfill_helper import (  # noqa: E402
    _clamp_parent_after_drain,
    enqueue_pending_reply_check,
    pop_pending_reply_checks,
    dequeue_pending_reply_check,
    bump_pending_reply_attempt,
    count_pending_reply_checks,
)
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
PENDING_REPLY_DRAIN_CAP = 40     # max budget-starved bot parents whose replies we drain per fire (separate budget from TEAM_REPLY_CHECK_CAP)
PENDING_REPLY_MAX_ATTEMPTS = 5   # abandon a queued parent after this many failed drain attempts (broken replies endpoint)
BOOTSTRAP_LOOKBACK_DAYS = 365    # if channel has null cursor, start fetching from now-N days
ACTIVE_THREAD_DAYS = 90          # re-drain threads with a reply in the last N days (catches late replies to old parents)
# Pre-standup sweep (--threads-sweep) tuning. WIDER than steady-state: it must catch a
# late reply on ANY channel the team actively uses (incl. busy cross-team channels like
# cbs-public), not just team_involved/oncall (validated miss 2026-06-24).
SWEEP_ROSTER_DAYS = 14          # sweep any channel a roster member posted in over the last N days
                                # (aligned with the active-thread window: the missing reply itself
                                # isn't in the DB yet, so the channel is detected via the member's
                                # PRIOR activity — 3d was too tight, missed cbs-public's 5-day gap)
SWEEP_ACTIVE_DAYS = 14          # re-walk threads with known activity in the last N days
SWEEP_CAP = 200                 # higher per-channel cap (once-daily bounded pass) so busy channels don't starve
# Team-search safety net (runs with --threads-sweep, or standalone via --search-net).
# Closes the LAST team_involved blind spot: a member's LATE reply to a thread whose
# non-team root was scanned (and correctly dropped — no team involvement YET) before
# the reply existed. The cursor has moved past the root, and every reconcile path
# (Phase 2.4 queue, Phase 2.5 stale/active) only revisits parents already in events.db,
# so that thread is otherwise permanently invisible (validated miss 2026-07-03:
# a member's lien-flow investigation reply on a team_involved support channel).
# search.messages `from:<member>` finds the reply regardless of what the cursor
# did to its root.
SEARCH_NET_LOOKBACK_DAYS = 7    # search window; late replies older than this are accepted drift
SEARCH_NET_PAGE_CAP = 3         # search.messages pages (100 hits each) per member per fire
SEARCH_NET_DRAIN_CAP = 50       # max missing-thread drains per fire (spillover logged + next fire)


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
) -> tuple[int, int, Optional[str], int, int, int, int, bool]:
    """Cursor-bound history fetch, FILTERED to threads where team participates.

    Decision per top-level msg:
      • Parent author in team OR parent body mentions team → KEEP (upsert)
      • Otherwise + reply_count > 0 + budget available → fetch replies, check.
        If any reply by team OR body mentions team → KEEP parent + replies.
        Else → DROP (don't upsert anything).
      • Otherwise + reply_count > 0 + budget EXHAUSTED → ENQUEUE for a later
        fire's reply-check drain (slack_pending_reply_check). Critically we do
        NOT silently drop: the cursor advances past this root, so without the
        queue a team reply buried under a starved bot root is lost forever.
      • Otherwise + reply_count == 0 → DROP (nothing to walk).

    Inline-fetches replies for tentative parents; skips Phase 2.5/2.7 for
    team_involved channels (replies handled here).

    Returns (top_inserted, replies_inserted, newest_ts, bot_skipped,
             dropped_parents, deferred_checks, starved_enqueued, hit_cap).
    """
    conn = sqlite3.connect(DB_PATH)
    inserted_total = 0
    replies_inserted = 0
    bot_skipped = 0
    dropped = 0
    deferred_checks = 0
    starved_enqueued = 0
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
                        # A bot root whose BODY tags a team member (e.g. a
                        # release bot pinging the owner to approve a CMR). If it
                        # has replies, the team member typically responds
                        # IN-THREAD — so KEEP the root: once stored, Phase 2.5
                        # reconcile walks its replies and captures that response.
                        # The old `bot_skipped; continue` dropped the root AND
                        # never walked the replies; because the cursor then
                        # advances past the root and the late-reply re-drain only
                        # revisits parents already in the DB, those replies (an
                        # owner action item) were lost forever. See
                        # tests/test_slack_team_reply_reconcile.py.
                        if (msg.get("reply_count") or 0) > 0:
                            keep_top.append(pm)
                        else:
                            # Team-tagged bot ping with no thread — nothing to recover.
                            bot_skipped += 1
                        continue
                    keep_top.append(pm)
                    continue

                reply_count = msg.get("reply_count") or 0
                if reply_count <= 0:
                    # Genuinely no replies to walk — drop.
                    if pm.is_bot and not keep_bot_messages:
                        bot_skipped += 1
                    else:
                        dropped += 1
                    continue
                if deferred_checks >= TEAM_REPLY_CHECK_CAP:
                    # Budget exhausted but this root HAS replies — a team reply
                    # may be buried here. The cursor will advance past this root
                    # this fire, so we must not drop-and-forget: enqueue it for a
                    # later fire's reply-check drain (see reconcile_pending_reply_checks).
                    if not dry_run:
                        enqueue_pending_reply_check(conn, channel_id, ts, reply_count)
                    starved_enqueued += 1
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
            dropped, deferred_checks, starved_enqueued, hit_cap)


def reconcile_pending_reply_checks(
    client: SlackClient,
    channel_id: str,
    dry_run: bool,
    users_cache: dict[str, str],
    keep_bot_messages: bool,
    name_resolver,
    subteams_cache: dict[str, str],
    team_slack_ids: set[str],
    team_subteam_ids: Optional[set[str]] = None,
) -> dict:
    """Drain budget-starved bot-rooted parents queued by the history pass.

    For each queued parent (oldest-first, capped at PENDING_REPLY_DRAIN_CAP):
      • Walk its replies via conversations.replies. The root comes back as the
        ts==parent_ts element — we parse it to recover the (bot) root we never
        stored, the rest are replies.
      • If the root OR any reply is team-involved → upsert root (kept regardless
        of bot flag, like the inline path) + replies, then dequeue.
      • Else → dequeue (confirmed no team — nothing of ours in this thread).
      • On API error → bump attempts and leave queued (retried next fire until
        PENDING_REPLY_MAX_ATTEMPTS, then abandoned by pop_pending_reply_checks).

    Decouples the reply-walk from the cursor, so a team reply buried under a
    bot root that the history pass starved still lands in events.db on a later
    fire. Returns a summary dict of counters.
    """
    out = {
        "queue_in": 0, "drained": 0, "kept": 0, "no_team": 0,
        "root_inserted": 0, "replies_inserted": 0, "errors": [],
        "queue_out": 0,
    }
    conn = sqlite3.connect(DB_PATH)
    try:
        out["queue_in"] = count_pending_reply_checks(conn, channel_id)
        queued = pop_pending_reply_checks(
            conn, channel_id, PENDING_REPLY_DRAIN_CAP, PENDING_REPLY_MAX_ATTEMPTS,
        )
        for parent_ts, _declared_rc, _attempts in queued:
            try:
                root_pm = None
                replies: list = []
                team_in_thread = False
                for raw in client.iter_replies(channel_id, parent_ts, limit=LIMIT):
                    rpm = api_message_to_parsed(
                        raw, users_cache, name_resolver=name_resolver,
                        subteams_cache=subteams_cache,
                    )
                    if raw.get("ts") == parent_ts:
                        root_pm = rpm
                        if is_team_involved(rpm.actor_id, rpm.body,
                                            team_slack_ids, team_subteam_ids):
                            team_in_thread = True
                        continue
                    if rpm.is_bot and not keep_bot_messages:
                        continue
                    replies.append(rpm)
                    if is_team_involved(rpm.actor_id, rpm.body,
                                        team_slack_ids, team_subteam_ids):
                        team_in_thread = True

                out["drained"] += 1
                if team_in_thread:
                    out["kept"] += 1
                    if not dry_run:
                        if root_pm is not None:
                            # Keep the root regardless of bot flag — it gives the
                            # replies meaning (matches inline keep_top behaviour).
                            res = upsert_messages(
                                conn, [root_pm], channel_id, thread_parent_ts=None,
                            )
                            out["root_inserted"] += res.inserted
                        if replies:
                            res = upsert_messages(
                                conn, replies, channel_id, thread_parent_ts=parent_ts,
                            )
                            out["replies_inserted"] += res.inserted
                        dequeue_pending_reply_check(conn, channel_id, parent_ts)
                    else:
                        out["root_inserted"] += 1 if root_pm is not None else 0
                        out["replies_inserted"] += len(replies)
                else:
                    out["no_team"] += 1
                    if not dry_run:
                        dequeue_pending_reply_check(conn, channel_id, parent_ts)
            except Exception as e:
                out["errors"].append(f"{parent_ts}: {e}")
                if not dry_run:
                    bump_pending_reply_attempt(conn, channel_id, parent_ts)

        out["queue_out"] = count_pending_reply_checks(conn, channel_id)
    finally:
        conn.close()
    return out


def _thread_root_from_match(match: dict) -> Optional[str]:
    """Root ts for a search.messages hit.

    A reply's permalink carries `?thread_ts=<root>`; a top-level message's
    doesn't (the hit IS the root). Parsed from the permalink so no extra
    API call is needed per hit.
    """
    ts = match.get("ts")
    permalink = match.get("permalink") or ""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(permalink).query)
        root = (q.get("thread_ts") or [None])[0]
    except ValueError:
        root = None
    return root or ts


def _drain_thread_if_team(
    client: SlackClient,
    conn: sqlite3.Connection,
    channel_id: str,
    root_ts: str,
    dry_run: bool,
    users_cache: dict[str, str],
    keep_bot_messages: bool,
    name_resolver,
    subteams_cache: dict[str, str],
    team_slack_ids: set[str],
    team_subteam_ids: Optional[set[str]] = None,
) -> tuple[int, int, bool]:
    """Walk one thread via conversations.replies; upsert root + replies iff a
    team member is involved. Same keep semantics as reconcile_pending_reply_checks
    (root kept regardless of bot flag — it gives the replies meaning; bot replies
    skipped unless keep_bot_messages). Returns (root_inserted, replies_inserted,
    team_in_thread)."""
    root_pm = None
    replies: list = []
    team_in_thread = False
    for raw in client.iter_replies(channel_id, root_ts, limit=LIMIT):
        rpm = api_message_to_parsed(
            raw, users_cache, name_resolver=name_resolver,
            subteams_cache=subteams_cache,
        )
        if raw.get("ts") == root_ts:
            root_pm = rpm
            if is_team_involved(rpm.actor_id, rpm.body, team_slack_ids, team_subteam_ids):
                team_in_thread = True
            continue
        if rpm.is_bot and not keep_bot_messages:
            continue
        replies.append(rpm)
        if is_team_involved(rpm.actor_id, rpm.body, team_slack_ids, team_subteam_ids):
            team_in_thread = True

    if not team_in_thread:
        return 0, 0, False
    if dry_run:
        return (1 if root_pm is not None else 0), len(replies), True

    root_inserted = replies_inserted = 0
    if root_pm is not None:
        root_inserted = upsert_messages(
            conn, [root_pm], channel_id, thread_parent_ts=None,
        ).inserted
    if replies:
        replies_inserted = upsert_messages(
            conn, replies, channel_id, thread_parent_ts=root_ts,
        ).inserted
    # Clamp reply_count to what actually landed + stamp drain_attempted_at so
    # the stale detector and the active-thread cooldown see this drain.
    _clamp_parent_after_drain(conn, channel_id, root_ts)
    return root_inserted, replies_inserted, True


def search_net_recover_missed_threads(
    client: SlackClient,
    team_channels: list[dict],
    team_slack_ids: set[str],
    dry_run: bool,
    users_cache: dict[str, str],
    name_resolver,
    subteams_cache: dict[str, str],
    team_subteam_ids: Optional[set[str]] = None,
) -> dict:
    """Search-driven safety net over team_involved channels.

    For each roster member, search.messages `from:<@uid> after:<N days ago>`
    (newest-first). Any hit inside a team_involved channel whose thread —
    root event OR the hit itself — is missing from events.db marks a thread
    the cursor-bound pipeline lost; drain it whole (root + replies).

    Why this exists: fetch_history_team_filtered correctly drops a non-team
    root that has no team replies YET. The cursor then advances past it, and
    every reconcile path only re-drains parents already stored — so a member's
    LATE reply to that root was permanently unfetchable. Search keys on the
    member, not the root, so it sees the reply no matter when it lands.

    Idempotent: already-ingested threads are skipped by the events.db id
    check; re-drains are upserts. Returns a counters dict.
    """
    out = {
        "members_searched": 0, "hits_seen": 0, "hits_in_scope": 0,
        "threads_checked": 0, "threads_missing": 0, "threads_drained": 0,
        "threads_kept": 0, "root_inserted": 0, "replies_inserted": 0,
        "drain_cap_dropped": 0, "errors": [],
    }
    chan_by_id = {c["id"]: c for c in team_channels if c.get("id")}
    if not chan_by_id or not team_slack_ids:
        return out

    after = (
        datetime.now(tz=timezone.utc) - timedelta(days=SEARCH_NET_LOOKBACK_DAYS)
    ).date().isoformat()

    # (channel_id, root_ts) → event-ids that must ALL exist in events.db for
    # the thread to count as covered (the root + every in-scope hit under it).
    threads: dict[tuple[str, str], set[str]] = {}
    for uid in sorted(team_slack_ids):
        out["members_searched"] += 1
        page = 1
        while page <= SEARCH_NET_PAGE_CAP:
            try:
                resp = client.search_messages(
                    f"from:<@{uid}> after:{after}", count=100, page=page,
                )
            except Exception as e:
                out["errors"].append(f"search {uid} p{page}: {e}")
                break
            msgs = resp.get("messages") or {}
            for m in msgs.get("matches") or []:
                out["hits_seen"] += 1
                cid = (m.get("channel") or {}).get("id")
                ts = m.get("ts")
                if not cid or not ts or cid not in chan_by_id:
                    continue
                out["hits_in_scope"] += 1
                root_ts = _thread_root_from_match(m)
                ids = threads.setdefault((cid, root_ts), {f"slack:{cid}:{root_ts}"})
                if ts != root_ts:
                    ids.add(f"slack:{cid}:{root_ts}:{ts}")
            paging = msgs.get("paging") or {}
            if page >= int(paging.get("pages") or 1):
                break
            page += 1

    out["threads_checked"] = len(threads)
    if not threads:
        return out

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    touched_channels: set[str] = set()
    try:
        missing: list[tuple[str, str]] = []
        for (cid, root_ts), expected_ids in sorted(threads.items()):
            id_list = sorted(expected_ids)
            ph = ",".join("?" * len(id_list))
            have = {
                r[0] for r in conn.execute(
                    f"SELECT id FROM events WHERE id IN ({ph})", id_list,
                )
            }
            if have >= expected_ids:
                continue
            missing.append((cid, root_ts))
        out["threads_missing"] = len(missing)
        if len(missing) > SEARCH_NET_DRAIN_CAP:
            out["drain_cap_dropped"] = len(missing) - SEARCH_NET_DRAIN_CAP
            missing = missing[:SEARCH_NET_DRAIN_CAP]

        for cid, root_ts in missing:
            ch = chan_by_id[cid]
            keep_bot = str(ch.get("keep_bot_messages", "false")).lower() == "true"
            try:
                r_ins, p_ins, kept = _drain_thread_if_team(
                    client, conn, cid, root_ts, dry_run, users_cache, keep_bot,
                    name_resolver, subteams_cache, team_slack_ids, team_subteam_ids,
                )
            except Exception as e:
                out["errors"].append(f"drain {cid}:{root_ts}: {e}")
                continue
            out["threads_drained"] += 1
            if kept:
                out["threads_kept"] += 1
                out["root_inserted"] += r_ins
                out["replies_inserted"] += p_ins
                if r_ins or p_ins:
                    touched_channels.add(cid)
    finally:
        conn.close()

    if not dry_run and touched_channels:
        from subprocess import run
        for cid in sorted(touched_channels):
            run([".venv/bin/python", "derive/build_thread_summary.py", "--channel", cid],
                cwd=str(_PKG_ROOT), check=False, capture_output=True)
    return out


def fetch_threads_capped(
    client: SlackClient,
    channel_id: str,
    parents: list[str],
    dry_run: bool,
    users_cache: dict[str, str],
    keep_bot_messages: bool,
    name_resolver,
    subteams_cache: dict[str, str],
    cap: int = STALE_CAP,
) -> tuple[int, int, list[str], bool]:
    """Returns (replies, bot_skipped, errors, hit_cap).

    Caps at `cap` parents (default STALE_CAP per steady-state fire); the pre-standup
    sweep passes a higher cap so an old-but-recently-active thread on a BUSY channel
    isn't starved past the cap (validated 2026-06-24: a late reply on a 9-Jun-rooted
    #cbs-public thread was ranked below the 50-cap and never re-walked).
    """
    conn = sqlite3.connect(DB_PATH)
    total = 0
    bot_skipped = 0
    errors: list[str] = []
    truncated = parents[:cap]
    hit_cap = len(parents) > cap
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
         dropped, deferred, starved_enqueued, hit_hist_cap) = fetch_history_team_filtered(
            client, cid, existing_cursor, dry_run, users_cache, keep_bot_messages,
            name_resolver, subteams_cache, team_slack_ids, team_subteam_ids,
        )
        summary.update({
            "top_inserted": inserted, "newest_ts": newest_ts,
            "hist_bot_skipped": hist_bot, "hist_hit_cap": hit_hist_cap,
            "team_filter_dropped_parents": dropped,
            "team_filter_deferred_checks": deferred,
            "team_filter_starved_enqueued": starved_enqueued,
        })

        # Phase 2.4 — drain budget-starved bot-rooted parents queued by this or
        # a prior fire. Walks their replies regardless of cursor position so a
        # team reply buried under a starved bot root is never lost (the original
        # release-notification channel / CMR-approval failure mode).
        pend = reconcile_pending_reply_checks(
            client, cid, dry_run, users_cache, keep_bot_messages,
            name_resolver, subteams_cache, team_slack_ids, team_subteam_ids,
        )
        summary.update({
            "pending_reply_queue_in": pend["queue_in"],
            "pending_reply_drained": pend["drained"],
            "pending_reply_kept": pend["kept"],
            "pending_reply_root_inserted": pend["root_inserted"],
            "pending_reply_replies_inserted": pend["replies_inserted"],
            "pending_reply_queue_out": pend["queue_out"],
        })
        summary["errors"].extend(pend["errors"])

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
            # Fold the starved-parent drain into the headline counts so totals
            # (and the success/cron-status tally) reflect recovered messages.
            "top_inserted": inserted + pend["root_inserted"],
            "replies_inserted": repl_inserted_inline + repl_late + pend["replies_inserted"],
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
        if not dry_run and (inserted > 0 or repl_inserted_inline > 0 or repl_late > 0
                            or pend["root_inserted"] > 0 or pend["replies_inserted"] > 0):
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


def channels_with_recent_roster_activity(team_slack_ids: set[str], days: int = SWEEP_ROSTER_DAYS) -> set[str]:
    """channel_ids where a ROSTER member authored a message in the last `days` — the set the
    team actually works in (incl. busy cross-team channels like cbs-public). Used to widen the
    pre-standup sweep beyond team_involved/oncall so late replies there aren't missed."""
    if not team_slack_ids:
        return set()
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn = sqlite3.connect(DB_PATH)
    try:
        ph = ",".join("?" * len(team_slack_ids))
        rows = conn.execute(
            f"SELECT DISTINCT channel_id FROM events WHERE source='slack' AND channel_id IS NOT NULL "
            f"AND ts >= ? AND actor IN ({ph})", (cutoff, *team_slack_ids)).fetchall()
        return {r[0] for r in rows if r[0]}
    finally:
        conn.close()


def sweep_channel_threads(client, ch, dry_run, users_cache, name_resolver, subteams_cache) -> dict:
    """PRE-STANDUP SWEEP — re-walk recently-active threads with the 24h drain cooldown
    BYPASSED, so a previous-evening late reply (on a thread whose root has scrolled below
    the channel cursor) lands BEFORE the 06:00 digest. Slack's own ingest only fires
    12:00–23:00, and the cooldown would otherwise hold the re-walk past standup time.
    Uses a wider active window (SWEEP_ACTIVE_DAYS) + a higher cap (SWEEP_CAP) than steady
    state so a late reply on a busy cross-team channel isn't starved (validated miss
    2026-06-24: a 23-Jun #cbs-public reply on a 9-Jun root, ranked below the 50-cap)."""
    cid = ch["id"]
    keep_bot = str(ch.get("keep_bot_messages", "false")).lower() == "true"
    if (ch.get("class") or "") == "oncall":
        keep_bot = True  # the incident bot's ack/resolve replies ARE the on-call signal
    parents = active_thread_parents(cid, SWEEP_ACTIVE_DAYS, ignore_cooldown=True)
    if not parents:
        return {"id": cid, "swept": 0, "replies_inserted": 0, "errors": []}
    repl, _bot, errs, hit_cap = fetch_threads_capped(
        client, cid, parents, dry_run, users_cache, keep_bot, name_resolver, subteams_cache,
        cap=SWEEP_CAP,
    )
    if not dry_run and repl > 0:
        from subprocess import run
        run([".venv/bin/python", "derive/build_thread_summary.py", "--channel", cid],
            cwd=str(_PKG_ROOT), check=False, capture_output=True)
    return {"id": cid, "swept": min(len(parents), SWEEP_CAP), "replies_inserted": repl,
            "hit_cap": hit_cap, "errors": errs}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("channel", nargs="?", help="optional single channel name/id; default = all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--threads-sweep", action="store_true",
                    help="pre-standup: re-walk recently-active threads (cooldown bypassed) on "
                         "team_involved + oncall channels to pull late replies before the digest; "
                         "also runs the team-search safety net (see --search-net)")
    ap.add_argument("--search-net", action="store_true",
                    help="run ONLY the team-search safety net: search.messages from:<member> "
                         "over the last %d days, drain any hit-thread missing from events.db "
                         "on team_involved channels (recovers late replies to roots the "
                         "cursor dropped before any team reply existed)" % SEARCH_NET_LOOKBACK_DAYS)
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
    print(f"[team] {len(team_slack_ids)} team slack-ids loaded (from people.yaml scope:team)")
    team_subteam_ids = load_team_subteam_ids()
    print(f"[team] {len(team_subteam_ids)} team subteam-ids loaded (from team_subteams.yaml)")

    # Pre-standup sweep scope = team_involved + oncall + ANY channel a roster member posted
    # in over the last SWEEP_ROSTER_DAYS (catches busy cross-team channels like cbs-public
    # where the team does real threaded work — validated miss 2026-06-24). Needs team_slack_ids,
    # so it runs AFTER the roster load.
    if args.threads_sweep:
        roster_chans = channels_with_recent_roster_activity(team_slack_ids)
        channels = [c for c in channels
                    if str(c.get("ingest_mode", "")).lower() == "team_involved"
                    or (c.get("class") or "") == "oncall"
                    or c.get("id") in roster_chans]
    elif args.search_net:
        channels = []  # net-only run: skip the per-channel ingest loop entirely

    log.info("Slack ingest starting. channels=%d dry_run=%s", len(channels), args.dry_run)
    print(f"\ningesting {len(channels)} channel(s)...\n")
    summaries: list[dict] = []
    any_success = False
    checked_ids: list[str] = []
    if args.threads_sweep:
        print(f"[threads-sweep] cooldown-bypassed re-walk over {len(channels)} team/oncall/roster-active channel(s)...")
    for ch in channels:
        cname = ch.get("name", ch["id"])
        t = time.monotonic()
        if args.threads_sweep:
            s = sweep_channel_threads(client, ch, args.dry_run, users_cache,
                                      name_resolver, subteams_cache)
            s["elapsed_s"] = round(time.monotonic() - t, 2)
            summaries.append(s)
            if s.get("replies_inserted"):
                print(f"  {cname:35s} swept {s.get('swept',0):3d} thr  repl+{s['replies_inserted']:3d}  ({s['elapsed_s']}s)")
            any_success = True
            continue
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

    # ── Team-search safety net (with --threads-sweep, or standalone --search-net) ──
    # Always scoped to the FULL config's team_involved channels, independent of any
    # sweep/single-channel narrowing above.
    net = None
    if args.threads_sweep or args.search_net:
        team_channels = [c for c in _load_channels_yaml()
                         if str(c.get("ingest_mode", "")).lower() == "team_involved"]
        net = search_net_recover_missed_threads(
            client, team_channels, team_slack_ids, args.dry_run,
            users_cache, name_resolver, subteams_cache, team_subteam_ids,
        )
        print(f"\n[search-net] members={net['members_searched']} "
              f"hits={net['hits_seen']}/{net['hits_in_scope']}-in-scope "
              f"threads={net['threads_checked']} missing={net['threads_missing']} "
              f"drained={net['threads_drained']} "
              f"root+{net['root_inserted']} repl+{net['replies_inserted']}")
        if net["drain_cap_dropped"]:
            print(f"[search-net] CAP: {net['drain_cap_dropped']} missing thread(s) "
                  f"deferred to next fire (drain cap {SEARCH_NET_DRAIN_CAP})")
        for e in net["errors"]:
            print(f"[search-net] ERR {e}")
        if args.threads_sweep and (net["root_inserted"] or net["replies_inserted"]):
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
    if net:
        total_new += (net["root_inserted"] or 0) + (net["replies_inserted"] or 0)
    total_edits = sum(s.get("rec_edits", 0) or 0 for s in summaries)
    total_dels = sum(s.get("rec_deletes", 0) or 0 for s in summaries)
    log.info("Done. source=slack total_new=%d total_dup=0 edits=%d deletes=%d",
             total_new, total_edits, total_dels)

    print("\n--- summary ---")
    print(json.dumps(summaries, indent=2))

    return 0 if any_success or all(s.get("skipped") for s in summaries) else 2


if __name__ == "__main__":
    sys.exit(main())

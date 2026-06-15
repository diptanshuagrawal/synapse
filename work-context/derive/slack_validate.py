#!/usr/bin/env python3
"""
slack_validate.py — sanity-check Slack-ingested data in events.db.

Layered checks (cheap → expensive):

  1. counts          rows per channel (top, replies, declared total)
  2. reply_drift     replies_in_db vs SUM(reply_count) per channel
  3. cursor_lag      slack_cursors.json head vs MAX(thread_started ts)
  4. orphan_replies  thread_reply with no matching thread_started parent
  5. raw_mentions    bodies still containing `<@U…>` (mention-expansion miss)
  6. bot_leaks       is_bot=true rows in channels with keep_bot_messages=false
  7. dup_ts          duplicate (channel_id, ts) — should be 0 (PK invariant)
  8. summary_lag     thread_summary rows vs parents_with_replies
  9. success_marker  state/last_slack_success.date freshness

Optional --deep adds per-channel API spot-check of latest non-empty thread.

Exit codes:
    0   all checks clean
    1   one or more anomalies
    2   env/config error
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_UNRESOLVED_MENTION = re.compile(r"<@[UW][A-Z0-9]+>")

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH  # noqa: E402

CURSOR_PATH = _REPO_ROOT / "state" / "slack_cursors.json"
SUCCESS_PATH = _REPO_ROOT / "state" / "last_slack_success.date"
CHANNELS_YAML = _REPO_ROOT / "config" / "slack_channels.yaml"

# thresholds
REPLY_DRIFT_WARN_PCT = 5
REPLY_DRIFT_FAIL_PCT = 25
CURSOR_LAG_WARN_HOURS = 2
CURSOR_LAG_FAIL_HOURS = 24
SUCCESS_MARKER_FAIL_HOURS = 6  # cron fires every 30min — anything >6h is bad

# ANSI
RED = "\033[31m"
YEL = "\033[33m"
GRN = "\033[32m"
DIM = "\033[2m"
RST = "\033[0m"


def _iso_to_epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _load_channels() -> dict[str, dict]:
    """Return {channel_id: {name, keep_bot_messages, ...}}."""
    with CHANNELS_YAML.open() as f:
        cfg = yaml.safe_load(f)
    defaults = cfg.get("defaults", {})
    out = {}
    for c in cfg.get("channels", []):
        cid = c.get("id")
        if not cid or cid == "TODO":
            continue
        merged = {**defaults, **c}
        out[cid] = merged
    return out


def _load_cursors() -> dict[str, str]:
    if not CURSOR_PATH.exists():
        return {}
    with CURSOR_PATH.open() as f:
        return json.load(f)


def _load_excluded_channels(conn: sqlite3.Connection) -> set[str]:
    """Channel ids snapshotted as AUTOMATION (alert/recon/digest) by
    cluster_noise_filter — the same set clustering drops. Their thread replies
    are intentionally capped (>1000-reply mega-threads) and never feed any
    derived output, so reply_drift on these is expected, not a defect."""
    try:
        rows = conn.execute("SELECT channel_id FROM cluster_excluded_channel").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {r[0] for r in rows}


# ── Per-channel checks ──────────────────────────────────────────────────


def check_channel(conn: sqlite3.Connection, cid: str, ch: dict, cursors: dict,
                  excluded: set[str] | None = None) -> dict:
    findings: list[tuple[str, str, str]] = []  # (severity, check, msg)
    name = ch.get("name", cid)
    keep_bot = ch.get("keep_bot_messages", False)
    is_automation = cid in (excluded or set())

    # 1. counts
    row = conn.execute(
        """SELECT
              SUM(CASE WHEN event_type='thread_started' THEN 1 ELSE 0 END),
              SUM(CASE WHEN event_type='thread_started' AND reply_count > 0 THEN 1 ELSE 0 END),
              SUM(CASE WHEN event_type='thread_reply' THEN 1 ELSE 0 END),
              COALESCE(SUM(reply_count), 0),
              MAX(CASE WHEN event_type='thread_started' THEN ts ELSE NULL END)
           FROM events
           WHERE source='slack' AND channel_id=?""",
        (cid,),
    ).fetchone()
    top, parents_with_repl, replies_db, declared, max_ts_iso = row
    top = top or 0
    replies_db = replies_db or 0
    declared = declared or 0

    if top == 0:
        findings.append(("WARN", "counts", "no thread_started rows — channel never ingested?"))

    # 2. reply drift
    if declared > 0:
        drift_abs = abs(declared - replies_db)
        drift_pct = (drift_abs / declared) * 100
        if drift_pct >= REPLY_DRIFT_WARN_PCT:
            # Automation channels (alert/recon/digest) are excluded from
            # clustering and routinely carry >1000-reply mega-threads that hit
            # the ingest reply cap — drift there is expected, not a defect.
            # Surface it as INFO (visible, not counted as FAIL/WARN).
            if is_automation:
                sev = "INFO"
            else:
                sev = "FAIL" if drift_pct >= REPLY_DRIFT_FAIL_PCT else "WARN"
            note = " [automation — drift expected]" if is_automation else ""
            findings.append((sev, "reply_drift",
                             f"replies_db={replies_db} declared={declared} "
                             f"({drift_pct:.1f}% off){note}"))

    # 3. cursor lag
    cursor = cursors.get(cid)
    if cursor and max_ts_iso:
        try:
            lag_s = float(cursor) - _iso_to_epoch(max_ts_iso)
            lag_h = lag_s / 3600
            # negative lag = cursor behind newest db (should advance next fire)
            if lag_h < -CURSOR_LAG_FAIL_HOURS:
                findings.append(("FAIL", "cursor_lag",
                                 f"cursor is {-lag_h:.1f}h BEHIND newest db row"))
            elif lag_h < -CURSOR_LAG_WARN_HOURS:
                findings.append(("WARN", "cursor_lag",
                                 f"cursor is {-lag_h:.1f}h behind newest db row"))
        except (ValueError, OSError):
            findings.append(("WARN", "cursor_lag", f"unparseable cursor: {cursor!r}"))
    elif top > 0 and not cursor:
        findings.append(("FAIL", "cursor_lag", "channel has rows but no cursor — won't advance"))

    # 4. orphan replies — exact lookup via PK. Avoid strftime on ISO ts: SQLite
    # rounds the integer seconds up when the fractional part ≥ .5 (e.g.
    # `2025-11-15T04:11:18.999699Z` → 1763179879 instead of 1763179878), which
    # made the previous BETWEEN check miss every parent with a high-fraction ts.
    orphans = conn.execute(
        """SELECT COUNT(*) FROM events r
           WHERE r.source='slack' AND r.channel_id=? AND r.event_type='thread_reply'
             AND NOT EXISTS (
               SELECT 1 FROM events p
               WHERE p.id = 'slack:' || r.channel_id || ':' || r.thread_ts
                 AND p.event_type = 'thread_started'
             )""",
        (cid,),
    ).fetchone()[0]
    if orphans > 0:
        findings.append(("WARN", "orphan_replies", f"{orphans} replies have no parent in db"))

    # 5. raw mentions — only unresolved `<@U…>` (no `|name` suffix);
    # legitimate rich-mentions `<@U…|Name>` are produced by Slack itself.
    candidates = conn.execute(
        """SELECT body FROM events
           WHERE source='slack' AND channel_id=? AND body LIKE '%<@U%'""",
        (cid,),
    ).fetchall()
    raw_mentions = sum(1 for (b,) in candidates if b and _UNRESOLVED_MENTION.search(b))
    if raw_mentions > 0:
        pct = (raw_mentions / max(top + replies_db, 1)) * 100
        sev = "FAIL" if pct >= 5 else "WARN"
        findings.append((sev, "raw_mentions",
                         f"{raw_mentions} rows have unresolved <@U…> ({pct:.1f}%)"))

    # 6. bot leaks
    if not keep_bot:
        bot_n = conn.execute(
            """SELECT COUNT(*) FROM events
               WHERE source='slack' AND channel_id=?
                 AND actor IS NOT NULL
                 AND json_valid(actor)
                 AND json_extract(actor, '$.is_bot')=1""",
            (cid,),
        ).fetchone()[0]
        if bot_n > 0:
            findings.append(("WARN", "bot_leaks", f"{bot_n} bot rows in keep_bot=false channel"))

    # 7. duplicate (ts, event_type) — Slack's thread_broadcast legitimately
    # produces 1 thread_started + 1 thread_reply with same ts; only same ts
    # within the same event_type is a real dup.
    dup_n = conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT ts FROM events
               WHERE source='slack' AND channel_id=?
               GROUP BY ts, event_type HAVING COUNT(*) > 1
           )""",
        (cid,),
    ).fetchone()[0]
    if dup_n > 0:
        findings.append(("FAIL", "dup_ts", f"{dup_n} duplicate (ts, event_type) in channel"))

    return {
        "channel": name,
        "id": cid,
        "top": top,
        "parents_with_replies": parents_with_repl or 0,
        "replies_db": replies_db,
        "declared_reply_total": declared,
        "max_ts": max_ts_iso,
        "cursor": cursor,
        "findings": findings,
    }


# ── Global checks ───────────────────────────────────────────────────────


def check_global(conn: sqlite3.Connection, per_channel: list[dict]) -> list[tuple[str, str, str]]:
    findings: list[tuple[str, str, str]] = []

    # 8. summary_lag — thread_summary count vs sum(parents_with_replies)
    try:
        ts_count = conn.execute("SELECT COUNT(*) FROM thread_summary").fetchone()[0]
        parents_total = sum(c["parents_with_replies"] for c in per_channel)
        if parents_total > 0 and ts_count < parents_total * 0.5:
            findings.append(("WARN", "summary_lag",
                             f"thread_summary={ts_count} parents_with_replies={parents_total} (<50%)"))
    except sqlite3.OperationalError:
        findings.append(("WARN", "summary_lag", "thread_summary table not found"))

    # 9. success marker freshness
    if not SUCCESS_PATH.exists():
        findings.append(("FAIL", "success_marker", f"{SUCCESS_PATH} missing — ingest never succeeded"))
    else:
        text = SUCCESS_PATH.read_text().strip()
        try:
            d = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            age_h = (datetime.now(tz=timezone.utc) - d).total_seconds() / 3600
            if age_h > SUCCESS_MARKER_FAIL_HOURS + 24:
                # marker is date-only so even today reads ~12h old by EOD; >30h means missed a day
                findings.append(("FAIL", "success_marker",
                                 f"{text} is {age_h:.0f}h old — cron not firing?"))
        except ValueError:
            findings.append(("WARN", "success_marker", f"unparseable marker: {text!r}"))

    return findings


# ── Deep mode: API spot-check ──────────────────────────────────────────


def deep_check_channel(cid: str, name: str, conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Pull latest thread parent from db, fetch its replies via API, compare counts."""
    from ingest.slack_api_client import SlackClient

    findings: list[tuple[str, str, str]] = []
    row = conn.execute(
        """SELECT ts, reply_count FROM events
           WHERE source='slack' AND channel_id=?
             AND event_type='thread_started' AND reply_count > 0
           ORDER BY ts DESC LIMIT 1""",
        (cid,),
    ).fetchone()
    if not row:
        return findings
    iso_ts, declared = row
    epoch = f"{_iso_to_epoch(iso_ts):.6f}"
    db_replies = conn.execute(
        """SELECT COUNT(*) FROM events
           WHERE source='slack' AND channel_id=?
             AND event_type='thread_reply' AND thread_ts=?""",
        (cid, epoch),
    ).fetchone()[0]
    try:
        client = SlackClient()
        api_replies = sum(1 for m in client.iter_replies(cid, epoch, limit=1000)
                          if m.get("ts") != epoch)
    except Exception as e:
        findings.append(("WARN", "deep_api", f"API call failed: {e}"))
        return findings
    if abs(api_replies - db_replies) > 2:
        findings.append(("FAIL", "deep_api",
                         f"parent {epoch}: api={api_replies} db={db_replies}"))
    return findings


# ── Render ──────────────────────────────────────────────────────────────


def _sev_colour(sev: str) -> str:
    return {"FAIL": RED, "WARN": YEL, "INFO": DIM}.get(sev, "")


def render_channel(c: dict) -> None:
    drift = ""
    if c["declared_reply_total"] > 0:
        pct = (c["replies_db"] / c["declared_reply_total"]) * 100
        drift = f"{c['replies_db']}/{c['declared_reply_total']} ({pct:.1f}%)"
    print(f"{c['channel']:35s}  top={c['top']:>5d}  repl={drift}")
    for sev, check, msg in c["findings"]:
        col = _sev_colour(sev)
        print(f"  {col}{sev:4s}{RST}  {check:14s}  {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", help="single channel id or name (default: all)")
    ap.add_argument("--deep", action="store_true",
                    help="add per-channel API spot-check of latest thread")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    channels = _load_channels()
    if args.channel:
        match = [(cid, ch) for cid, ch in channels.items()
                 if cid == args.channel or ch.get("name") == args.channel]
        if not match:
            print(f"channel not found: {args.channel}", file=sys.stderr)
            return 2
        channels = dict(match)

    cursors = _load_cursors()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = None
    excluded = _load_excluded_channels(conn)

    per_channel: list[dict] = []
    for cid, ch in channels.items():
        c = check_channel(conn, cid, ch, cursors, excluded)
        if args.deep:
            c["findings"].extend(deep_check_channel(cid, ch.get("name", cid), conn))
        per_channel.append(c)

    global_findings = check_global(conn, per_channel)

    if args.json:
        print(json.dumps({
            "channels": per_channel,
            "global": [{"sev": s, "check": c, "msg": m} for s, c, m in global_findings],
        }, indent=2, default=str))
    else:
        print(f"\n=== slack_validate — {len(per_channel)} channel(s) ===\n")
        for c in per_channel:
            render_channel(c)
        print("\n=== global ===")
        if not global_findings:
            print(f"  {GRN}OK{RST}")
        for sev, check, msg in global_findings:
            col = _sev_colour(sev)
            print(f"  {col}{sev:4s}{RST}  {check:14s}  {msg}")
        # overall summary
        fails = sum(1 for c in per_channel for s, _, _ in c["findings"] if s == "FAIL")
        warns = sum(1 for c in per_channel for s, _, _ in c["findings"] if s == "WARN")
        fails += sum(1 for s, _, _ in global_findings if s == "FAIL")
        warns += sum(1 for s, _, _ in global_findings if s == "WARN")
        print(f"\nsummary: {RED if fails else GRN}{fails} FAIL{RST}  "
              f"{YEL if warns else GRN}{warns} WARN{RST}")

    fails = sum(1 for c in per_channel for s, _, _ in c["findings"] if s == "FAIL") \
        + sum(1 for s, _, _ in global_findings if s == "FAIL")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

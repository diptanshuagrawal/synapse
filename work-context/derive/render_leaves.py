#!/usr/bin/env python3
"""render_leaves.py — emit derived/team-leaves.md from team_leaves table.

Sections rendered:
  1. Active today  — today between date_start and date_end (inclusive)
  2. Upcoming      — date_start within next 30 days
  3. Recent past   — date_end within last 14 days
  4. Ambiguous     — date_start IS NULL (chat couldn't parse dates)

Each row links back to the Slack permalink. Idempotent; safe to run
on every cron fire. Touches no other state.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import get_db  # noqa: E402

OUT = _REPO_ROOT / "derived" / "team-leaves.md"
UPCOMING_HORIZON_DAYS = 30
RECENT_LOOKBACK_DAYS = 14


def _fmt_range(ds: str | None, de: str | None) -> str:
    if not ds and not de:
        return "_no dates_"
    if ds and not de:
        return ds
    if not ds and de:
        return f"… → {de}"
    if ds == de:
        return ds
    return f"{ds} → {de}"


def _days(ds: str | None, de: str | None) -> str:
    """Inclusive day count for the range, blank when undated."""
    if not ds or not de:
        return "-"
    try:
        d1 = date.fromisoformat(ds)
        d2 = date.fromisoformat(de)
        return str((d2 - d1).days + 1)
    except ValueError:
        return "-"


_WS_RE = re.compile(r"\s+")


def _clean_excerpt(s: str | None, max_len: int = 70) -> str:
    """Collapse whitespace, drop pipes (cell delimiter), trim."""
    if not s:
        return ""
    s = _WS_RE.sub(" ", s).replace("|", "/").strip()
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s


def _link(url: str | None, label: str) -> str:
    if not url:
        return label
    return f"[{label}]({url})"


def _channel_label(name: str | None, cid: str | None) -> str:
    n = name or cid or "?"
    if len(n) > 28:
        n = n[:27] + "…"
    return f"#{n}" if not n.startswith("#") else n


def main() -> int:
    conn = get_db()
    today = date.today().isoformat()
    horizon = (date.today() + timedelta(days=UPCOMING_HORIZON_DAYS)).isoformat()
    recent_cut = (date.today() - timedelta(days=RECENT_LOOKBACK_DAYS)).isoformat()

    # Dedup view: same actor + dates + reason can appear twice when one
    # slack message is stored as both top-level and thread-context. Keep
    # the row with the lowest event_id (top-level beats nested form).
    DEDUP_CTE = """
        WITH dedup AS (
            SELECT actor, date_start, date_end, reason, mentioned_at,
                   channel_id, channel_name, body_excerpt, url,
                   ROW_NUMBER() OVER (
                       PARTITION BY actor,
                                    COALESCE(date_start,'_'),
                                    COALESCE(date_end,'_'),
                                    COALESCE(reason,'_')
                       ORDER BY LENGTH(event_id), event_id
                   ) AS rn
            FROM team_leaves
        )
    """

    # Active: today between start and end. Treat NULL date_end as same-day.
    active = conn.execute(DEDUP_CTE + """
        SELECT actor, date_start, date_end, reason, mentioned_at,
               channel_id, channel_name, body_excerpt, url
        FROM dedup WHERE rn = 1
          AND date_start IS NOT NULL
          AND date_start <= ?
          AND (date_end IS NULL OR date_end >= ?)
          AND (date_start = date_end OR date_end IS NULL OR date_end >= ?)
        ORDER BY date_start, actor
    """, (today, today, today)).fetchall()

    # Upcoming: starts in future and within horizon.
    upcoming = conn.execute(DEDUP_CTE + """
        SELECT actor, date_start, date_end, reason, mentioned_at,
               channel_id, channel_name, body_excerpt, url
        FROM dedup WHERE rn = 1
          AND date_start IS NOT NULL
          AND date_start > ?
          AND date_start <= ?
        ORDER BY date_start, actor
    """, (today, horizon)).fetchall()

    # Recent past: ended within last 14d.
    recent = conn.execute(DEDUP_CTE + """
        SELECT actor, date_start, date_end, reason, mentioned_at,
               channel_id, channel_name, body_excerpt, url
        FROM dedup WHERE rn = 1
          AND date_end IS NOT NULL
          AND date_end < ?
          AND date_end >= ?
        ORDER BY date_end DESC, actor
    """, (today, recent_cut)).fetchall()

    # Ambiguous: no date_start (chat flagged future leave, date TBD).
    ambig = conn.execute(DEDUP_CTE + """
        SELECT actor, date_start, date_end, reason, mentioned_at,
               channel_id, channel_name, body_excerpt, url
        FROM dedup WHERE rn = 1
          AND date_start IS NULL
          AND mentioned_at >= datetime('now', '-30 days')
        ORDER BY mentioned_at DESC
    """).fetchall()

    total = conn.execute("SELECT COUNT(*) FROM team_leaves").fetchone()[0]
    processed = conn.execute("SELECT COUNT(*) FROM team_leaves_processed").fetchone()[0]

    now_iso = datetime.now(tz=timezone.utc).isoformat()
    lines: list[str] = []
    lines.append("# Team leaves")
    lines.append("")
    lines.append(f"_Generated: {now_iso} · {total} total rows · {processed} events classified_")
    lines.append("")

    def render_section(title: str, rows: list, show_dates: bool = True) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not rows:
            lines.append("_none_")
            lines.append("")
            return
        if show_dates:
            lines.append("| Person | Dates | Days | Reason | Channel | Excerpt | Link |")
            lines.append("|---|---|---|---|---|---|---|")
            for actor, ds, de, reason, ts, cid, cname, excerpt, url in rows:
                lines.append(
                    f"| {actor} "
                    f"| {_fmt_range(ds, de)} "
                    f"| {_days(ds, de)} "
                    f"| {reason or '-'} "
                    f"| {_channel_label(cname, cid)} "
                    f"| {_clean_excerpt(excerpt)} "
                    f"| {_link(url, 'view') if url else '-'} |"
                )
        else:
            lines.append("| Person | Mentioned | Reason | Channel | Excerpt | Link |")
            lines.append("|---|---|---|---|---|---|")
            for actor, ds, de, reason, ts, cid, cname, excerpt, url in rows:
                lines.append(
                    f"| {actor} "
                    f"| {ts[:10] if ts else '-'} "
                    f"| {reason or '-'} "
                    f"| {_channel_label(cname, cid)} "
                    f"| {_clean_excerpt(excerpt)} "
                    f"| {_link(url, 'view') if url else '-'} |"
                )
        lines.append("")

    render_section("Active today", active, show_dates=True)
    render_section(f"Upcoming (next {UPCOMING_HORIZON_DAYS}d)", upcoming, show_dates=True)
    render_section(f"Recent past (last {RECENT_LOOKBACK_DAYS}d)", recent, show_dates=True)
    render_section("Ambiguous (date TBD)", ambig, show_dates=False)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n")
    print(f"[render] wrote {OUT}  (active={len(active)} upcoming={len(upcoming)} "
          f"recent={len(recent)} ambig={len(ambig)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
mom_extractor.py — surface MoM (Minutes of Meeting) signals for retros.

Weekly-sync MoMs in slack channels (default: `service-c-weekly-sync` =
`C0EXAMPLE`) carry concrete go-live dates + numeric impact (% rollout,
₹X revenue, branch counts) that don't reliably cluster into the
embedding-based topic_brief pipeline — they're one-line status callouts
in larger threads.

Retro skill phase 1m consumes this output to layer MoM-grounded delivery
dates onto cluster-grained workstream framing.

Usage:
    .venv/bin/python derive/mom_extractor.py \\
        --since 2026-05-01T00:00:00Z --until 2026-05-28T23:59:59Z \\
        > /tmp/retro_moms.json

Output: JSON array of MoM thread dicts, each with:
  - subject, channel_id, ts, root_actor, root_body (full)
  - replies[]: top-N replies sorted by ts, with actor + body
  - subject_url: slack permalink

The retro synthesizer then reads the bodies + replies to extract
delivery dates / rollout %s / dollar impacts.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH  # noqa: E402
from derive.sources_config import slack_workspace, mom_channels  # noqa: E402

# Channels known to host weekly-sync MoMs. From config (slack.mom_channels).
MOM_CHANNELS = set(mom_channels())

# Title patterns that flag a thread as an ACTUAL MoM post (not a meeting
# reminder). Order matters — narrower patterns first.
MOM_TITLE_PATTERNS = [
    "mom:",
    "mom ",   # e.g. "MoM 27th May" (no colon)
    "tl;dr - mom",
    "tl;dr mom",
    "minutes of",
    "status update -",
    "sync notes",
]

# Anti-patterns: titles that mention "weekly sync" / "MoM" but are reminders
# or meta-discussion, not the MoM itself.
MOM_ANTI_PATTERNS = [
    "please join",
    "joining",
    "<!here>",
    "did the connect",
    "any mom?",
    "where is",
    "any update",
]


def _is_mom_title(title: str) -> bool:
    if not title:
        return False
    t = title.lower().strip().lstrip("*").strip()
    if any(ap in t for ap in MOM_ANTI_PATTERNS):
        return False
    return any(p in t for p in MOM_TITLE_PATTERNS)


def _slack_permalink(subject: str) -> str:
    """`slack:<CHANNEL>:<TS>` → permalink. TS embeds dot already."""
    parts = subject.split(":")
    if len(parts) != 3:
        return ""
    _, channel, ts = parts
    return f"https://{slack_workspace()}.slack.com/archives/{channel}/p{ts.replace('.', '')}"


def collect_moms(conn: sqlite3.Connection, since: str, until: str,
                 channels: set[str] | None = None,
                 max_replies: int = 8) -> list[dict]:
    channels = channels or MOM_CHANNELS
    placeholders = ",".join("?" * len(channels))
    rows = conn.execute(f"""
        SELECT subject, actor, title, body, ts
        FROM events
        WHERE source = 'slack'
          AND event_type = 'thread_started'
          AND ts BETWEEN ? AND ?
          AND substr(subject, 7, 11) IN ({placeholders})
        ORDER BY ts ASC
    """, (since, until, *channels)).fetchall()

    out: list[dict] = []
    for sub, actor, title, body, ts in rows:
        if not _is_mom_title(title or ""):
            continue
        channel = sub.split(":")[1] if sub.count(":") == 2 else ""
        # Pull replies in chronological order, capped
        replies = conn.execute("""
            SELECT actor, body, ts FROM events
            WHERE subject = ? AND event_type = 'thread_reply'
            ORDER BY ts ASC
            LIMIT ?
        """, (sub, max_replies)).fetchall()
        out.append({
            "subject": sub,
            "channel_id": channel,
            "ts": ts,
            "root_actor": actor or "",
            "title": title or "",
            "root_body": body or "",
            "replies": [
                {"actor": a or "", "body": (b or "")[:1000], "ts": t}
                for a, b, t in replies
            ],
            "subject_url": _slack_permalink(sub),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="ISO8601 (e.g. 2026-05-01T00:00:00Z)")
    ap.add_argument("--until", required=True, help="ISO8601 (e.g. 2026-05-28T23:59:59Z)")
    ap.add_argument("--channels", nargs="*", default=None,
                    help="Override default MOM_CHANNELS list (slack channel ids)")
    ap.add_argument("--max-replies", type=int, default=8)
    ap.add_argument("--format", choices=["json", "summary"], default="json")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    channels = set(args.channels) if args.channels else None
    moms = collect_moms(conn, args.since, args.until, channels, args.max_replies)

    if args.format == "summary":
        print(f"MoMs in window: {len(moms)}", file=sys.stderr)
        for m in moms:
            print(f"  {m['ts'][:10]}  {m['title'][:80]}")
            print(f"    {m['subject_url']}")
        return 0

    print(json.dumps(moms, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

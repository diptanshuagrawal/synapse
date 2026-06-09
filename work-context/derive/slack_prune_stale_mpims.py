#!/usr/bin/env python3
"""
slack_prune_stale_mpims.py — remove MPIM entries from slack_channels.yaml
when their last activity is >N days old.

Scope:
  - Targets ONLY rows with `allow_mpim: true` (working-group MPIMs).
  - Leaves all other channel types untouched (public/private/team rooms).
  - Skips MPIMs that have ZERO events in events.db — those just got added
    and haven't been backfilled yet (grace period).

Effect of prune:
  - Removes the yaml row → cron stops fetching.
  - Removes the cursor from state/slack_cursors.json → re-add later would
    auto-bootstrap fresh.
  - events.db rows UNTOUCHED. History preserved.

Dry-run by default. --apply commits the yaml + cursor edits.

Usage:
    python -m derive.slack_prune_stale_mpims                # dry, all MPIMs
    python -m derive.slack_prune_stale_mpims --apply
    python -m derive.slack_prune_stale_mpims --quiet-days 60
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH  # noqa: E402

CHANNELS_YAML = _REPO_ROOT / "config" / "slack_channels.yaml"
CURSORS_JSON = _REPO_ROOT / "state" / "slack_cursors.json"
DEFAULT_QUIET_DAYS = 30


def _load_channels() -> tuple[list[dict], str]:
    raw = CHANNELS_YAML.read_text()
    with CHANNELS_YAML.open() as f:
        cfg = yaml.safe_load(f)
    return cfg.get("channels", []), raw


def _last_event_iso(conn: sqlite3.Connection, channel_id: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(ts) FROM events WHERE source='slack' AND channel_id=?",
        (channel_id,),
    ).fetchone()
    return row[0] if row and row[0] else None


def _days_since(iso_str: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return 999.0
    return (datetime.now(tz=timezone.utc) - dt).total_seconds() / 86400


def _remove_yaml_block(raw: str, channel_id: str) -> tuple[str, bool]:
    """Remove the yaml block for the row whose `id: <channel_id>` line matches.

    Block bounds: from `  - id: <cid>` line through last `    <key>: …` line
    of that row. Preserves surrounding rows + comments.
    """
    lines = raw.splitlines()
    needle = f"  - id: {channel_id}"
    start = None
    for i, line in enumerate(lines):
        if line == needle:
            start = i
            break
    if start is None:
        return raw, False
    end = start + 1
    while end < len(lines):
        # Stop when we hit either a blank line followed by `  - id:` OR EOF
        line = lines[end]
        if line.startswith("  - id:"):
            break
        if line.startswith("  #"):  # comment header for next block
            break
        end += 1
    # Trim trailing blank line if present (clean spacing)
    cut_end = end
    if cut_end > start and lines[cut_end - 1].strip() == "":
        cut_end -= 1
    new_lines = lines[:start] + lines[cut_end:]
    # Collapse leading blank if removal left two blanks in a row
    while (start < len(new_lines) and start > 0
           and new_lines[start - 1].strip() == "" and new_lines[start].strip() == ""):
        new_lines.pop(start)
    return "\n".join(new_lines) + ("\n" if raw.endswith("\n") else ""), True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet-days", type=int, default=DEFAULT_QUIET_DAYS,
                    help=f"prune MPIMs with no activity in this many days (default {DEFAULT_QUIET_DAYS})")
    ap.add_argument("--apply", action="store_true",
                    help="commit yaml + cursor edits; default dry-run")
    args = ap.parse_args()

    channels, raw = _load_channels()
    mpim_rows = [c for c in channels
                 if str(c.get("allow_mpim", "false")).lower() == "true"]
    print(f"[scan] {len(mpim_rows)} MPIM rows in yaml (out of {len(channels)} channels)",
          flush=True)
    if not mpim_rows:
        print("nothing to do")
        return 0

    conn = sqlite3.connect(DB_PATH)
    cursors = json.loads(CURSORS_JSON.read_text()) if CURSORS_JSON.exists() else {}

    to_prune: list[tuple[dict, float, str | None]] = []  # (row, days_quiet, last_iso)
    print(f"\n{'channel':<60}  {'last_event':<28}  days_quiet  verdict")
    print("-" * 110)
    for ch in mpim_rows:
        cid = ch.get("id")
        cname = ch.get("name", cid)
        last_iso = _last_event_iso(conn, cid)
        if not last_iso:
            print(f"{cname[:60]:<60}  {'(no events yet)':<28}  {'-':>10}  GRACE-skip")
            continue
        days = _days_since(last_iso)
        verdict = "PRUNE" if days > args.quiet_days else "keep"
        print(f"{cname[:60]:<60}  {last_iso[:28]:<28}  {days:>10.1f}  {verdict}")
        if days > args.quiet_days:
            to_prune.append((ch, days, last_iso))

    conn.close()

    print(f"\n[summary] {len(to_prune)} MPIMs to prune (quiet>{args.quiet_days}d) "
          f"out of {len(mpim_rows)} total MPIMs")

    if not args.apply:
        print("\n[dry] re-run with --apply to remove yaml rows + cursors")
        return 0

    if not to_prune:
        return 0

    # Apply yaml edits (one channel at a time, walking the line-based remover).
    new_raw = raw
    pruned_ids = []
    for ch, _, _ in to_prune:
        cid = ch["id"]
        new_raw, removed = _remove_yaml_block(new_raw, cid)
        if removed:
            pruned_ids.append(cid)
        else:
            print(f"  WARN: yaml block for {cid} not found — skipped",
                  file=sys.stderr)
    CHANNELS_YAML.write_text(new_raw)
    print(f"\n[apply] removed {len(pruned_ids)} yaml rows")

    # Remove cursors for pruned channels
    cursor_drops = [cid for cid in pruned_ids if cid in cursors]
    for cid in cursor_drops:
        cursors.pop(cid, None)
    if cursor_drops:
        CURSORS_JSON.write_text(json.dumps(cursors, indent=2, sort_keys=True))
        print(f"[apply] removed {len(cursor_drops)} cursor entries")

    print("\nNote: events.db rows preserved. Re-discovery would re-add fresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

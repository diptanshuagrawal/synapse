"""One-shot (re-runnable) backfill: populate sprint_id/name/state + story_points
for existing jira issue_created rows.

Why: schema migration added these columns with NULL default. Existing rows
predate the columns. Also useful as a periodic refresh — story points / sprint
assignment can change after issue creation and the standard ingest dedups on
event.id (INSERT OR IGNORE) so updates don't propagate. Run this script
periodically to refresh.

Strategy:
  - SELECT subjects from events table that need refresh (NULL story_points AND
    NULL sprint_id, OR --all flag).
  - Hit /rest/api/3/search/jql in batches of 50 with key-in JQL fetching
    customfield_10051 (Story Points) + customfield_10010 (Sprint array).
  - UPDATE the issue_created row in events table.
  - Sprint pick rule mirrors ingest/jira.py::_extract_sprint:
      prefer active → closed (highest id) → future (lowest id) → highest id.

Usage:
  ATLASSIAN_EMAIL=... ATLASSIAN_TOKEN=... .venv/bin/python ingest/backfill-jira-sprint-points.py
  ATLASSIAN_EMAIL=... ATLASSIAN_TOKEN=... .venv/bin/python ingest/backfill-jira-sprint-points.py --all
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from derive.sources_config import atlassian_host

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "index" / "events.db"
DOMAIN = atlassian_host()
BATCH = 50


def auth():
    email = os.environ.get("ATLASSIAN_EMAIL", "")
    token = os.environ.get("ATLASSIAN_TOKEN", "")
    if not email or not token:
        sys.exit("ATLASSIAN_EMAIL and ATLASSIAN_TOKEN required in env")
    return (email, token)


def chunked(seq: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _pick_sprint(sprints: list[dict]) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Mirror ingest/jira.py::_extract_sprint pick logic."""
    valid = [s for s in (sprints or []) if isinstance(s, dict) and s.get("id") is not None]
    if not valid:
        return None, None, None
    by_state: dict[str, list[dict]] = {"active": [], "closed": [], "future": []}
    for s in valid:
        by_state.setdefault(s.get("state", ""), []).append(s)
    if by_state.get("active"):
        pick = max(by_state["active"], key=lambda s: s["id"])
    elif by_state.get("closed"):
        pick = max(by_state["closed"], key=lambda s: s["id"])
    elif by_state.get("future"):
        pick = min(by_state["future"], key=lambda s: s["id"])
    else:
        pick = max(valid, key=lambda s: s["id"])
    return pick.get("id"), pick.get("name"), pick.get("state")


def fetch_fields(keys: list[str], creds) -> dict[str, dict]:
    """Hit /rest/api/3/search/jql with key-in JQL.
    Returns {key: {story_points, sprint_id, sprint_name, sprint_state}}.
    """
    jql = "key in (" + ",".join(keys) + ")"
    out: dict[str, dict] = {}
    next_token = None
    while True:
        body = {
            "jql": jql,
            "fields": ["customfield_10051", "customfield_10010"],
            "maxResults": len(keys),
        }
        if next_token:
            body["nextPageToken"] = next_token
        r = requests.post(
            f"https://{DOMAIN}/rest/api/3/search/jql",
            json=body,
            auth=creds,
            timeout=30,
        )
        if r.status_code == 429:
            time.sleep(5)
            continue
        r.raise_for_status()
        data = r.json()
        for issue in data.get("issues", []):
            k = issue.get("key")
            if not k:
                continue
            f = issue.get("fields") or {}
            sp = f.get("customfield_10051")
            try:
                sp_val = float(sp) if sp is not None else None
            except (TypeError, ValueError):
                sp_val = None
            sid, sname, sstate = _pick_sprint(f.get("customfield_10010") or [])
            out[k] = {
                "story_points": sp_val,
                "sprint_id": sid,
                "sprint_name": sname,
                "sprint_state": sstate,
            }
        next_token = data.get("nextPageToken")
        if not next_token or data.get("isLast"):
            break
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true",
                        help="Refresh all jira issue_created rows (default: only those with NULL story_points AND NULL sprint_id)")
    args = parser.parse_args()

    creds = auth()
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()

    if args.all:
        cur.execute("""
            SELECT DISTINCT subject FROM events
            WHERE source = 'jira'
              AND event_type = 'issue_created'
              AND subject IS NOT NULL
        """)
    else:
        cur.execute("""
            SELECT DISTINCT subject FROM events
            WHERE source = 'jira'
              AND event_type = 'issue_created'
              AND story_points IS NULL
              AND sprint_id IS NULL
              AND subject IS NOT NULL
        """)
    subjects = [r[0] for r in cur.fetchall()]
    print(f"backfill: {len(subjects)} jira subjects to process (--all={args.all})")
    if not subjects:
        return

    updated = 0
    nonzero_sp = 0
    nonzero_sprint = 0
    batches = list(chunked(subjects, BATCH))
    for i, batch in enumerate(batches, 1):
        print(f"  batch {i}/{len(batches)}: {len(batch)} keys", flush=True)
        try:
            mapping = fetch_fields(batch, creds)
        except Exception as e:
            print(f"    ERROR: {e}", file=sys.stderr)
            continue
        for k, vals in mapping.items():
            cur.execute(
                """UPDATE events
                   SET story_points = ?, sprint_id = ?, sprint_name = ?, sprint_state = ?
                   WHERE source = 'jira' AND subject = ? AND event_type = 'issue_created'""",
                (vals["story_points"], vals["sprint_id"],
                 vals["sprint_name"], vals["sprint_state"], k),
            )
            if cur.rowcount:
                updated += cur.rowcount
                if vals["story_points"] is not None:
                    nonzero_sp += 1
                if vals["sprint_id"] is not None:
                    nonzero_sprint += 1
        conn.commit()
        time.sleep(0.3)

    print(f"\nbackfill: updated {updated} rows ({nonzero_sp} with story_points, {nonzero_sprint} with sprint)")

    # Summary
    cur.execute("""
        SELECT sprint_state, COUNT(DISTINCT subject)
        FROM events WHERE source='jira' AND event_type='issue_created' AND sprint_state IS NOT NULL
        GROUP BY sprint_state ORDER BY 2 DESC
    """)
    rows = cur.fetchall()
    if rows:
        print("\nDistinct jira subjects by sprint_state:")
        for st, n in rows:
            print(f"  {st:10} {n}")

    cur.execute("""
        SELECT COUNT(*), ROUND(AVG(story_points), 2), MIN(story_points), MAX(story_points)
        FROM events
        WHERE source='jira' AND event_type='issue_created' AND story_points IS NOT NULL
    """)
    n, avg, mn, mx = cur.fetchone()
    if n:
        print(f"\nStory points: n={n}, avg={avg}, min={mn}, max={mx}")


if __name__ == "__main__":
    main()

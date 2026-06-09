"""One-shot (re-runnable) backfill: populate events.assignee for existing
jira issue_created rows.

Why: schema migration added `assignee` column with NULL default. Existing
issue_created rows ingested before the column existed have NULL assignee,
which breaks the assigned-only SP attribution rule in /narrative for tickets
that were never reassigned (no `assignment` changelog event → no fallback).

Strategy:
  - SELECT distinct subjects from events table that have NULL assignee on
    their issue_created row.
  - Hit /rest/api/3/search/jql in batches of 50 with key-in JQL fetching
    only `assignee` field.
  - Extract assignee.emailAddress (preferred) or accountId fallback.
  - UPDATE the issue_created row's assignee column in place.

Usage:
  ATLASSIAN_EMAIL=... ATLASSIAN_TOKEN=... .venv/bin/python ingest/backfill-jira-assignees.py
  ATLASSIAN_EMAIL=... ATLASSIAN_TOKEN=... .venv/bin/python ingest/backfill-jira-assignees.py --all
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from derive.sources_config import atlassian_host  # noqa: E402

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


def _user(u: Optional[dict]) -> Optional[str]:
    """Mirror ingest/jira.py::_user — return email preferred, accountId fallback."""
    if not u or not isinstance(u, dict):
        return None
    return u.get("emailAddress") or u.get("accountId")


def fetch_assignees(keys: list[str], creds) -> dict[str, Optional[str]]:
    """Hit /rest/api/3/search/jql for assignee field only.
    Returns {key: assignee_email_or_accountId_or_None}.
    """
    jql = "key in (" + ",".join(keys) + ")"
    out: dict[str, Optional[str]] = {}
    next_token = None
    while True:
        body = {"jql": jql, "fields": ["assignee"], "maxResults": len(keys)}
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
            assignee = (issue.get("fields") or {}).get("assignee")
            out[k] = _user(assignee)
        next_token = data.get("nextPageToken")
        if not next_token or data.get("isLast"):
            break
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true",
                        help="Refresh all jira issue_created rows (default: only those with NULL assignee)")
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
              AND assignee IS NULL
              AND subject IS NOT NULL
        """)
    subjects = [r[0] for r in cur.fetchall()]
    print(f"backfill: {len(subjects)} jira subjects to process (--all={args.all})")
    if not subjects:
        return

    updated = 0
    with_assignee = 0
    unassigned = 0
    batches = list(chunked(subjects, BATCH))
    for i, batch in enumerate(batches, 1):
        print(f"  batch {i}/{len(batches)}: {len(batch)} keys", flush=True)
        try:
            mapping = fetch_assignees(batch, creds)
        except Exception as e:
            print(f"    ERROR: {e}", file=sys.stderr)
            continue
        for k, ass in mapping.items():
            cur.execute(
                """UPDATE events SET assignee = ?
                   WHERE source = 'jira' AND subject = ? AND event_type = 'issue_created'""",
                (ass, k),
            )
            if cur.rowcount:
                updated += cur.rowcount
                if ass:
                    with_assignee += 1
                else:
                    unassigned += 1
        conn.commit()
        time.sleep(0.3)

    print(f"\nbackfill complete: updated {updated} rows ({with_assignee} with assignee, {unassigned} unassigned-at-creation)")

    # Sanity summary
    cur.execute("""SELECT assignee, COUNT(DISTINCT subject) FROM events
                   WHERE source='jira' AND event_type='issue_created' AND assignee IS NOT NULL
                   GROUP BY assignee ORDER BY 2 DESC LIMIT 15""")
    rows = cur.fetchall()
    if rows:
        print("\nTop 15 assignees (distinct subjects):")
        for ass, n in rows:
            print(f"  {(ass or 'None'):50} {n}")


if __name__ == "__main__":
    main()

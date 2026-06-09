"""One-shot backfill: populate events.issue_type for existing jira rows.

Why: schema migration added issue_type column with NULL default. Historical jira
rows ingested before the column existed have issue_type=NULL. This script hits
the Jira bulk-fetch API to fill it, no re-ingest of the whole event history.

Usage:
  ATLASSIAN_TOKEN=... ATLASSIAN_EMAIL=... .venv/bin/python ingest/backfill-jira-issue-type.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from derive.sources_config import atlassian_host

DB = ROOT / "index" / "events.db"
DOMAIN = atlassian_host()
BATCH = 50  # JQL "key in (...)" accepts up to ~100; 50 is conservative


def auth():
    email = os.environ.get("ATLASSIAN_EMAIL", "")
    token = os.environ.get("ATLASSIAN_TOKEN", "")
    if not email or not token:
        sys.exit("ATLASSIAN_EMAIL and ATLASSIAN_TOKEN required in env")
    return (email, token)


def chunked(seq: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_issue_types(keys: list[str], creds) -> dict[str, str]:
    """Hit /rest/api/3/search/jql with key-in JQL, return {key: issuetype.name}."""
    jql = "key in (" + ",".join(keys) + ")"
    out: dict[str, str] = {}
    next_token = None
    while True:
        body = {"jql": jql, "fields": ["issuetype"], "maxResults": len(keys)}
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
            it = ((issue.get("fields") or {}).get("issuetype") or {}).get("name")
            if k and it:
                out[k] = it
        next_token = data.get("nextPageToken")
        if not next_token or data.get("isLast"):
            break
    return out


def main() -> None:
    creds = auth()
    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT subject FROM events
        WHERE source = 'jira'
          AND event_type = 'issue_created'
          AND (issue_type IS NULL OR issue_type = '')
          AND subject IS NOT NULL
    """)
    subjects = [r[0] for r in cur.fetchall()]
    print(f"backfill: {len(subjects)} jira subjects need issue_type")
    if not subjects:
        return

    updated = 0
    batches = list(chunked(subjects, BATCH))
    for i, batch in enumerate(batches, 1):
        print(f"  batch {i}/{len(batches)}: {len(batch)} keys", flush=True)
        try:
            mapping = fetch_issue_types(batch, creds)
        except Exception as e:
            print(f"    ERROR: {e}", file=sys.stderr)
            continue
        for k, it in mapping.items():
            cur.execute(
                "UPDATE events SET issue_type=? WHERE source='jira' AND subject=?",
                (it, k),
            )
            updated += cur.rowcount
        conn.commit()
        time.sleep(0.3)  # polite pacing

    print(f"backfill: updated {updated} event rows with issue_type")

    # Summary by issue_type
    cur.execute("""
        SELECT issue_type, COUNT(DISTINCT subject)
        FROM events WHERE source='jira' AND event_type='issue_created'
        GROUP BY issue_type ORDER BY 2 DESC
    """)
    print("\nDistinct jira subjects by issue_type:")
    for it, n in cur.fetchall():
        print(f"  {it or '(null)':20} {n}")


if __name__ == "__main__":
    main()

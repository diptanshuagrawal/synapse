"""One-shot (re-runnable) backfill: synthesise page_created events for every
Confluence page in events DB that lacks one.

Why: regular ingest only captures page_created for pages whose version 1 was
visible in the cursor window. Pages that existed before ingest started have
no page_created event — so trd_owners scoring loses the authoritative "who
originated this page" signal.

Strategy:
  - SELECT distinct subject (page_id) from events where source='confluence'
    AND NOT EXISTS (corresponding page_created event)
  - For each, fetch /wiki/api/v2/pages/{id}?body-format=storage to get
    `createdAt` + `authorId`.
  - Resolve authorId via people.yaml jira_id → canonical (then find best
    matching actor string the rest of ingest uses; for Confluence ingest
    that's jira_id).
  - Synthesise an Event(event_type='page_created', actor=<jira_id>,
    subject='page:<id>', ts=<createdAt>, ...) via store_event.
  - store_event dedupes via INSERT OR IGNORE on event.id, so re-running is
    safe.

Usage:
    ATLASSIAN_EMAIL=... ATLASSIAN_TOKEN=... .venv/bin/python ingest/backfill-confluence-creators.py
    ATLASSIAN_EMAIL=... ATLASSIAN_TOKEN=... .venv/bin/python ingest/backfill-confluence-creators.py --verbose
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

# Allow `from ingest.common import ...` when run as a script
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ingest.common import Event, Refs, get_db, store_event, enrich_refs
from derive.sources_config import atlassian_host

DOMAIN = os.environ.get("JIRA_DOMAIN", atlassian_host())
TIMEOUT = 30


def auth() -> tuple[str, str]:
    email = os.environ.get("ATLASSIAN_EMAIL", "")
    token = os.environ.get("ATLASSIAN_TOKEN", "")
    if not email or not token:
        sys.exit("ATLASSIAN_EMAIL and ATLASSIAN_TOKEN required in env")
    return (email, token)


def fetch_page_meta(page_id: str, creds: tuple[str, str]) -> dict | None:
    """Hit Confluence v2 API for page createdAt + authorId.

    v2 endpoint: GET /wiki/api/v2/pages/{id}
    Returns {id, status, title, createdAt, authorId, version, ...}
    """
    url = f"https://{DOMAIN}/wiki/api/v2/pages/{page_id}"
    r = requests.get(url, auth=creds, headers={"Accept": "application/json"}, timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    if r.status_code == 429:
        time.sleep(5)
        return fetch_page_meta(page_id, creds)
    if r.status_code >= 400:
        return None
    return r.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Only process first N pages (0 = all)")
    args = parser.parse_args()

    creds = auth()
    conn = get_db()
    cur = conn.cursor()

    # Distinct page_ids that have any confluence event but no page_created.
    cur.execute(
        """
        SELECT DISTINCT subject FROM events
        WHERE source = 'confluence' AND subject LIKE 'page:%'
          AND subject NOT IN (
            SELECT subject FROM events
            WHERE source = 'confluence' AND event_type = 'page_created'
          )
        """
    )
    candidates = [r[0] for r in cur.fetchall()]
    if args.limit:
        candidates = candidates[: args.limit]
    print(f"backfill: {len(candidates)} pages need synthetic page_created event")
    if not candidates:
        return

    synth = 0
    not_found = 0
    no_author = 0
    errors = 0

    for i, subject in enumerate(candidates, 1):
        page_id = subject.replace("page:", "")
        if args.verbose:
            print(f"  [{i}/{len(candidates)}] page:{page_id}", end=" ", flush=True)
        try:
            meta = fetch_page_meta(page_id, creds)
        except Exception as e:
            errors += 1
            if args.verbose:
                print(f"ERR {e}", flush=True)
            continue
        if not meta:
            not_found += 1
            if args.verbose:
                print("404/err", flush=True)
            continue
        author_id = meta.get("authorId") or ""
        created_at = meta.get("createdAt") or ""
        title = meta.get("title") or ""
        if not author_id or not created_at:
            no_author += 1
            if args.verbose:
                print("no-author/no-ts", flush=True)
            continue

        # Build synthetic Event. actor = jira_id (the Atlassian account id).
        # Mirror ingest/confluence.py::normalize_page formatting.
        event = Event(
            id=f"confluence:{page_id}:created",   # same id format ingest would use
            source="confluence",
            event_type="page_created",
            ts=created_at,                          # already ISO 8601 from Confluence v2
            actor=author_id,
            subject=subject,
            title=title,
            body="",
            url=f"https://{DOMAIN}/wiki/spaces/_/pages/{page_id}",
        )
        enrich_refs(event, actor_field="jira_id")
        try:
            inserted = store_event(conn, event, dry_run=False)
        except Exception as e:
            errors += 1
            if args.verbose:
                print(f"INSERT-ERR {e}", flush=True)
            continue
        if inserted:
            synth += 1
            if args.verbose:
                print(f"OK ts={created_at[:10]} author={author_id[:30]}", flush=True)
        else:
            if args.verbose:
                print("dup (already had page_created)", flush=True)
        time.sleep(0.15)  # polite pacing
    conn.commit()

    print(
        f"\nbackfill complete: synth={synth} not_found={not_found} no_author={no_author} errors={errors}"
    )


if __name__ == "__main__":
    main()

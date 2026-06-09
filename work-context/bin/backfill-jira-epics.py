#!/usr/bin/env python3
"""
One-shot backfill: prefix existing Jira event titles with `[Epic EX-XXX]`.

Skips events whose title already starts with `[Epic `.
Rebuilds events_fts after the UPDATE.
"""
from __future__ import annotations
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from ingest.jira import JiraClient, get_epic_key  # noqa: E402
from derive.sources_config import atlassian_host, owner_email  # noqa: E402

TOKEN_FILE = Path.home() / ".secrets/atlassian_token"
EMAIL_FILE = Path.home() / ".secrets/atlassian_email"
DEFAULT_EMAIL = owner_email()
DOMAIN = atlassian_host()
DB = ROOT / "index/events.db"

BATCH_SIZE = 50  # Jira `key in (...)` accepts ~100 keys; keep margin


def load_creds() -> tuple[str, str]:
    if not TOKEN_FILE.exists():
        print(f"ERROR: {TOKEN_FILE} missing", file=sys.stderr)
        sys.exit(1)
    token = TOKEN_FILE.read_text().strip()
    email = EMAIL_FILE.read_text().strip() if EMAIL_FILE.exists() else DEFAULT_EMAIL
    return email, token


def main() -> None:
    email, token = load_creds()
    client = JiraClient(DOMAIN, email, token)

    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()

    # Distinct issue keys with un-prefixed titles
    cur.execute("""
        SELECT DISTINCT subject FROM events
        WHERE source = 'jira'
          AND subject IS NOT NULL AND subject != ''
          AND title NOT LIKE '[Epic %'
    """)
    keys = [r[0] for r in cur.fetchall()]
    print(f"Distinct Jira issues without epic prefix: {len(keys)}")

    epic_by_key: dict[str, str] = {}
    fields = ["summary", "parent", "issuetype", "customfield_10014"]

    for i in range(0, len(keys), BATCH_SIZE):
        batch = keys[i:i + BATCH_SIZE]
        keys_csv = ",".join(batch)
        jql = f"key in ({keys_csv})"
        try:
            for issue in client.search_issues(jql, fields, expand=[]):
                k = issue["key"]
                epic = get_epic_key(issue)
                if epic:
                    epic_by_key[k] = epic
        except Exception as e:
            print(f"  batch {i // BATCH_SIZE} failed: {e}", file=sys.stderr)
            continue
        print(f"  fetched batch {i // BATCH_SIZE + 1} / {(len(keys) + BATCH_SIZE - 1) // BATCH_SIZE}; epics resolved so far: {len(epic_by_key)}")

    print(f"\nEpic mappings resolved: {len(epic_by_key)} / {len(keys)} issues")

    if not epic_by_key:
        print("No epic mappings found — nothing to backfill.")
        conn.close()
        return

    updated = 0
    for issue_key, epic_key in epic_by_key.items():
        prefix = f"[Epic {epic_key}] "
        cur.execute("""
            UPDATE events
            SET title = ? || title
            WHERE source = 'jira'
              AND subject = ?
              AND title NOT LIKE '[Epic %'
        """, (prefix, issue_key))
        updated += cur.rowcount
    conn.commit()
    print(f"Updated {updated} events")

    print("Rebuilding events_fts...")
    cur.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
    conn.commit()
    print("Done.")
    conn.close()


if __name__ == "__main__":
    main()

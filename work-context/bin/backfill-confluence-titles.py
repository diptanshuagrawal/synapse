#!/usr/bin/env python3
"""
One-shot backfill: rewrite confluence comment event titles with real page titles.

Old format: "inline comment on page EXAMPLE_PAGE_ID"
New format: "inline comment on 'service-a instant-pay Contract' (page EXAMPLE_PAGE_ID)"

Reads $HOME/.secrets/atlassian_{email,token}; one API call per unique page.
Rebuilds events_fts after the UPDATE.
"""
from __future__ import annotations
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from ingest.confluence import ConfluenceClient  # noqa: E402
from derive.sources_config import atlassian_host, owner_email  # noqa: E402

TOKEN_FILE = Path.home() / ".secrets/atlassian_token"
EMAIL_FILE = Path.home() / ".secrets/atlassian_email"
DEFAULT_EMAIL = owner_email()
DOMAIN = atlassian_host()
DB = ROOT / "index/events.db"


def load_creds() -> tuple[str, str]:
    if not TOKEN_FILE.exists():
        print(f"ERROR: {TOKEN_FILE} missing", file=sys.stderr)
        sys.exit(1)
    token = TOKEN_FILE.read_text().strip()
    email = EMAIL_FILE.read_text().strip() if EMAIL_FILE.exists() else DEFAULT_EMAIL
    return email, token


def main() -> None:
    email, token = load_creds()
    client = ConfluenceClient(DOMAIN, email, token)

    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()

    cur.execute("""
        SELECT id, subject, title FROM events
        WHERE source = 'confluence' AND event_type = 'comment'
          AND title LIKE '% comment on page %'
    """)
    rows = cur.fetchall()
    print(f"Candidate events: {len(rows)}")

    by_page: dict[str, list[tuple[str, str]]] = {}
    for eid, subj, old_title in rows:
        page_id = subj.split(":", 1)[1] if ":" in (subj or "") else ""
        if not page_id:
            continue
        by_page.setdefault(page_id, []).append((eid, old_title))
    print(f"Unique pages: {len(by_page)}")

    updated = 0
    skipped = 0
    for i, (page_id, events) in enumerate(by_page.items(), start=1):
        title = client.get_page_title(page_id)
        if not title:
            skipped += 1
            print(f"  [{i}/{len(by_page)}] skip {page_id} (no title)")
            continue
        for eid, old_title in events:
            kind = "footer" if old_title.startswith("footer ") else "inline"
            new_title = f"{kind} comment on '{title}' (page {page_id})"
            cur.execute("UPDATE events SET title = ? WHERE id = ?", (new_title, eid))
            updated += 1
        print(f"  [{i}/{len(by_page)}] ✓ {page_id}: {title[:60]} ({len(events)} events)")
        if i % 50 == 0:
            conn.commit()
        time.sleep(0.05)  # gentle pacing

    conn.commit()
    print(f"\nUpdated {updated} events, skipped {skipped} pages")

    print("Rebuilding events_fts...")
    cur.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
    conn.commit()
    print("Done.")
    conn.close()


if __name__ == "__main__":
    main()

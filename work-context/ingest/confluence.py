"""
Confluence ingest script.

Polls all accessible Confluence spaces for pages + comments updated by team members
(authors/contributors found in config/people.yaml).

Emits unified events: page_created, page_updated, comment.

Usage:
    python ingest/confluence.py [--dry-run] [--space SPACE_KEY] [--reset-cursor]

Env:
    ATLASSIAN_EMAIL   — required.
    ATLASSIAN_TOKEN   — required.
    JIRA_DOMAIN       — optional. Defaults to your-org.atlassian.net (Confluence shares the same host /wiki).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from ingest.common import (
    Event,
    _load_people,
    enrich_refs,
    get_db,
    read_cursor,
    store_event,
    write_cursor,
    write_success_date,
)
from derive.sources_config import atlassian_host

ROOT = Path(__file__).parent.parent

DEFAULT_DOMAIN = atlassian_host()


def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    return logging.getLogger("confluence-ingest")


# ── client ───────────────────────────────────────────────────────────────────

class ConfluenceClient:
    def __init__(self, domain: str, email: str, token: str) -> None:
        self.base = f"https://{domain}/wiki"
        self.session = requests.Session()
        self.session.auth = (email, token)
        self.session.headers.update({"Accept": "application/json"})
        self._page_title_cache: dict[str, str] = {}

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = self.session.get(f"{self.base}{path}", params=params, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "30"))
            logging.getLogger("confluence-ingest").warning("Rate limited. Sleeping %ds.", wait)
            time.sleep(wait)
            resp = self.session.get(f"{self.base}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_page_title(self, page_id: str) -> str:
        """Fetch (and cache for run) page title. Returns empty string on 404/error."""
        if not page_id:
            return ""
        if page_id in self._page_title_cache:
            return self._page_title_cache[page_id]
        try:
            resp = self.session.get(f"{self.base}/api/v2/pages/{page_id}",
                                    params={"body-format": "storage"}, timeout=15)
            if resp.status_code == 200:
                title = (resp.json() or {}).get("title") or ""
            else:
                title = ""
        except Exception:
            title = ""
        self._page_title_cache[page_id] = title
        return title

    def paginate(self, path: str, params: Optional[dict] = None) -> Iterable[dict]:
        """v2 API uses _links.next for pagination."""
        next_path = path
        next_params = dict(params or {})
        next_params.setdefault("limit", 100)
        first = True
        while next_path:
            data = self.get(next_path, next_params if first else None)
            for item in data.get("results", []):
                yield item
            next_link = (data.get("_links") or {}).get("next")
            if not next_link:
                break
            # next_link is a path like "/api/v2/pages?cursor=...&limit=100"
            # Strip "/wiki" prefix if present
            if next_link.startswith("/wiki"):
                next_link = next_link[len("/wiki"):]
            next_path = next_link
            first = False


# ── normalizers ──────────────────────────────────────────────────────────────

def _ts(s: Optional[str]) -> str:
    if not s:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if "+" in s[10:] and s[-5].isdigit() and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return s


def load_team_account_ids() -> set[str]:
    """All Atlassian accountIds from people.yaml — used to filter authors."""
    return {(p.get("jira_id") or "").strip() for p in _load_people() if p.get("jira_id")}


def normalize_page(domain: str, page: dict, is_first_version: bool) -> Event:
    page_id = str(page["id"])
    title = page.get("title") or ""
    version = page.get("version") or {}
    # version.authorId = who made this specific edit; page.authorId = original creator
    author_id = (version.get("authorId") or page.get("authorId") or "").strip()
    ts_str = version.get("createdAt") or page.get("createdAt")
    body_storage = (page.get("body") or {}).get("storage") or {}
    body = body_storage.get("value") or ""

    event_type = "page_created" if is_first_version else "page_updated"
    url = f"https://{domain}/wiki/spaces/{page.get('spaceId', '')}/pages/{page_id}"

    event = Event(
        id=f"confluence:page:{page_id}:v{version.get('number', 1)}",
        source="confluence",
        event_type=event_type,
        ts=_ts(ts_str),
        actor=author_id,  # Atlassian accountId — resolve via jira_id field
        subject=f"page:{page_id}",
        title=title,
        body=body[:5000],  # cap to avoid massive blobs
        url=url,
    )
    enrich_refs(event, actor_field="jira_id")
    return event


def normalize_comment(domain: str, comment: dict, kind: str, page_title: str = "") -> Event:
    comment_id = str(comment["id"])
    parent = comment.get("pageId") or comment.get("blogPostId") or "?"
    version = comment.get("version") or {}
    author_id = (version.get("authorId") or comment.get("authorId") or "").strip()
    body_atlas = (comment.get("body") or {}).get("atlas_doc_format") or (comment.get("body") or {}).get("storage") or {}
    body = body_atlas.get("value") or ""
    ts_str = version.get("createdAt") or comment.get("createdAt")

    if page_title:
        title = f"{kind} comment on '{page_title}' (page {parent})"
    else:
        title = f"{kind} comment on page {parent}"

    event = Event(
        id=f"confluence:comment:{kind}:{comment_id}",
        source="confluence",
        event_type="comment",
        ts=_ts(ts_str),
        actor=author_id,
        subject=f"page:{parent}",
        title=title,
        body=body[:5000],
        url=f"https://{domain}/wiki/pages/viewpage.action?pageId={parent}",
    )
    enrich_refs(event, actor_field="jira_id")
    return event


# ── ingest ───────────────────────────────────────────────────────────────────

def ingest_pages(client: ConfluenceClient, domain: str, since: Optional[str],
                 team_ids: set[str], conn, dry_run: bool, log: logging.Logger) -> tuple[int, int]:
    """Walk all pages, emit page_created/page_updated for team-authored versions only."""
    new_count = 0
    dup_count = 0

    # v2 pages endpoint — sort by modified date descending
    log.info("Fetching pages (filter: team authors only)")
    page_count = 0

    # Body format = storage gives us HTML/storage XML; cheaper than atlas_doc_format
    params = {"body-format": "storage", "limit": 100, "sort": "-modified-date"}

    for page in client.paginate("/api/v2/pages", params):
        page_count += 1
        author_id = (page.get("authorId") or "").strip()
        version = page.get("version") or {}
        ts_str = version.get("createdAt") or page.get("createdAt")
        ts_norm = _ts(ts_str)

        # Stop once we hit pages older than cursor
        if since and ts_norm < since:
            log.info("  reached cursor at %d pages", page_count)
            break

        # Filter: only team members
        if author_id not in team_ids:
            continue

        is_first = (version.get("number", 1) == 1)
        ev = normalize_page(domain, page, is_first)
        if store_event(conn, ev, dry_run=dry_run):
            new_count += 1
            if dry_run:
                log.info("  [DRY] %s %s", ev.event_type, ev.title[:60])
        else:
            dup_count += 1

    log.info("  %d pages scanned, %d new, %d dup", page_count, new_count, dup_count)
    return new_count, dup_count


def ingest_comments(client: ConfluenceClient, domain: str, since: Optional[str],
                    team_ids: set[str], conn, dry_run: bool, log: logging.Logger) -> tuple[int, int]:
    """Walk footer + inline comments, emit team-authored only."""
    new_count = 0
    dup_count = 0

    for kind, path in [("footer", "/api/v2/footer-comments"), ("inline", "/api/v2/inline-comments")]:
        log.info("Fetching %s comments", kind)
        c_count = 0
        params = {"body-format": "storage", "limit": 100, "sort": "-created-date"}

        try:
            for c in client.paginate(path, params):
                c_count += 1
                version = c.get("version") or {}
                author_id = (version.get("authorId") or c.get("authorId") or "").strip()
                ts_str = version.get("createdAt") or c.get("createdAt")
                ts_norm = _ts(ts_str)

                if since and ts_norm < since:
                    log.info("  %s: reached cursor at %d comments", kind, c_count)
                    break
                if author_id not in team_ids:
                    continue

                page_id = str(c.get("pageId") or c.get("blogPostId") or "")
                page_title = client.get_page_title(page_id) if page_id else ""
                ev = normalize_comment(domain, c, kind, page_title=page_title)
                if store_event(conn, ev, dry_run=dry_run):
                    new_count += 1
                    if dry_run:
                        log.info("  [DRY] %s %s", ev.event_type, ev.subject)
                else:
                    dup_count += 1
        except Exception as e:
            log.warning("  %s comments fetch failed: %s", kind, e)

    return new_count, dup_count


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Confluence ingest")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reset-cursor", action="store_true")
    args = parser.parse_args()

    email = os.environ.get("ATLASSIAN_EMAIL")
    token = os.environ.get("ATLASSIAN_TOKEN")
    domain = os.environ.get("JIRA_DOMAIN", DEFAULT_DOMAIN)
    if not email or not token:
        print("ERROR: ATLASSIAN_EMAIL and ATLASSIAN_TOKEN must be set", file=sys.stderr)
        sys.exit(1)

    log = setup_logging()
    cursor_key = "confluence"

    since = None if args.reset_cursor else read_cursor(cursor_key)
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    team_ids = load_team_account_ids()
    log.info("Confluence ingest starting. domain=%s since=%s team_ids=%d dry_run=%s",
             domain, since, len(team_ids), args.dry_run)
    if not team_ids:
        log.warning("No jira_id values in people.yaml — nothing will match")

    conn = get_db()
    client = ConfluenceClient(domain, email, token)
    total_new = 0
    total_dup = 0
    n_failed = 0
    last_err = ""
    TOTAL_STAGES = 2  # pages + comments

    try:
        n, d = ingest_pages(client, domain, since, team_ids, conn, args.dry_run, log)
        total_new += n; total_dup += d
    except Exception as e:
        last_err = str(e)
        log.error("Pages ingest failed: %s", e)
        n_failed += 1

    try:
        n, d = ingest_comments(client, domain, since, team_ids, conn, args.dry_run, log)
        total_new += n; total_dup += d
    except Exception as e:
        last_err = str(e)
        log.error("Comments ingest failed: %s", e)
        n_failed += 1

    if not args.dry_run and n_failed == 0:
        write_cursor(cursor_key, now_ts)
        if not args.reset_cursor:
            write_success_date("confluence")
        log.info("Cursor updated to %s", now_ts)
    elif n_failed == TOTAL_STAGES:
        log.error("Cursor NOT updated — ALL %d stages failed (last error: %s)",
                  TOTAL_STAGES, last_err[:200])
    elif n_failed > 0:
        log.warning("Cursor NOT updated — %d of %d stages failed (last error: %s)",
                    n_failed, TOTAL_STAGES, last_err[:200])

    log.info("Done. source=confluence total_new=%d total_dup=%d", total_new, total_dup)

    if n_failed == TOTAL_STAGES:
        sys.exit(2)
    if n_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

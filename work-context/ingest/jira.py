"""
Jira ingest script.

Polls configured Jira projects for issues updated since the last cursor.
Emits unified events: issue_created, status_change, comment, assignment.

Usage:
    python ingest/jira.py [--dry-run] [--project EX] [--reset-cursor]

Env:
    ATLASSIAN_EMAIL   — required. Email for basic auth (e.g., owner@example.com).
    ATLASSIAN_TOKEN   — required. API token from https://id.atlassian.com/manage-profile/security/api-tokens.
    JIRA_DOMAIN       — optional. Overrides atlassian.host from config/sources.yaml.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from ingest.common import (
    Event,
    enrich_refs,
    get_db,
    read_cursor,
    store_event,
    write_cursor,
    write_success_date,
)
from derive.identity_signals import (
    init as init_identity_signals,
    record_user_dict,
)
from derive.sources_config import atlassian_host, jira_project_keys

ROOT = Path(__file__).parent.parent

DEFAULT_PROJECTS = jira_project_keys()
DEFAULT_DOMAIN = atlassian_host()

# ── logging ──────────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    return logging.getLogger("jira-ingest")


# ── client ───────────────────────────────────────────────────────────────────

class JiraClient:
    def __init__(self, domain: str, email: str, token: str) -> None:
        self.base = f"https://{domain}"
        self.session = requests.Session()
        self.session.auth = (email, token)
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = self.session.get(f"{self.base}{path}", params=params, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "30"))
            logging.getLogger("jira-ingest").warning("Rate limited. Sleeping %ds.", wait)
            time.sleep(wait)
            resp = self.session.get(f"{self.base}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, body: dict) -> dict:
        resp = self.session.post(f"{self.base}{path}", json=body, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "30"))
            logging.getLogger("jira-ingest").warning("Rate limited. Sleeping %ds.", wait)
            time.sleep(wait)
            resp = self.session.post(f"{self.base}{path}", json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def search_issues(self, jql: str, fields: list[str], expand: list[str]):
        """Yield issues from /rest/api/3/search/jql; fall back to legacy /search."""
        # Try newer endpoint first (token-paginated)
        try:
            next_token: Optional[str] = None
            first = True
            while True:
                body: dict = {"jql": jql, "fields": fields, "expand": ",".join(expand), "maxResults": 100}
                if next_token:
                    body["nextPageToken"] = next_token
                data = self.post("/rest/api/3/search/jql", body)
                for issue in data.get("issues", []):
                    yield issue
                next_token = data.get("nextPageToken")
                if not next_token:
                    return
                first = False
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise
            # Fall through to legacy endpoint
            logging.getLogger("jira-ingest").info("Falling back to legacy /search endpoint")

        # Legacy endpoint: startAt + maxResults
        start_at = 0
        while True:
            body = {
                "jql": jql,
                "fields": fields,
                "expand": expand,
                "startAt": start_at,
                "maxResults": 100,
            }
            data = self.post("/rest/api/3/search", body)
            issues = data.get("issues", [])
            for issue in issues:
                yield issue
            start_at += len(issues)
            if start_at >= data.get("total", 0) or not issues:
                break

    def issue_comments(self, issue_key: str):
        """Yield comments for an issue (paginated)."""
        start_at = 0
        while True:
            data = self.get(
                f"/rest/api/3/issue/{issue_key}/comment",
                {"startAt": start_at, "maxResults": 100, "expand": "renderedBody"},
            )
            comments = data.get("comments", [])
            for c in comments:
                yield c
            start_at += len(comments)
            if start_at >= data.get("total", 0) or not comments:
                break


# ── normalizers ──────────────────────────────────────────────────────────────

def _ts(s: Optional[str]) -> str:
    if not s:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # Jira: "2026-05-08T11:23:45.123+0530"
    if "+" in s[10:] and s[-5].isdigit():
        # Convert "+0530" → "+05:30"
        s = s[:-2] + ":" + s[-2:]
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return s


def _user(u: Optional[dict]) -> Optional[str]:
    """Prefer email, fall back to displayName."""
    if not u:
        return None
    return u.get("emailAddress") or u.get("displayName") or u.get("accountId")


def _flatten_adf(node) -> str:
    """Flatten Atlassian Document Format JSON to plain text."""
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return "".join(_flatten_adf(c) for c in node.get("content", []))
    if isinstance(node, list):
        return "".join(_flatten_adf(c) for c in node)
    return ""


def _extract_sprint(f: dict) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Pick the most relevant sprint from customfield_10010 array.

    Strategy: prefer active state; else closed with highest id (most recent past);
    else future with lowest id (nearest upcoming). Returns (id, name, state).
    """
    sprints = f.get("customfield_10010") or []
    if not isinstance(sprints, list) or not sprints:
        return None, None, None
    valid = [s for s in sprints if isinstance(s, dict) and s.get("id") is not None]
    if not valid:
        return None, None, None
    by_state = {"active": [], "closed": [], "future": []}
    for s in valid:
        by_state.setdefault(s.get("state", ""), []).append(s)
    pick = None
    if by_state.get("active"):
        pick = max(by_state["active"], key=lambda s: s["id"])
    elif by_state.get("closed"):
        pick = max(by_state["closed"], key=lambda s: s["id"])
    elif by_state.get("future"):
        pick = min(by_state["future"], key=lambda s: s["id"])
    else:
        pick = max(valid, key=lambda s: s["id"])
    return pick.get("id"), pick.get("name"), pick.get("state")


def _extract_story_points(f: dict) -> Optional[float]:
    v = f.get("customfield_10051")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_epic_key(issue: dict) -> str:
    """Extract Epic key from a Jira issue.

    Two cases:
      - Next-gen projects: parent field with issuetype Epic
      - Classic projects: customfield_10014 (Epic Link)
    """
    f = issue.get("fields", {})
    parent = f.get("parent") or {}
    parent_key = parent.get("key")
    if parent_key:
        parent_type = ((parent.get("fields") or {}).get("issuetype") or {}).get("name", "")
        if parent_type.lower() == "epic":
            return parent_key
    # Classic Epic Link field
    epic_link = f.get("customfield_10014")
    if isinstance(epic_link, str):
        return epic_link
    if isinstance(epic_link, dict):
        return epic_link.get("key", "") or ""
    return ""


def _prefix_epic(title: str, epic_key: str) -> str:
    if epic_key and not title.startswith("[Epic "):
        return f"[Epic {epic_key}] {title}"
    return title


def normalize_issue_created(domain: str, issue: dict) -> Event:
    key = issue["key"]
    f = issue.get("fields", {})
    creator = f.get("creator") or f.get("reporter")
    description = f.get("description")
    body = _flatten_adf(description) if isinstance(description, dict) else (description or "")
    epic_key = get_epic_key(issue)
    issue_type = (f.get("issuetype") or {}).get("name") or None
    sprint_id, sprint_name, sprint_state = _extract_sprint(f)
    assignee_email = _user(f.get("assignee"))   # None if unassigned at creation
    # Capture initial status (typically "Backlog" / "To Do") so downstream
    # consumers can resolve current status by ORDER BY ts DESC LIMIT 1.
    initial_status = (f.get("status") or {}).get("name") or None
    event = Event(
        id=f"jira:{key}:created",
        source="jira",
        event_type="issue_created",
        ts=_ts(f.get("created")),
        actor=_user(creator),
        subject=key,
        title=_prefix_epic(f.get("summary") or "", epic_key),
        body=body,
        url=f"https://{domain}/browse/{key}",
        issue_type=issue_type,
        story_points=_extract_story_points(f),
        sprint_id=sprint_id,
        sprint_name=sprint_name,
        sprint_state=sprint_state,
        assignee=assignee_email,
        to_status=initial_status,
    )
    enrich_refs(event, actor_field="email")
    return event


def normalize_changelog_entry(domain: str, key: str, history: dict, epic_key: str = "") -> list[Event]:
    """One changelog history may have multiple items — emit one event per status/assignee change."""
    events: list[Event] = []
    author = history.get("author") or {}
    actor = _user(author)
    ts = _ts(history.get("created"))
    history_id = history.get("id", "")
    url = f"https://{domain}/browse/{key}"

    for idx, item in enumerate(history.get("items", [])):
        field = item.get("field")
        if field == "status":
            # Capture the new-status string so consumers can query current
            # state without parsing the title. Falls back to None if Jira
            # didn't send a toString (extremely rare).
            new_status = item.get("toString") or None
            events.append(Event(
                id=f"jira:{key}:status:{history_id}:{idx}",
                source="jira",
                event_type="status_change",
                ts=ts,
                actor=actor,
                subject=key,
                title=_prefix_epic(f"status: {item.get('fromString') or '∅'} → {item.get('toString') or '∅'}", epic_key),
                body="",
                url=url,
                to_status=new_status,
            ))
        elif field == "assignee":
            events.append(Event(
                id=f"jira:{key}:assignee:{history_id}:{idx}",
                source="jira",
                event_type="assignment",
                ts=ts,
                actor=actor,
                subject=key,
                title=_prefix_epic(f"assignee: {item.get('fromString') or '∅'} → {item.get('toString') or '∅'}", epic_key),
                body="",
                url=url,
            ))
        elif field == "Sprint":
            # fromString / toString are comma-separated sprint names (full set both sides).
            # Compute set diff for human-readable delta.
            from_set = {s.strip() for s in (item.get("fromString") or "").split(",") if s.strip()}
            to_set = {s.strip() for s in (item.get("toString") or "").split(",") if s.strip()}
            added = sorted(to_set - from_set)
            removed = sorted(from_set - to_set)
            if not added and not removed:
                continue
            delta_parts = []
            if added:
                delta_parts.append("+" + ",".join(added))
            if removed:
                delta_parts.append("-" + ",".join(removed))
            events.append(Event(
                id=f"jira:{key}:sprint:{history_id}:{idx}",
                source="jira",
                event_type="sprint_change",
                ts=ts,
                actor=actor,
                subject=key,
                title=_prefix_epic(f"sprint: {' '.join(delta_parts)}", epic_key),
                body=f"from: {item.get('fromString') or '∅'}\nto: {item.get('toString') or '∅'}",
                url=url,
            ))
        elif field == "Story Points":
            events.append(Event(
                id=f"jira:{key}:storypoints:{history_id}:{idx}",
                source="jira",
                event_type="story_points_change",
                ts=ts,
                actor=actor,
                subject=key,
                title=_prefix_epic(f"story_points: {item.get('fromString') or '∅'} → {item.get('toString') or '∅'}", epic_key),
                body="",
                url=url,
            ))
    for e in events:
        enrich_refs(e, actor_field="email")
    return events


def normalize_comment(domain: str, key: str, comment: dict, epic_key: str = "") -> Event:
    rendered = comment.get("renderedBody") or ""
    body = _flatten_adf(comment.get("body")) if isinstance(comment.get("body"), dict) else (comment.get("body") or rendered)
    event = Event(
        id=f"jira:{key}:comment:{comment['id']}",
        source="jira",
        event_type="comment",
        ts=_ts(comment.get("created")),
        actor=_user(comment.get("author")),
        subject=key,
        title=_prefix_epic(f"Comment on {key}", epic_key),
        body=body,
        url=f"https://{domain}/browse/{key}?focusedCommentId={comment['id']}",
    )
    enrich_refs(event, actor_field="email")
    return event


# ── per-project ingest ───────────────────────────────────────────────────────

def ingest_project(client: JiraClient, domain: str, project: str, since: Optional[str],
                   conn, dry_run: bool, log: logging.Logger) -> tuple[int, int]:
    new_count = 0
    dup_count = 0

    # JQL: updated since cursor (or all if no cursor)
    jql = f"project = {project}"
    if since:
        # Convert ISO Z → Jira format "yyyy-MM-dd HH:mm" (UTC)
        try:
            dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            jql += f' AND updated >= "{dt.strftime("%Y-%m-%d %H:%M")}"'
        except ValueError:
            pass
    jql += " ORDER BY updated ASC"

    log.info("Jira project=%s jql=%r", project, jql)
    fields = ["summary", "description", "created", "updated", "creator", "reporter",
              "assignee", "status", "parent", "issuetype",
              "customfield_10014",   # Epic Link (classic projects)
              "customfield_10051",   # Story Points
              "customfield_10010"]   # Sprint (array of {id, name, state})
    issues_fetched = 0

    for issue in client.search_issues(jql, fields, expand=["changelog"]):
        issues_fetched += 1
        key = issue["key"]
        f = issue.get("fields", {})
        created_ts = _ts(f.get("created"))
        epic_key = get_epic_key(issue)

        # Capture identity signals from every user-shaped object on the issue.
        for u in (f.get("creator"), f.get("reporter"), f.get("assignee")):
            record_user_dict(conn, "jira", u)
        for history in issue.get("changelog", {}).get("histories", []):
            record_user_dict(conn, "jira", history.get("author"))

        # Issue created event (only if created since cursor)
        if not since or created_ts >= since:
            ev = normalize_issue_created(domain, issue)
            if store_event(conn, ev, dry_run=dry_run):
                new_count += 1
                if dry_run:
                    log.info("  [DRY] %s %s", ev.event_type, ev.subject)
            else:
                dup_count += 1

        # Changelog events (status, assignee) — filter by since
        for history in issue.get("changelog", {}).get("histories", []):
            h_ts = _ts(history.get("created"))
            if since and h_ts < since:
                continue
            for ev in normalize_changelog_entry(domain, key, history, epic_key=epic_key):
                if store_event(conn, ev, dry_run=dry_run):
                    new_count += 1
                    if dry_run:
                        log.info("  [DRY] %s %s %s", ev.event_type, ev.subject, ev.title)
                else:
                    dup_count += 1

        # Comments — fetch separately, filter by created since
        try:
            for c in client.issue_comments(key):
                c_ts = _ts(c.get("created"))
                record_user_dict(conn, "jira", c.get("author"))
                if since and c_ts < since:
                    continue
                ev = normalize_comment(domain, key, c, epic_key=epic_key)
                if store_event(conn, ev, dry_run=dry_run):
                    new_count += 1
                    if dry_run:
                        log.info("  [DRY] %s %s", ev.event_type, ev.subject)
                else:
                    dup_count += 1
        except Exception as e:
            log.warning("  Failed to fetch comments for %s: %s", key, e)

    log.info("  %d issues processed", issues_fetched)
    return new_count, dup_count


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Jira ingest")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--project", action="append", dest="projects",
                        help="Override project list (repeatable)")
    parser.add_argument("--reset-cursor", action="store_true")
    args = parser.parse_args()

    email = os.environ.get("ATLASSIAN_EMAIL")
    token = os.environ.get("ATLASSIAN_TOKEN")
    domain = os.environ.get("JIRA_DOMAIN", DEFAULT_DOMAIN)
    if not email or not token:
        print("ERROR: ATLASSIAN_EMAIL and ATLASSIAN_TOKEN must be set", file=sys.stderr)
        sys.exit(1)

    log = setup_logging()
    projects = args.projects or DEFAULT_PROJECTS
    cursor_key = "jira"

    since = None if args.reset_cursor else read_cursor(cursor_key)
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    log.info("Jira ingest starting. domain=%s projects=%s since=%s dry_run=%s",
             domain, projects, since, args.dry_run)

    conn = get_db()
    init_identity_signals(conn)
    client = JiraClient(domain, email, token)
    total_new = 0
    total_dup = 0
    n_failed = 0
    last_err = ""

    for project in projects:
        try:
            new, dup = ingest_project(client, domain, project, since, conn, args.dry_run, log)
            total_new += new
            total_dup += dup
            log.info("Project %s: %d new, %d duplicates", project, new, dup)
        except Exception as e:
            last_err = str(e)
            log.error("Project %s failed: %s", project, e)
            n_failed += 1

    if not args.dry_run and n_failed == 0:
        write_cursor(cursor_key, now_ts)
        if not args.reset_cursor:
            write_success_date("jira")
        log.info("Cursor updated to %s", now_ts)
    elif n_failed == len(projects):
        log.error("Cursor NOT updated — ALL %d projects failed (last error: %s)",
                  len(projects), last_err[:200])
    elif n_failed > 0:
        log.warning("Cursor NOT updated — %d of %d projects failed (last error: %s)",
                    n_failed, len(projects), last_err[:200])

    log.info("Done. source=jira total_new=%d total_dup=%d", total_new, total_dup)

    if n_failed == len(projects) and projects:
        sys.exit(2)
    if n_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

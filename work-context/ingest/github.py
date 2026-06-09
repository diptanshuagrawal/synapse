"""
GitHub ingest script.

Polls configured repos for PRs, PR reviews, PR comments, and commits
since the last cursor. Normalizes to unified Event schema, enriches refs,
writes to raw JSONL and SQLite index.

Usage:
    python ingest/github.py [--dry-run] [--repo example-org/service-a] [--reset-cursor] [--include-diffs]

Env:
    GITHUB_TOKEN   — required. Personal access token with repo read scope.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# Ensure parent dir is on path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from ingest.common import (
    Event,
    Refs,
    _load_people,
    enrich_refs,
    get_db,
    read_cursor,
    store_event,
    write_cursor,
    write_success_date,
)
from derive.identity_signals import (
    init as init_identity_signals,
    record_signal,
)
from derive.sources_config import github_repos


def _record_commit_signals(conn, commit: dict) -> None:
    """Emit github identity pairs from a commit payload.

    Pairs captured:
        (github_login, email)   — from commit.author.login + commit.commit.author.email
        (github_login, name)    — login + git author name
        (email, name)            — git author email + name
    """
    if not commit:
        return
    gh_user = commit.get("author") or {}
    git_author = (commit.get("commit") or {}).get("author") or {}
    login = gh_user.get("login")
    email = git_author.get("email")
    name = git_author.get("name")
    try:
        if login and email:
            record_signal(conn, "github", "github", login, "email", email)
        if login and name:
            record_signal(conn, "github", "github", login, "git_name", name)
        if email and name:
            record_signal(conn, "github", "email", email, "git_name", name)
    except Exception:
        pass  # never let signal capture break ingest

ROOT = Path(__file__).parent.parent

DEFAULT_REPOS = github_repos()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return logging.getLogger("github-ingest")


# ---------------------------------------------------------------------------
# GitHub API client
# ---------------------------------------------------------------------------

class GitHubClient:
    BASE = "https://api.github.com"

    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def get(self, path: str, params: Optional[dict] = None) -> dict | list:
        url = f"{self.BASE}{path}"
        resp = self.session.get(url, params=params, timeout=30)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(1, reset - int(time.time()))
            logging.getLogger("github-ingest").warning(
                "Rate limited. Sleeping %ds.", wait
            )
            time.sleep(wait)
            resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def paginate(self, path: str, params: Optional[dict] = None) -> list[dict]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        results = []
        page = 1
        while True:
            params["page"] = page
            data = self.get(path, params)
            if not isinstance(data, list) or not data:
                break
            results.extend(data)
            if len(data) < params["per_page"]:
                break
            page += 1
        return results


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def _ts(s: Optional[str]) -> str:
    """Normalize GitHub timestamp to ISO8601 UTC with Z suffix."""
    if not s:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return s.replace("+00:00", "Z") if s.endswith("+00:00") else s


def _actor(obj: Optional[dict]) -> Optional[str]:
    if not obj:
        return None
    return obj.get("login")


def _email_to_github(email: Optional[str]) -> Optional[str]:
    """Resolve email → GitHub handle via people.yaml. Used for unlinked commit authors."""
    if not email:
        return None
    el = email.lower()
    for p in _load_people():
        if (p.get("email") or "").lower() == el:
            return p.get("github")
    return None


def normalize_pr(repo: str, pr: dict) -> Event:
    number = pr["number"]
    state = pr["state"]
    merged = pr.get("merged_at") is not None

    if merged:
        event_type = "pr_merged"
        ts = _ts(pr.get("merged_at"))
    elif state == "closed":
        event_type = "pr_closed"
        ts = _ts(pr.get("closed_at"))
    else:
        event_type = "pr_opened"
        ts = _ts(pr.get("created_at"))

    event = Event(
        id=f"github:{repo}:pr:{number}:{event_type}",
        source="github",
        event_type=event_type,
        ts=ts,
        actor=_actor(pr.get("user")),
        subject=f"{repo}#{number}",
        title=pr.get("title"),
        body=pr.get("body") or "",
        url=pr.get("html_url"),
    )
    extra = []
    if pr.get("merged_by"):
        extra.append((_actor(pr["merged_by"]) or "", "github"))
    enrich_refs(event, actor_field="github", extra_handles=extra)
    return event


def normalize_review(repo: str, pr_number: int, review: dict) -> Event:
    event = Event(
        id=f"github:{repo}:pr:{pr_number}:review:{review['id']}",
        source="github",
        event_type="review",
        ts=_ts(review.get("submitted_at")),
        actor=_actor(review.get("user")),
        subject=f"{repo}#{pr_number}",
        title=f"Review on #{pr_number}: {review.get('state', '')}",
        body=review.get("body") or "",
        url=review.get("html_url"),
    )
    enrich_refs(event, actor_field="github")
    return event


def normalize_pr_comment(repo: str, pr_number: int, comment: dict, kind: str = "comment") -> Event:
    event = Event(
        id=f"github:{repo}:pr:{pr_number}:{kind}:{comment['id']}",
        source="github",
        event_type="comment",
        ts=_ts(comment.get("created_at")),
        actor=_actor(comment.get("user")),
        subject=f"{repo}#{pr_number}",
        title=f"Comment on #{pr_number}",
        body=comment.get("body") or "",
        url=comment.get("html_url"),
    )
    enrich_refs(event, actor_field="github")
    return event


def normalize_pr_commit(repo: str, pr_number: int, commit: dict) -> Event:
    """Commit within a PR — links commit author to PR subject for domain attribution."""
    sha = commit["sha"][:12]
    c = commit.get("commit", {})
    gh_user = commit.get("author") or {}
    git_author = c.get("author") or {}

    actor = (
        _actor(gh_user)
        or _email_to_github(git_author.get("email"))
        or git_author.get("name")
    )

    event = Event(
        id=f"github:{repo}:pr:{pr_number}:commit:{commit['sha']}",
        source="github",
        event_type="commit_in_pr",
        ts=_ts(git_author.get("date")),
        actor=actor,
        subject=f"{repo}#{pr_number}",
        title=c.get("message", "").split("\n")[0],
        body="",
        url=commit.get("html_url"),
    )
    enrich_refs(event, actor_field="github")
    return event


def normalize_commit(repo: str, commit: dict) -> Event:
    sha = commit["sha"][:12]
    c = commit.get("commit", {})
    gh_user = commit.get("author") or {}
    git_author = c.get("author") or {}

    # Prefer GH login → email lookup in people.yaml → git name fallback
    actor = (
        _actor(gh_user)
        or _email_to_github(git_author.get("email"))
        or git_author.get("name")
    )

    event = Event(
        id=f"github:{repo}:commit:{commit['sha']}",
        source="github",
        event_type="commit_pushed",
        ts=_ts(git_author.get("date")),
        actor=actor,
        subject=f"{repo}@{sha}",
        title=c.get("message", "").split("\n")[0],
        body=c.get("message", ""),
        url=commit.get("html_url"),
    )
    enrich_refs(event, actor_field="github")
    return event


# ---------------------------------------------------------------------------
# Diff fetcher
# ---------------------------------------------------------------------------

def fetch_commit_diff(client: GitHubClient, repo: str, sha: str, log: logging.Logger) -> None:
    """Fetch full commit diff (files + patches) and store to raw/github/diffs/{sha}.json.

    Skips if already fetched. Each file entry includes `patch` (unified diff),
    `filename`, `additions`, `deletions`, `status`.
    """
    diff_path = ROOT / "raw" / "github" / "diffs" / f"{sha}.json"
    if diff_path.exists():
        return
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = client.get(f"/repos/{repo}/commits/{sha}")
        # Keep only what's useful; drop raw commit object noise
        payload = {
            "sha": sha,
            "repo": repo,
            "files": [
                {
                    "filename": f.get("filename"),
                    "status": f.get("status"),
                    "additions": f.get("additions"),
                    "deletions": f.get("deletions"),
                    "patch": f.get("patch"),  # unified diff — may be absent for binary files
                }
                for f in data.get("files", [])
            ],
        }
        diff_path.write_text(json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        log.warning("  Failed to fetch diff for %s: %s", sha, e)


# ---------------------------------------------------------------------------
# PR-level metadata: diff stats + CI checks → pr_meta
# ---------------------------------------------------------------------------

# Check-run conclusions that count as a failing PR build.
_FAILING_CONCLUSIONS = {"failure", "timed_out", "action_required", "cancelled", "stale"}


def fetch_pr_checks(client: GitHubClient, repo: str, sha: Optional[str],
                    log: logging.Logger) -> tuple[str, list[str]]:
    """Return (status, failed_check_names) for a PR head sha.

    status: success | failure | pending | none | unknown.
    Uses the check-runs API; a single call (per_page=100) covers virtually all
    PRs. Never raises — falls back to 'unknown' so it can't break ingest.
    """
    if not sha:
        return ("unknown", [])
    try:
        data = client.get(f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100})
    except Exception as e:
        log.warning("  check-runs fetch failed for %s@%s: %s", repo, sha[:12], e)
        return ("unknown", [])
    runs = (data or {}).get("check_runs", []) if isinstance(data, dict) else []
    if not runs:
        return ("none", [])
    failed = [
        r.get("name") for r in runs
        if (r.get("conclusion") or "").lower() in _FAILING_CONCLUSIONS and r.get("name")
    ]
    if failed:
        return ("failure", failed)
    if any((r.get("status") or "").lower() != "completed" for r in runs):
        return ("pending", [])
    return ("success", [])


def upsert_pr_meta(conn, repo: str, pr: dict, checks: tuple[str, list[str]]) -> None:
    """Upsert a pr_meta row from a full PR detail object + CI checks.

    `pr` must be the single-PR endpoint payload (the list payload lacks
    additions/deletions/changed_files). Keyed by subject 'owner/repo#N'.
    """
    number = pr["number"]
    subject = f"{repo}#{number}"
    state = "merged" if pr.get("merged_at") else pr.get("state")
    labels = [l.get("name") for l in (pr.get("labels") or []) if l.get("name")]
    head_sha = (pr.get("head") or {}).get("sha")
    checks_status, checks_failed = checks
    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn.execute(
        """
        INSERT INTO pr_meta (subject, repo, number, state, additions, deletions,
                             files_changed, is_draft, labels_json, head_sha,
                             checks_status, checks_failed_json, created_at,
                             merged_at, updated_at, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(subject) DO UPDATE SET
            state=excluded.state, additions=excluded.additions,
            deletions=excluded.deletions, files_changed=excluded.files_changed,
            is_draft=excluded.is_draft, labels_json=excluded.labels_json,
            head_sha=excluded.head_sha, checks_status=excluded.checks_status,
            checks_failed_json=excluded.checks_failed_json,
            merged_at=excluded.merged_at, updated_at=excluded.updated_at,
            fetched_at=excluded.fetched_at
        """,
        (
            subject, repo, number, state, pr.get("additions"), pr.get("deletions"),
            pr.get("changed_files"), 1 if pr.get("draft") else 0,
            json.dumps(labels), head_sha, checks_status, json.dumps(checks_failed),
            pr.get("created_at"), pr.get("merged_at"), pr.get("updated_at"), fetched_at,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Per-repo ingest
# ---------------------------------------------------------------------------

def ingest_repo(
    client: GitHubClient,
    repo: str,
    since: Optional[str],
    conn,
    dry_run: bool,
    include_diffs: bool,
    log: logging.Logger,
) -> tuple[int, int]:
    """Returns (new_events, duplicate_events)."""
    new_count = 0
    dup_count = 0

    # /pulls does not support `since` — paginate manually, stop when updated_at < since
    params: dict = {"state": "all", "sort": "updated", "direction": "desc", "per_page": 100}
    log.info("Fetching PRs for %s since %s", repo, since or "beginning")
    prs: list[dict] = []
    page = 1
    while True:
        params["page"] = page
        page_data = client.get(f"/repos/{repo}/pulls", params)
        if not isinstance(page_data, list) or not page_data:
            break
        if since:
            relevant = [pr for pr in page_data if pr.get("updated_at", "") >= since]
            prs.extend(relevant)
            if len(relevant) < len(page_data):
                break  # rest of pages are older than cursor
        else:
            prs.extend(page_data)
        if len(page_data) < 100:
            break
        page += 1
    log.info("  %d PRs fetched", len(prs))

    for pr_idx, pr in enumerate(prs):
        pr_number = pr["number"]
        if pr_idx % 50 == 0:
            log.info("  Processing PR %d/%d (#%d)...", pr_idx + 1, len(prs), pr_number)

        # PR event
        event = normalize_pr(repo, pr)
        is_new = store_event(conn, event, dry_run=dry_run)
        if is_new:
            new_count += 1
            if dry_run:
                log.info("  [DRY RUN] %s %s %s", event.event_type, event.subject, event.title)
        else:
            dup_count += 1

        # PR-level metadata (diff stats + CI checks) → pr_meta.
        # The list payload lacks additions/changed_files, so fetch the single-PR
        # detail once and reuse it for the merged_by lookup below. Skip the fetch
        # for immutable PRs already fully captured (keeps re-runs/backfill cheap).
        pr_detail: Optional[dict] = None
        if not dry_run:
            subject = f"{repo}#{pr_number}"
            immutable = bool(pr.get("merged_at")) or pr.get("state") == "closed"
            meta_row = conn.execute(
                "SELECT checks_status FROM pr_meta WHERE subject = ?", (subject,)
            ).fetchone()
            meta_complete = bool(meta_row and meta_row["checks_status"] is not None)
            if not (immutable and meta_complete):
                try:
                    pr_detail = client.get(f"/repos/{repo}/pulls/{pr_number}") or {}
                    head_sha = (pr_detail.get("head") or {}).get("sha")
                    upsert_pr_meta(conn, repo, pr_detail,
                                   fetch_pr_checks(client, repo, head_sha, log))
                except Exception as e:
                    log.warning("  pr_meta failed for %s#%d: %s", repo, pr_number, e)

        # pr_merged_by — merger identity as separate event (domain ownership signal)
        # GitHub list-PRs API always returns merged_by:null; fetch individual PR to get merger.
        # Skip the extra API call if event already exists (idempotent re-runs).
        if pr.get("merged_at"):
            merged_by_obj = pr.get("merged_by")
            if not merged_by_obj:
                merged_by_id = f"github:{repo}:pr:{pr_number}:pr_merged_by"
                row = conn.execute("SELECT 1 FROM events WHERE id = ?", (merged_by_id,)).fetchone()
                if not row:
                    detail = pr_detail  # reuse the pr_meta fetch when available
                    if detail is None:
                        try:
                            detail = client.get(f"/repos/{repo}/pulls/{pr_number}")
                        except Exception as exc:
                            log.warning("  pr_merged_by fetch failed for %s#%d: %s", repo, pr_number, exc)
                    merged_by_obj = (detail or {}).get("merged_by")
            merger_login = (_actor(merged_by_obj) or None) if merged_by_obj else None
            if merger_login:
                merger_event = Event(
                    id=f"github:{repo}:pr:{pr_number}:pr_merged_by",
                    source="github",
                    event_type="pr_merged_by",
                    ts=_ts(pr.get("merged_at")),
                    actor=merger_login,
                    subject=f"{repo}#{pr_number}",
                    title=pr.get("title"),
                    body="",
                    url=pr.get("html_url"),
                )
                enrich_refs(merger_event, actor_field="github")
                m_new = store_event(conn, merger_event, dry_run=dry_run)
                if m_new:
                    new_count += 1
                    if dry_run:
                        log.info("  [DRY RUN] pr_merged_by %s by %s", merger_event.subject, merger_login)
                else:
                    dup_count += 1

        # Reviews
        try:
            reviews = client.paginate(f"/repos/{repo}/pulls/{pr_number}/reviews")
            for review in reviews:
                rev_event = normalize_review(repo, pr_number, review)
                is_new = store_event(conn, rev_event, dry_run=dry_run)
                if is_new:
                    new_count += 1
                    if dry_run:
                        log.info("  [DRY RUN] %s %s", rev_event.event_type, rev_event.subject)
                else:
                    dup_count += 1
        except Exception as e:
            log.warning("  Failed to fetch reviews for PR %d: %s", pr_number, e)

        # PR comments (review comments on diff lines)
        try:
            comments = client.paginate(f"/repos/{repo}/pulls/{pr_number}/comments")
            for comment in comments:
                c_event = normalize_pr_comment(repo, pr_number, comment)
                is_new = store_event(conn, c_event, dry_run=dry_run)
                if is_new:
                    new_count += 1
                    if dry_run:
                        log.info("  [DRY RUN] %s %s", c_event.event_type, c_event.subject)
                else:
                    dup_count += 1
        except Exception as e:
            log.warning("  Failed to fetch comments for PR %d: %s", pr_number, e)

        # PR commits (commit_in_pr — links each contributor to PR subject)
        try:
            pr_commits = client.paginate(f"/repos/{repo}/pulls/{pr_number}/commits")
            for commit in pr_commits:
                _record_commit_signals(conn, commit)
                pc_event = normalize_pr_commit(repo, pr_number, commit)
                if pc_event.actor:
                    pc_new = store_event(conn, pc_event, dry_run=dry_run)
                    if pc_new:
                        new_count += 1
                        if dry_run:
                            log.info("  [DRY RUN] commit_in_pr %s by %s", pc_event.subject, pc_event.actor)
                    else:
                        dup_count += 1
        except Exception as e:
            log.warning("  Failed to fetch PR commits for PR %d: %s", pr_number, e)

        # Issue-level comments (general PR discussion, distinct from inline review comments)
        try:
            issue_comments = client.paginate(f"/repos/{repo}/issues/{pr_number}/comments")
            for comment in issue_comments:
                ic_event = normalize_pr_comment(repo, pr_number, comment, kind="issue_comment")
                is_new = store_event(conn, ic_event, dry_run=dry_run)
                if is_new:
                    new_count += 1
                    if dry_run:
                        log.info("  [DRY RUN] issue_comment %s by %s", ic_event.subject, ic_event.actor)
                else:
                    dup_count += 1
        except Exception as e:
            log.warning("  Failed to fetch issue comments for PR %d: %s", pr_number, e)

    # Commits on default branch
    commit_params: dict = {}
    if since:
        commit_params["since"] = since

    try:
        log.info("Fetching commits for %s", repo)
        commits = client.paginate(f"/repos/{repo}/commits", commit_params)
        log.info("  %d commits fetched", len(commits))
        for commit in commits:
            _record_commit_signals(conn, commit)
            c_event = normalize_commit(repo, commit)
            is_new = store_event(conn, c_event, dry_run=dry_run)
            if is_new:
                new_count += 1
                if dry_run:
                    log.info("  [DRY RUN] %s %s %s", c_event.event_type, c_event.subject, c_event.title)
                if include_diffs and not dry_run:
                    fetch_commit_diff(client, repo, commit["sha"], log)
            else:
                dup_count += 1
    except Exception as e:
        log.warning("  Failed to fetch commits for %s: %s", repo, e)

    return new_count, dup_count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub ingest")
    parser.add_argument("--dry-run", action="store_true", help="Print events without writing")
    parser.add_argument("--repo", action="append", dest="repos", help="Override repo list (repeatable)")
    parser.add_argument("--reset-cursor", action="store_true", help="Ignore existing cursor (full re-fetch)")
    parser.add_argument("--include-diffs", action="store_true", default=False,
                        help="Fetch and store full commit diffs (files+patches) to raw/github/diffs/. "
                             "Adds 1 API call per new commit. Off by default.")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    log = setup_logging()
    repos = args.repos or DEFAULT_REPOS
    cursor_key = "github"

    since = None if args.reset_cursor else read_cursor(cursor_key)
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    log.info("GitHub ingest starting. repos=%s since=%s dry_run=%s include_diffs=%s",
             repos, since, args.dry_run, args.include_diffs)

    conn = get_db()
    init_identity_signals(conn)
    total_new = 0
    total_dup = 0
    n_failed = 0
    last_err: str = ""

    for repo in repos:
        try:
            new, dup = ingest_repo(
                client=GitHubClient(token),
                repo=repo,
                since=since,
                conn=conn,
                dry_run=args.dry_run,
                include_diffs=args.include_diffs,
                log=log,
            )
            total_new += new
            total_dup += dup
            log.info("Repo %s: %d new, %d duplicates", repo, new, dup)
        except Exception as e:
            last_err = str(e)
            log.error("Repo %s failed: %s", repo, e)
            n_failed += 1

    if not args.dry_run and n_failed == 0:
        write_cursor(cursor_key, now_ts)
        if not args.reset_cursor:
            write_success_date("github")
        log.info("Cursor updated to %s", now_ts)
    elif n_failed == len(repos):
        # 100% failure — auth outage, network down, or similar critical.
        log.error("Cursor NOT updated — ALL %d repos failed (last error: %s)",
                  len(repos), last_err[:200])
    elif n_failed > 0:
        log.warning("Cursor NOT updated — %d of %d repos failed (last error: %s)",
                    n_failed, len(repos), last_err[:200])

    log.info("Done. source=github total_new=%d total_dup=%d", total_new, total_dup)
    if n_failed == len(repos) and repos:
        sys.exit(2)  # critical — distinguish from partial-fail exit 1
    if n_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

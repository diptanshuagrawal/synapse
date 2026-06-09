"""
One-time backfill of pr_meta (diff stats + CI checks) for historical PRs.

Phase 2 of the PR-quality scorer (PRD: prd/pr-quality-scorer.md). Walks each
repo's PRs and populates pr_meta via the same helpers steady-state ingest uses
(ingest/github.py::fetch_pr_checks + upsert_pr_meta), so backfilled rows are
identical to forward-captured ones.

Idempotent: skips immutable PRs (merged/closed) already fully captured. So a
narrow run (--since-days 45) followed by a full run (--all) only fetches the
PRs the first run didn't cover — no wasted API calls.

Cost: ~2 API calls per processed PR (1 detail + 1 check-runs).

Usage:
    python ingest/github_backfill_pr_meta.py --since-days 45
    python ingest/github_backfill_pr_meta.py --all
    python ingest/github_backfill_pr_meta.py --all --repo example-org/service-a --dry-run

Env:
    GITHUB_TOKEN — required.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ingest.common import get_db
from ingest.github import (
    DEFAULT_REPOS,
    GitHubClient,
    fetch_pr_checks,
    setup_logging,
    upsert_pr_meta,
)


def _list_prs(client: GitHubClient, repo: str, cutoff: str | None, log) -> list[dict]:
    """List PRs (newest-updated first), stopping once we page past the cutoff.

    cutoff: ISO8601 string; only PRs with updated_at >= cutoff are kept.
    None → all PRs.
    """
    params = {"state": "all", "sort": "updated", "direction": "desc", "per_page": 100}
    prs: list[dict] = []
    page = 1
    while True:
        params["page"] = page
        page_data = client.get(f"/repos/{repo}/pulls", params)
        if not isinstance(page_data, list) or not page_data:
            break
        if cutoff:
            keep = [pr for pr in page_data if pr.get("updated_at", "") >= cutoff]
            prs.extend(keep)
            if len(keep) < len(page_data):
                break  # remaining pages are all older than cutoff
        else:
            prs.extend(page_data)
        if len(page_data) < 100:
            break
        page += 1
    return prs


def backfill_repo(client: GitHubClient, repo: str, cutoff: str | None,
                  conn, dry_run: bool, log) -> tuple[int, int]:
    """Returns (processed, skipped)."""
    prs = _list_prs(client, repo, cutoff, log)
    log.info("%s: %d PRs in window", repo, len(prs))
    processed = skipped = 0
    for i, pr in enumerate(prs):
        num = pr["number"]
        subject = f"{repo}#{num}"
        immutable = bool(pr.get("merged_at")) or pr.get("state") == "closed"
        row = conn.execute(
            "SELECT checks_status FROM pr_meta WHERE subject = ?", (subject,)
        ).fetchone()
        if immutable and row and row["checks_status"] is not None:
            skipped += 1
            continue
        if dry_run:
            processed += 1
            continue
        try:
            detail = client.get(f"/repos/{repo}/pulls/{num}") or {}
            head = (detail.get("head") or {}).get("sha")
            upsert_pr_meta(conn, repo, detail, fetch_pr_checks(client, repo, head, log))
            processed += 1
        except Exception as e:
            log.warning("  %s: pr_meta failed: %s", subject, e)
        if (i + 1) % 25 == 0:
            log.info("  %s: %d/%d (processed=%d skipped=%d)", repo, i + 1, len(prs), processed, skipped)
    return processed, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill pr_meta for historical PRs")
    ap.add_argument("--repo", action="append", dest="repos", help="Override repo list (repeatable)")
    ap.add_argument("--since-days", type=int, help="Only PRs updated in the last N days")
    ap.add_argument("--all", action="store_true", help="All PRs (no date cutoff)")
    ap.add_argument("--dry-run", action="store_true", help="Count what would be fetched; no writes/API detail calls")
    args = ap.parse_args()

    if not args.all and args.since_days is None:
        ap.error("pick a window: --since-days N or --all")

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    log = setup_logging()
    repos = args.repos or DEFAULT_REPOS
    cutoff = None
    if args.since_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.since_days)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("Backfill pr_meta. repos=%s cutoff=%s dry_run=%s", repos, cutoff or "ALL", args.dry_run)

    conn = get_db()
    client = GitHubClient(token)
    tot_p = tot_s = 0
    for repo in repos:
        p, s = backfill_repo(client, repo, cutoff, conn, args.dry_run, log)
        log.info("%s: processed=%d skipped=%d", repo, p, s)
        tot_p += p
        tot_s += s
    log.info("Done. total processed=%d skipped=%d", tot_p, tot_s)


if __name__ == "__main__":
    main()

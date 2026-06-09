#!/usr/bin/env python3
"""
jira_backfill_status.py — one-shot backfill of `to_status` column on
existing jira events.

Background
----------
Migration 007 added `to_status` to `events`. Going forward, jira ingest
populates it on `status_change` (toString from changelog) and
`issue_created` (initial status). Existing rows from prior ingests have
to_status=NULL — this script fills them by re-fetching changelog data
via Jira API.

Idempotent: only updates rows where to_status IS NULL.

Cost: one paged JQL search (~24 API calls for ~2k issues) — fast.

CLI
---
    .venv/bin/python derive/jira_backfill_status.py [--dry-run] [--project EX]

After running, verify:
    SELECT COUNT(*) FROM events WHERE source='jira' AND event_type='status_change' AND to_status IS NOT NULL;
    SELECT COUNT(*) FROM events WHERE source='jira' AND event_type='issue_created' AND to_status IS NOT NULL;
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import get_db  # noqa: E402
from ingest.jira import JiraClient  # noqa: E402
from derive.sources_config import atlassian_host, owner_email, jira_project_keys  # noqa: E402


def backfill(project: str, dry_run: bool = False) -> dict:
    """Walk all issues in project via JQL + changelog, UPDATE to_status
    on issue_created + status_change rows where it's NULL."""
    token = os.environ.get("ATLASSIAN_TOKEN")
    email = os.environ.get("ATLASSIAN_EMAIL", owner_email())
    domain = os.environ.get("ATLASSIAN_DOMAIN", atlassian_host())
    if not token:
        raise SystemExit("ATLASSIAN_TOKEN env var required")

    client = JiraClient(domain, email, token)
    conn = get_db()

    n_created_filled = 0
    n_status_filled = 0
    n_created_already = 0
    n_status_already = 0
    n_missing = 0
    n_issues = 0

    fields = ["status"]
    jql = f"project = {project} ORDER BY updated ASC"
    for issue in client.search_issues(jql, fields, expand=["changelog"]):
        n_issues += 1
        key = issue["key"]
        f = issue.get("fields", {})

        # 1) issue_created — initial status. Update only if NULL.
        initial_status = (f.get("status") or {}).get("name")
        created_id = f"jira:{key}:created"
        existing = conn.execute(
            "SELECT to_status FROM events WHERE id = ?", (created_id,)
        ).fetchone()
        if existing is None:
            n_missing += 1
        elif existing[0] is not None:
            n_created_already += 1
        elif initial_status:
            if not dry_run:
                conn.execute(
                    "UPDATE events SET to_status = ? WHERE id = ? AND to_status IS NULL",
                    (initial_status, created_id),
                )
            n_created_filled += 1

        # 2) status_change events — one per changelog history.items[field=status]
        for history in issue.get("changelog", {}).get("histories", []):
            history_id = history.get("id", "")
            for idx, item in enumerate(history.get("items", [])):
                if item.get("field") != "status":
                    continue
                to_status = item.get("toString")
                if not to_status:
                    continue
                event_id = f"jira:{key}:status:{history_id}:{idx}"
                existing = conn.execute(
                    "SELECT to_status FROM events WHERE id = ?", (event_id,)
                ).fetchone()
                if existing is None:
                    n_missing += 1
                    continue
                if existing[0] is not None:
                    n_status_already += 1
                    continue
                if not dry_run:
                    conn.execute(
                        "UPDATE events SET to_status = ? WHERE id = ? AND to_status IS NULL",
                        (to_status, event_id),
                    )
                n_status_filled += 1

        if n_issues % 50 == 0:
            if not dry_run:
                conn.commit()
            print(f"  ...{n_issues} issues processed (created_filled={n_created_filled} status_filled={n_status_filled})", file=sys.stderr)

    if not dry_run:
        conn.commit()

    return {
        "project": project,
        "dry_run": dry_run,
        "n_issues": n_issues,
        "issue_created_filled": n_created_filled,
        "issue_created_already_set": n_created_already,
        "status_change_filled": n_status_filled,
        "status_change_already_set": n_status_already,
        "events_missing_in_db": n_missing,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--project", default=jira_project_keys()[0],
                    help="Jira project key (default: first configured project key)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Count what WOULD be updated; no writes")
    args = ap.parse_args()

    import json
    stats = backfill(args.project, dry_run=args.dry_run)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

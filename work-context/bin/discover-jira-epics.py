#!/usr/bin/env python3
"""
List all EX Epics with summary — output is yaml-ready snippets to paste into config/projects.yaml.

Usage:
    bin/discover-jira-epics.py                       # all EX epics, all statuses
    bin/discover-jira-epics.py --project EX          # explicit project
    bin/discover-jira-epics.py --status "In Progress","To Do"   # filter by status
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from ingest.jira import JiraClient  # noqa: E402
from derive.sources_config import owner_email, atlassian_host, jira_project_keys  # noqa: E402

TOKEN_FILE = Path.home() / ".secrets/atlassian_token"
EMAIL_FILE = Path.home() / ".secrets/atlassian_email"
DEFAULT_EMAIL = owner_email()
DOMAIN = atlassian_host()


def _load_slug_keywords() -> dict[str, list[str]]:
    """slug → match-keywords, sourced from config/projects.yaml (no hardcoded
    org slugs here). Tolerant of a missing config (returns empty)."""
    import yaml
    path = ROOT / "config" / "projects.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    out: dict[str, list[str]] = {}
    for p in data.get("projects", []) or []:
        slug = p.get("slug")
        kws = [str(k).lower() for k in (p.get("keywords") or [])]
        if slug and kws:
            out[slug] = kws
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=(jira_project_keys()[0] if jira_project_keys() else "EX"))
    parser.add_argument("--status", default="", help="Comma-separated status filter")
    args = parser.parse_args()

    if not TOKEN_FILE.exists():
        print(f"ERROR: {TOKEN_FILE} missing", file=sys.stderr)
        sys.exit(1)
    token = TOKEN_FILE.read_text().strip()
    email = EMAIL_FILE.read_text().strip() if EMAIL_FILE.exists() else DEFAULT_EMAIL

    client = JiraClient(DOMAIN, email, token)

    jql = f'project = {args.project} AND issuetype = Epic'
    if args.status:
        statuses = [f'"{s.strip()}"' for s in args.status.split(",") if s.strip()]
        jql += f' AND status in ({",".join(statuses)})'
    jql += ' ORDER BY created DESC'

    print(f"# JQL: {jql}\n")
    fields = ["summary", "status", "created", "updated"]

    rows = []
    for issue in client.search_issues(jql, fields, expand=[]):
        f = issue.get("fields", {})
        rows.append({
            "key":     issue["key"],
            "summary": (f.get("summary") or "").strip(),
            "status":  ((f.get("status") or {}).get("name") or "").strip(),
            "created": (f.get("created") or "")[:10],
        })

    print(f"# Found {len(rows)} epic(s)\n")
    for r in rows:
        print(f"  - {r['key']:<12} [{r['status']:<14}] {r['created']}  {r['summary']}")

    print("\n# Suggested yaml snippets — drop into config/projects.yaml under matching slug:")
    print("# (review epic names manually — auto-classification is title-substring guess)")
    keywords_by_slug = _load_slug_keywords()
    suggested: dict[str, list[str]] = {}
    for r in rows:
        s_lower = r["summary"].lower()
        for slug, kws in keywords_by_slug.items():
            if any(k in s_lower for k in kws):
                suggested.setdefault(slug, []).append(r["key"])
                break

    for slug, keys in suggested.items():
        print(f"\n  - slug: {slug}")
        print(f"    jira_epics: {keys}")


if __name__ == "__main__":
    main()

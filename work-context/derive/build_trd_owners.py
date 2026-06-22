"""Materialise the trd_owners table from raw Confluence events.

Run policy:
  - Hooked from ingest/run-confluence.sh after every successful ingest.
  - Can be run manually anytime; idempotent (DELETE + INSERT replaces full table).

Scoring (per (page, actor)):
    page_created  → 10
    page_updated  → 3 per update
    comment       → 1 per comment (footer or inline; both have event_type='comment')

Top scorer = OWNER. Anyone with score ≥ 30% of owner = CONTRIBUTOR.

TRD detection:
    title regex (?i)(TRD|Tech Spec|Technical Design|Technical Redesign)
    OR page_id in any config/projects.yaml::confluence_pages

Actor canonicalisation:
    Resolve event.actor → canonical via people.yaml
    Match against: jira_id, email, github, name, git_name, slack_handle
    Unresolved actors are dropped from scoring (not in team).

Usage:
    .venv/bin/python derive/build_trd_owners.py
    .venv/bin/python derive/build_trd_owners.py --verbose
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "index" / "events.db"
PEOPLE_YAML = ROOT / "config" / "people.yaml"
PROJECTS_YAML = ROOT / "config" / "projects.yaml"
MIGRATION = ROOT / "derive" / "migrations" / "003_trd_owners.sql"

TRD_RE = re.compile(r"(?i)\b(TRD|Tech Spec|Technical Design|Technical Redesign)\b")

W_CREATED = 10.0
W_UPDATED = 3.0
W_COMMENT = 1.0
CONTRIBUTOR_THRESHOLD = 0.30   # fraction of owner score


def load_people() -> dict[str, str]:
    """Build a reverse lookup: any handle/email/id → canonical."""
    with open(PEOPLE_YAML) as f:
        data = yaml.safe_load(f).get("people", [])
    lookup: dict[str, str] = {}
    for p in data:
        canonical = p.get("canonical")
        if not canonical:
            continue
        for field in ("canonical", "github", "email", "jira_id", "name", "git_name", "slack_handle"):
            v = p.get(field)
            if v:
                lookup[str(v).lower()] = canonical
    return lookup


def load_project_pages() -> dict[str, str]:
    """Map page_id → project_slug from projects.yaml."""
    with open(PROJECTS_YAML) as f:
        data = yaml.safe_load(f).get("projects", [])
    m: dict[str, str] = {}
    for p in data:
        slug = p.get("slug")
        for pid in (p.get("confluence_pages") or []):
            m[str(pid)] = slug
    return m


def is_trd(title: str, page_id: str, project_pages: dict[str, str]) -> tuple[bool, str | None]:
    """Return (is_trd, project_slug). project_slug is set only when matched via projects.yaml."""
    if page_id in project_pages:
        return True, project_pages[page_id]
    if TRD_RE.search(title or ""):
        return True, None
    return False, None


def fetch_trd_events(conn: sqlite3.Connection) -> dict[str, dict]:
    """Build {page_id: {title, events: [...]}} for every TRD-tagged page.

    Returns events keyed by page_id; each event has actor, event_type, ts, title.
    """
    project_pages = load_project_pages()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT subject, title, event_type, actor, ts FROM events
        WHERE source = 'confluence'
          AND subject LIKE 'page:%'
          AND event_type IN ('page_created', 'page_updated', 'comment')
        ORDER BY ts ASC
        """
    )
    pages: dict[str, dict] = {}
    for subject, title, event_type, actor, ts in cur.fetchall():
        page_id = subject.replace("page:", "")
        # Use the most recent non-comment title (comments have "inline comment on …" prefix)
        if event_type != "comment":
            pages.setdefault(page_id, {"title": title or "", "events": []})
            pages[page_id]["title"] = title or pages[page_id]["title"]
        else:
            pages.setdefault(page_id, {"title": "", "events": []})
        pages[page_id]["events"].append({"actor": actor, "event_type": event_type, "ts": ts})

    # Filter: only keep TRD-tagged pages.
    out: dict[str, dict] = {}
    for page_id, info in pages.items():
        # If we only saw comment events for this page, the comment title is "inline comment on '<real-title>' (page <id>)".
        # Extract real title from such a comment so TRD-by-title can still match.
        title = info["title"]
        if not title:
            for ev in info["events"]:
                # Comment title format: "inline comment on '<real-title>' (page <id>)"
                # or "comment on '<real-title>' (page <id>)"
                m = re.search(r"on '(.+?)' \(page \d+\)", str(ev.get("title") or ""))
                if m:
                    title = m.group(1)
                    break
        is_trd_, project = is_trd(title, page_id, project_pages)
        if not is_trd_:
            continue
        info["title"] = title
        info["project_slug"] = project
        out[page_id] = info
    return out


def score_page(events: list[dict], people_lookup: dict[str, str], verbose: bool = False) -> dict:
    """Score actors for one page. Returns {scores: {canonical: score}, total_events, last_ts}."""
    scores: dict[str, float] = {}
    total = 0
    last_ts = ""
    for ev in events:
        total += 1
        if ev["ts"] > last_ts:
            last_ts = ev["ts"]
        actor = (ev.get("actor") or "").strip()
        if not actor:
            continue
        canonical = people_lookup.get(actor.lower())
        if not canonical:
            if verbose:
                print(f"  unresolved actor: {actor!r}", file=sys.stderr)
            continue
        et = ev["event_type"]
        if et == "page_created":
            scores[canonical] = scores.get(canonical, 0) + W_CREATED
        elif et == "page_updated":
            scores[canonical] = scores.get(canonical, 0) + W_UPDATED
        elif et == "comment":
            scores[canonical] = scores.get(canonical, 0) + W_COMMENT
    return {"scores": scores, "total_events": total, "last_ts": last_ts}


def derive_owner_and_contributors(scores: dict[str, float]) -> tuple[str | None, float, list[str]]:
    """Top scorer → owner. Anyone ≥ 30% of owner → contributor."""
    if not scores:
        return None, 0.0, []
    sorted_scores = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    owner, owner_score = sorted_scores[0]
    threshold = owner_score * CONTRIBUTOR_THRESHOLD
    contributors = [c for c, s in sorted_scores[1:] if s >= threshold]
    return owner, owner_score, contributors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(str(DB))
    # Wait for the write lock instead of failing immediately — Slack ingest can
    # hold it for minutes and this runs concurrently from every ingest wrapper.
    conn.execute("PRAGMA busy_timeout = 30000")  # 30s, matches ingest/common.py
    # Ensure schema present.
    if MIGRATION.exists():
        conn.executescript(MIGRATION.read_text())
    else:
        print(f"WARN: migration not found at {MIGRATION}", file=sys.stderr)

    people_lookup = load_people()
    trd_pages = fetch_trd_events(conn)

    if args.verbose:
        print(f"Found {len(trd_pages)} TRD-tagged pages.", file=sys.stderr)

    # Wipe-and-rebuild (idempotent, simple).
    conn.execute("DELETE FROM trd_owners")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    rows_inserted = 0
    for page_id, info in trd_pages.items():
        result = score_page(info["events"], people_lookup, verbose=args.verbose)
        owner, owner_score, contributors = derive_owner_and_contributors(result["scores"])
        if not owner:
            if args.verbose:
                print(f"  skipping page:{page_id} '{info['title']}' — no resolvable owner", file=sys.stderr)
            continue
        conn.execute(
            """
            INSERT INTO trd_owners
                (page_id, title, owner, owner_score, scores_json, contributors_json,
                 project_slug, last_event_ts, total_events, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page_id,
                info["title"],
                owner,
                owner_score,
                json.dumps(result["scores"]),
                json.dumps(contributors),
                info.get("project_slug"),
                result["last_ts"],
                result["total_events"],
                now,
            ),
        )
        rows_inserted += 1
        if args.verbose:
            print(f"  page:{page_id} '{info['title'][:60]}' → owner={owner} ({owner_score:.1f}); contributors={contributors}", file=sys.stderr)
    conn.commit()

    # Summary.
    print(f"trd_owners refreshed: {rows_inserted} rows ({len(trd_pages)} TRD pages found)")
    cur = conn.execute("SELECT owner, COUNT(*) FROM trd_owners GROUP BY owner ORDER BY 2 DESC")
    rows = cur.fetchall()
    if rows:
        print("\nOwnership distribution:")
        for owner, n in rows:
            print(f"  {owner:30} {n}")


if __name__ == "__main__":
    main()

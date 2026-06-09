"""Dump per-actor narrative signals for chat-session narrative generation.

Output:
  - <out>           : JSON list, one entry per actor needing narrative
  - <out>.rules.md  : narrative.SYSTEM_PROMPT (style/format spec)

Per-actor entry:
  {
    actor, name, window_days, content_hash,
    signals: {  # PersonSignals.to_dict()
      authored_prs, reviews_given, pr_comments_count,
      jira_owned, jira_transitioned, jira_commented,
      confluence_pages, confluence_comments_count
    }
  }

Skip actors whose (actor, window_days, content_hash) already exists in
person_narrative cache.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import llm_classifier as lc        # noqa: E402
import narrative as nv             # noqa: E402
import rollup as r                 # noqa: E402

DB = lc.ROOT / "index" / "events.db"

BOT_LIKE = "%[bot]%"


def _active_actors(conn: sqlite3.Connection, since: str, team_handles: set[str]) -> list[str]:
    cur = conn.execute("""
        SELECT actor, count(*) FROM events
        WHERE source = 'github'
          AND actor NOT LIKE ? AND actor IS NOT NULL AND ts >= ?
        GROUP BY actor HAVING count(*) >= 3
        ORDER BY count(*) DESC
    """, (BOT_LIKE, since))
    return [a for a, _ in cur.fetchall() if a in team_handles]


def _verdicts_for(conn: sqlite3.Connection, since: str, projects: list[dict],
                  team_handles: set[str]) -> dict[str, lc.SubjectVerdict]:
    subjects = r.collect_subjects(conn, since, projects, team_handles=team_handles)
    out: dict[str, lc.SubjectVerdict] = {}
    for s in subjects:
        h = lc._content_hash(s)
        row = conn.execute(
            "SELECT domains, summary, risk_flags, confidence, source FROM subject_summary "
            "WHERE subject=? AND content_hash=?",
            (s.subject, h),
        ).fetchone()
        if not row:
            continue
        out[s.subject] = lc.SubjectVerdict(
            domains=json.loads(row[0] or "[]"),
            summary=row[1] or "",
            risk_flags=json.loads(row[2] or "[]"),
            confidence=row[3] or 0.0,
            source=row[4] or "",
        )
    return out


def _rules_md() -> str:
    return (
        "# Narrative rules (mirrors narrative.SYSTEM_PROMPT)\n\n"
        + nv.SYSTEM_PROMPT.strip()
        + "\n\n## Echo-back\nEach narrative MUST include `actor` and `content_hash` "
          "unchanged from the dump.\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--actor", default="", help="restrict to one github handle")
    ap.add_argument("--out", required=True)
    ap.add_argument("--force", action="store_true",
                    help="dump even when cache row exists (re-narration)")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB))
    lc.ensure_schema(conn)
    nv.ensure_schema(conn)

    projects = r.load_projects()
    people, alias_map = r.load_people()
    team_handles = {p["github"] for p in people.values() if p.get("github")}
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat().replace("+00:00", "Z")

    if args.actor:
        if args.actor not in team_handles:
            print(f"WARN: {args.actor} not in people.yaml team_handles", file=sys.stderr)
        actors = [args.actor]
    else:
        actors = _active_actors(conn, since, team_handles)

    verdicts = _verdicts_for(conn, since, projects, team_handles)

    pending: list[dict] = []
    for actor in actors:
        sig = nv.build_signals(conn, actor, since, args.days, verdicts, people, alias_map)
        h = nv._content_hash(sig)
        if not args.force:
            cached = nv.load_cached(conn, actor, args.days, h)
            if cached is not None:
                print(f"  skip {actor}: cache hit ({h})")
                continue
        pending.append({
            "actor": actor,
            "name": sig.name,
            "window_days": args.days,
            "content_hash": h,
            "signals": sig.to_dict(),
        })

    out_path = Path(args.out)
    out_path.write_text(json.dumps(pending, indent=2))
    (out_path.with_suffix(out_path.suffix + ".rules.md")).write_text(_rules_md())
    print(f"dump_pending_narrative: {len(pending)} actors → {out_path}")


if __name__ == "__main__":
    main()

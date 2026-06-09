"""
person_deepread.py — one-shot bundle of everything chat needs to render a
per-person narrative. Replaces the 4-5 separate ctx_execute probes the
/ask person_range path used to make.

Emits a single JSON blob with:
  profile           — full person_profile.compute_profile() output (schema v3)
  clusters          — top 10 clusters person touched in window, with brief +
                      person's contribution count + role label + top members
  assigned_tickets  — every jira ticket assigned to person in window, with
                      issue_type / story_points / sprint_name / latest_status
                      / title / creator
  prs               — every PR the person opened in window (subset of
                      fate.pr_fate, with title)
  confluence        — every confluence page-event (created/updated/comment)
                      by person, with title + body bytes
  jira_comments     — top 20 jira comments by person (any length, on others'
                      tickets), with preview body
  slack_threads     — top 20 thread_started by person, with channel + reply
                      count + body preview

Use from /ask person_range:

    .venv/bin/python derive/person_deepread.py --name <canon> \\
        --since <iso> --until <iso>

Returns single JSON to stdout. Chat reads, synthesises narrative, writes
markdown. No more multi-probe round trips.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db, DB_PATH  # noqa: E402
from derive.person_profile import (  # noqa: E402
    compute_profile, _build_person_alias_map, _resolve_canonical, _ph,
)

# Cache lives in repo state/ — survives across all invocations regardless of
# whether caller is bash, ctx_execute sandbox, or LaunchAgent. Same dir as
# other long-lived state (cursors, last_*_success.date).
CACHE_DIR = _PKG_ROOT / "state" / "cache" / "person_deepread"


def _cache_path(name: str, since: str, until: str) -> Path:
    key = hashlib.sha1(f"{name}|{since}|{until}".encode()).hexdigest()[:12]
    safe = name.replace("/", "_")
    return CACHE_DIR / f"deepread_{safe}_{since[:10]}_{until[:10]}_{key}.json"


def _cache_fresh(cache: Path) -> bool:
    """Cache hit iff file exists AND events.db hasn't been modified since."""
    if not cache.exists():
        return False
    db_mtime = DB_PATH.stat().st_mtime if DB_PATH.exists() else 0
    return cache.stat().st_mtime >= db_mtime


def _person_aliases(name: str) -> tuple[str, list[str]] | None:
    person_map = _build_person_alias_map()
    canon = _resolve_canonical(name, person_map)
    if not canon:
        return None
    return canon, person_map[canon]["aliases"]


def _top_clusters_for_person(
    conn: sqlite3.Connection, aliases: list[str], since: str, until: str,
    canonical: str, top_n: int = 10,
) -> list[dict]:
    """Top clusters person touched, ordered by their reply_count in cluster."""
    ph = _ph(aliases)
    rows = conn.execute(
        f"""SELECT m.cluster_id, COUNT(*) AS reply_count,
                   MIN(e.ts) AS first_touch, MAX(e.ts) AS last_touch
            FROM events e
            JOIN topic_brief_member m ON m.subject = e.subject
            WHERE e.actor IN ({ph}) AND e.ts >= ? AND e.ts < ?
            GROUP BY m.cluster_id
            ORDER BY reply_count DESC LIMIT ?""",
        (*aliases, since, until, top_n),
    ).fetchall()
    out: list[dict] = []
    for cid, rc, ft, lt in rows:
        br = conn.execute(
            """SELECT cluster_id, label, status, summary, root_cause,
                       decisions_json, blockers_json, participants_json,
                       last_activity_ts, member_count
               FROM topic_brief WHERE cluster_id=?""",
            (cid,),
        ).fetchone()
        if not br:
            continue
        keys = ["cluster_id", "label", "status", "summary", "root_cause",
                "decisions_json", "blockers_json", "participants_json",
                "last_activity_ts", "member_count"]
        brief = dict(zip(keys, br))
        for jkey in ("decisions_json", "blockers_json", "participants_json"):
            if brief.get(jkey):
                try:
                    brief[jkey] = json.loads(brief[jkey])
                except (json.JSONDecodeError, TypeError):
                    pass
        # Person's role within cluster
        person_role = None
        person_contrib = None
        if isinstance(brief.get("participants_json"), list):
            for p in brief["participants_json"]:
                if (p.get("person") or "").lower() == canonical.lower():
                    person_role = p.get("role")
                    person_contrib = p.get("contribution_count")
                    break
        # Top 5 members of cluster (any source) for citation material.
        members = [
            {"subject": s, "source": src, "similarity": sim, "member_role": mr}
            for s, src, sim, mr in conn.execute(
                """SELECT subject, source, similarity, member_role
                   FROM topic_brief_member WHERE cluster_id=?
                   ORDER BY similarity DESC LIMIT 5""",
                (cid,),
            )
        ]
        out.append({
            "cluster_id": cid,
            "reply_count_in_window": rc,
            "first_touch": ft,
            "last_touch": lt,
            "brief": brief,
            "person_role": person_role,
            "person_contrib_count": person_contrib,
            "top_members": members,
        })
    return out


def _assigned_tickets(
    conn: sqlite3.Connection, aliases: list[str], since: str, until: str,
) -> list[dict]:
    """Every jira ticket assigned to person in window, with full metadata."""
    ph = _ph(aliases)
    # Creation-assigned in window.
    rows = conn.execute(
        f"""SELECT subject, title, issue_type, story_points, sprint_name,
                   sprint_state, ts AS created_ts, assignee AS creator
            FROM events
            WHERE source='jira' AND event_type='issue_created'
              AND assignee IN ({ph})
              AND ts >= ? AND ts < ?
            ORDER BY ts DESC""",
        (*aliases, since, until),
    ).fetchall()
    out: list[dict] = []
    for sub, title, it, sp, sn, ss, cts, creator in rows:
        latest = conn.execute(
            """SELECT to_status, ts FROM events
                WHERE subject=? AND to_status IS NOT NULL
                ORDER BY ts DESC LIMIT 1""",
            (sub,),
        ).fetchone()
        out.append({
            "subject": sub,
            "title": title or "",
            "issue_type": it or "",
            "story_points": sp,
            "sprint_name": sn or "",
            "sprint_state": ss or "",
            "created_ts": (cts or "")[:10],
            "creator": creator or "",
            "latest_status": latest[0] if latest else None,
            "latest_status_ts": (latest[1] if latest else "")[:10] if latest else None,
        })
    return out


def _person_prs(
    conn: sqlite3.Connection, aliases: list[str], since: str, until: str,
) -> list[dict]:
    ph = _ph(aliases)
    rows = conn.execute(
        f"""SELECT subject, title, ts FROM events
            WHERE source='github' AND event_type='pr_opened'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?
            ORDER BY ts""",
        (*aliases, since, until),
    ).fetchall()
    return [{"subject": s, "title": t or "", "opened_ts": (ts or "")[:10]} for s, t, ts in rows]


def _confluence(conn: sqlite3.Connection, aliases: list[str], since: str, until: str):
    ph = _ph(aliases)
    rows = conn.execute(
        f"""SELECT subject, event_type, ts, title, LENGTH(COALESCE(body,'')) AS bsize
            FROM events
            WHERE source='confluence' AND actor IN ({ph})
              AND ts >= ? AND ts < ?
            ORDER BY ts""",
        (*aliases, since, until),
    ).fetchall()
    return [{"subject": s, "event_type": et, "ts": (ts or "")[:10],
             "title": t or "", "body_bytes": b} for s, et, ts, t, b in rows]


def _top_jira_comments(
    conn: sqlite3.Connection, aliases: list[str], since: str, until: str,
    top_n: int = 20,
) -> list[dict]:
    """Top N jira comments by length on others' tickets."""
    ph = _ph(aliases)
    own = {
        r[0] for r in conn.execute(
            f"""SELECT subject FROM events
                WHERE source='jira' AND event_type='issue_created'
                  AND actor IN ({ph})""",
            aliases,
        ).fetchall()
    }
    rows = conn.execute(
        f"""SELECT subject, ts, LENGTH(body) AS L, SUBSTR(body,1,400) AS preview
            FROM events
            WHERE source='jira' AND event_type='comment'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?
            ORDER BY LENGTH(body) DESC LIMIT ?""",
        (*aliases, since, until, top_n * 2),
    ).fetchall()
    out: list[dict] = []
    for sub, ts, L, prev in rows:
        if sub in own:
            continue
        out.append({"subject": sub, "ts": (ts or "")[:10], "length": L,
                    "preview": prev or ""})
        if len(out) >= top_n:
            break
    return out


def _top_slack_threads(
    conn: sqlite3.Connection, aliases: list[str], since: str, until: str,
    top_n: int = 20,
) -> list[dict]:
    ph = _ph(aliases)
    rows = conn.execute(
        f"""SELECT subject, ts, channel_id, COALESCE(reply_count, 0) AS rc,
                   SUBSTR(body,1,300) AS preview
            FROM events
            WHERE source='slack' AND event_type='thread_started'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?
            ORDER BY rc DESC, ts DESC LIMIT ?""",
        (*aliases, since, until, top_n),
    ).fetchall()
    return [{"subject": s, "ts": (ts or "")[:16], "channel_id": ch,
             "reply_count": rc, "preview": prev or ""} for s, ts, ch, rc, prev in rows]


def bundle(name: str, since: str, until: str) -> dict:
    person_aliases = _person_aliases(name)
    if not person_aliases:
        profile = compute_profile(name, since, until)
        return {"profile": profile, "error": profile.get("error")}
    canonical, aliases = person_aliases

    conn = get_db()
    profile = compute_profile(name, since, until)

    return {
        "profile": profile,
        "clusters": _top_clusters_for_person(conn, aliases, since, until, canonical),
        "assigned_tickets": _assigned_tickets(conn, aliases, since, until),
        "prs": _person_prs(conn, aliases, since, until),
        "confluence": _confluence(conn, aliases, since, until),
        "jira_comments": _top_jira_comments(conn, aliases, since, until),
        "slack_threads": _top_slack_threads(conn, aliases, since, until),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--no-cache", action="store_true",
                    help="Force recompute even if cache is fresh.")
    args = ap.parse_args()

    cache = _cache_path(args.name, args.since, args.until)
    if not args.no_cache and _cache_fresh(cache):
        sys.stderr.write(f"[cached] {cache}\n")
        sys.stdout.write(cache.read_text())
        return

    payload = bundle(args.name, args.since, args.until)
    text = json.dumps(payload, indent=2, default=str)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(text)
    sys.stderr.write(f"[computed → cached] {cache}\n")
    sys.stdout.write(text)


if __name__ == "__main__":
    main()

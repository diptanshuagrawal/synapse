#!/usr/bin/env python3
"""Aggregate the events DB into the v3 dashboard "Insights" payload.

All output is aggregate / non-PII (counts, distributions, time-series, project
slugs) — safe to render in the local dashboard. This module powers both the
standalone v3 preview (dumped to dashboard_v3_data.json) and, once wired, the
live /api/insights endpoint in bin/dashboard.py.

    python3 bin/_v3_insights.py            # writes bin/dashboard_v3_data.json
    python3 bin/_v3_insights.py --stdout   # prints JSON
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "index/events.db"
OUT = Path(__file__).resolve().parent / "dashboard_v3_data.json"

SOURCES = ["slack", "github", "jira", "confluence"]
# IST = UTC + 5:30; events.ts is ISO (UTC). Shift before bucketing temporal cuts.
IST = "+330 minutes"


def _rows(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def build(conn) -> dict:
    out: dict = {}

    # ── work rhythm: hour(IST) × weekday(IST) punchcard ──────────────────────
    pc = _rows(conn, f"""
        SELECT CAST(strftime('%w', datetime(ts,'{IST}')) AS INT) wd,
               CAST(strftime('%H', datetime(ts,'{IST}')) AS INT) hr,
               COUNT(*) n
        FROM events
        WHERE ts >= '2025-06-01'
        GROUP BY wd, hr
    """)
    out["punchcard"] = [{"wd": wd, "hr": hr, "n": n} for wd, hr, n in pc]

    # ── activity stream: monthly volume by source ────────────────────────────
    stream = _rows(conn, """
        SELECT substr(ts,1,7) mo, source, COUNT(*) n
        FROM events
        WHERE ts >= '2025-01' AND source IN ('slack','github','jira','confluence')
        GROUP BY mo, source ORDER BY mo
    """)
    by_mo: dict[str, dict] = {}
    for mo, src, n in stream:
        by_mo.setdefault(mo, {"month": mo, **{s: 0 for s in SOURCES}})[src] = n
    out["stream"] = list(by_mo.values())

    # ── code review: friction categories ─────────────────────────────────────
    out["friction"] = [
        {"category": c, "n": n, "avg_score": round(s or 0, 1)}
        for c, n, s in _rows(conn,
            "SELECT dominant_category, COUNT(*), AVG(score) FROM pr_friction "
            "GROUP BY dominant_category ORDER BY 2 DESC")
    ]

    # ── code review: human vs AI taxonomy (matterai/claude → "ai") ───────────
    tax: dict[str, dict] = {}
    for cat, src, n in _rows(conn,
            "SELECT category, source, COUNT(*) FROM pr_comment_class GROUP BY 1,2"):
        side = "human" if src == "human" else "ai"
        tax.setdefault(cat, {"category": cat, "human": 0, "ai": 0})[side] += n
    out["review_taxonomy"] = sorted(
        tax.values(), key=lambda d: d["human"] + d["ai"], reverse=True)

    # ── code review: PR size vs friction scatter ─────────────────────────────
    out["pr_scatter"] = [
        {"size": (a or 0) + (d or 0), "score": round(sc or 0, 1),
         "repo": repo.split("/")[-1], "cat": cat, "files": f or 0}
        for a, d, sc, repo, cat, f in _rows(conn, """
            SELECT m.additions, m.deletions, f.score, m.repo,
                   f.dominant_category, m.files_changed
            FROM pr_friction f JOIN pr_meta m ON m.subject = f.subject
            WHERE m.additions IS NOT NULL
        """)
    ]

    # ── release health: outcomes + weekly cadence ────────────────────────────
    out["release_outcomes"] = [
        {"outcome": o, "n": n, "features": fe or 0}
        for o, n, fe in _rows(conn,
            "SELECT outcome, COUNT(*), SUM(is_feature_release) FROM feature_release "
            "GROUP BY outcome ORDER BY 2 DESC")
    ]
    cadence = _rows(conn, """
        SELECT substr(released_at,1,7) mo,
               COUNT(*) total,
               SUM(outcome='emergency') emerg,
               SUM(outcome='rolled_back') rb
        FROM feature_release
        WHERE released_at IS NOT NULL AND released_at >= '2025-01'
        GROUP BY mo ORDER BY mo
    """)
    out["release_cadence"] = [
        {"month": mo, "total": t, "emergency": e or 0, "rolled_back": rb or 0}
        for mo, t, e, rb in cadence
    ]

    # ── topics & projects ────────────────────────────────────────────────────
    out["cluster_status"] = [
        {"status": s, "n": n, "members": m or 0, "avg_conf": round(c or 0, 2)}
        for s, n, m, c in _rows(conn,
            "SELECT status, COUNT(*), SUM(member_count), AVG(confidence) "
            "FROM topic_brief GROUP BY status ORDER BY 2 DESC")
    ]
    out["projects"] = [
        {"slug": slug, "n": n}
        for slug, n in _rows(conn,
            "SELECT project_slug, COUNT(*) FROM cluster_project_map "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 22")
    ]

    # ── comms: channel-class mix + incident timeline ─────────────────────────
    out["channel_class"] = [
        {"cls": c or "(unset)", "threads": t, "msgs": m or 0}
        for c, t, m in _rows(conn,
            "SELECT channel_class, COUNT(*), SUM(msg_count) FROM thread_summary "
            "GROUP BY 1 ORDER BY 3 DESC LIMIT 10")
    ]
    ops = _rows(conn, """
        SELECT substr(first_ts,1,7) mo, ops_pattern_match pat, COUNT(*) n
        FROM thread_summary
        WHERE ops_pattern_match IS NOT NULL AND first_ts >= '2025-01'
        GROUP BY mo, pat ORDER BY mo
    """)
    pats = ["incident", "drill", "rca", "rollback", "year_end"]
    by_mo2: dict[str, dict] = {}
    for mo, pat, n in ops:
        d = by_mo2.setdefault(mo, {"month": mo, **{p: 0 for p in pats}})
        if pat in d:
            d[pat] = n
    out["ops_timeline"] = list(by_mo2.values())

    out["thread_sizes"] = [
        {"bucket": b, "n": n} for b, n in _rows(conn, """
        SELECT CASE WHEN reply_count=0 THEN '0'
                    WHEN reply_count<5 THEN '1-4'
                    WHEN reply_count<20 THEN '5-19'
                    WHEN reply_count<50 THEN '20-49'
                    ELSE '50+' END b,
               COUNT(*) n
        FROM thread_summary GROUP BY b ORDER BY MIN(reply_count)
    """)]

    # headline totals for the section intro
    out["totals"] = {
        "events": _rows(conn, "SELECT COUNT(*) FROM events")[0][0],
        "prs": _rows(conn, "SELECT COUNT(*) FROM pr_meta")[0][0],
        "releases": _rows(conn, "SELECT COUNT(*) FROM feature_release")[0][0],
        "clusters": _rows(conn, "SELECT COUNT(*) FROM topic_brief")[0][0],
        "threads": _rows(conn, "SELECT COUNT(*) FROM thread_summary")[0][0],
        "span": list(_rows(conn,
            "SELECT MIN(substr(ts,1,10)), MAX(substr(ts,1,10)) FROM events")[0]),
    }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(str(DB_PATH))
    payload = build(conn)
    conn.close()
    text = json.dumps(payload, separators=(",", ":"))
    if args.stdout:
        print(text)
    else:
        OUT.write_text(text)
        print(f"wrote {OUT} ({len(text):,} bytes)")


if __name__ == "__main__":
    main()

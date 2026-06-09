#!/usr/bin/env python3
"""pr_quality_report.py — read-only data bundle for the /pr-report skill.

Phase 5 of the PR-quality scorer (PRD: prd/pr-quality-scorer.md). No LLM, no
writes. Emits a compact JSON bundle the /pr-report chat turn renders into a
stakeholder report (per-PR lines, per-dev coaching, human-vs-agentic coverage
gap + bridging suggestions).

All interpretation lives in derive/github_metrics.py; this only assembles.

Usage:
    python derive/pr_quality_report.py --since 2026-04-01
    python derive/pr_quality_report.py            # all merged PRs
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from derive.github_metrics import (  # noqa: E402
    get_conn, merged_prs, aggregate_by_dev, coverage_gap, CATEGORY_WEIGHTS,
    pr_title_url,
)

TOP_N = 20


def _band(score: float) -> str:
    if score == 0:
        return "clean"
    if score < 10:
        return "low"
    if score < 25:
        return "moderate"
    if score < 50:
        return "high"
    return "severe"


def build_report(conn, since: str | None, team_only: bool = True) -> dict:
    prs = merged_prs(conn, since=since, team_only=team_only)
    subjects = {pr["subject"] for pr in prs}

    # Pull computed friction rows for the windowed PRs.
    rows = conn.execute("SELECT subject, score, dominant_category, mechanical_json, "
                        "category_counts_json FROM pr_friction").fetchall()
    rows = [r for r in rows if r["subject"] in subjects]

    bands: dict[str, int] = {}
    top: list[dict] = []
    for r in rows:
        bands[_band(r["score"])] = bands.get(_band(r["score"]), 0) + 1
        mech = json.loads(r["mechanical_json"] or "{}")
        title, url = pr_title_url(conn, r["subject"])
        top.append({
            "subject": r["subject"],
            "title": title,
            "url": url,
            "score": r["score"],
            "dominant_category": r["dominant_category"],
            "author": mech.get("author"),
            "ttm_hours": mech.get("ttm_hours"),
            "changes_requested": mech.get("changes_requested"),
            "rework_commits": mech.get("rework_commits"),
            "review_rounds": mech.get("review_rounds"),
            "category_counts": json.loads(r["category_counts_json"] or "{}"),
        })
    top.sort(key=lambda x: x["score"], reverse=True)

    # Classification coverage caveat: coverage-gap + category signals are only
    # meaningful for PRs whose comments have been classified.
    classified = conn.execute("SELECT COUNT(*) FROM pr_comment_class").fetchone()[0]
    prs_with_class = conn.execute(
        "SELECT COUNT(DISTINCT subject) FROM pr_comment_class").fetchone()[0]

    # Trim per-dev subject lists — the chat needs counts + categories, not every
    # PR id (a prolific author can have 70+ and bloats the bundle).
    per_dev = aggregate_by_dev(conn, since=since, team_only=team_only)
    for d in per_dev.values():
        d["example_subjects"] = d.pop("subjects", [])[:5]

    return {
        "window": since or "all",
        "scope": "team" if team_only else "all-authors",
        "merged_prs": len(prs),
        "scored": len(rows),
        "friction_bands": bands,
        "top_friction": top[:TOP_N],
        "per_dev": per_dev,
        "coverage_gap": coverage_gap(conn, since=since, team_only=team_only),
        "classification_coverage": {
            "classified_comments": classified,
            "prs_with_any_classification": prs_with_class,
            "note": "coverage_gap + dominant categories are mechanical-only for "
                    "PRs not yet classified; run /pr-quality to deepen.",
        },
        "category_weights": CATEGORY_WEIGHTS,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="merged_at >= ISO date (e.g. 2026-04-01)")
    ap.add_argument("--all-authors", action="store_true",
                    help="org-wide view, not just scope:team authors (default: team only)")
    args = ap.parse_args()
    conn = get_conn()
    print(json.dumps(build_report(conn, args.since, team_only=not args.all_authors),
                     indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

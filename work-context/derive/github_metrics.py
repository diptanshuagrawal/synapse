"""Shared GitHub PR-interpretation primitives for derive/* skills.

Single source of truth for post-merge PR friction scoring (PRD:
prd/pr-quality-scorer.md). ALL PR-interpretation logic any skill needs lives
HERE. Skills consume, do not reimplement. Peer to derive/jira_metrics.py.

Consumed by:
- `.claude/commands/pr-quality.md`  (classify + report)
- run directly to (re)populate the pr_friction table.

What it computes:
- Mechanical signals (no LLM): review rounds, changes-requested cycles, rework
  commits after first HUMAN review, time-to-merge, comment counts.
- Friction score (0-100) per merged PR + dominant reason.
- Category-weighted comment friction once pr_comment_class is populated by the
  /pr-quality classify pass (Phase 4). Until then, scores are mechanical-only.
- Per-dev aggregation + human-vs-agentic coverage gap.

Design notes baked in (per PRD decisions):
- Rework window starts at the FIRST HUMAN review. Bot reviews (matterai,
  github-actions, codecov, …) arrive near-instantly and would zero the window.
- Human comments weigh ABOVE matterai comments (SOURCE_WEIGHT).
- nits/naming are near-zero; business-logic/correctness/security dominate.
- CI status is captured but NOT scored yet: most head-sha "failure" rows are
  non-gating checks (codecov, BVT deploy-cohort, build-and-push), so raw
  checks_status is too noisy to weight. ci_gating_failed is surfaced in
  mechanical_json for transparency, weight 0 pending the gating-semantics call.

Usage from a skill's Python block:

    import sys; sys.path.insert(0, '$HOME/context/work-context')
    from derive.github_metrics import (
        merged_prs, compute_friction, populate_pr_friction,
        aggregate_by_dev, coverage_gap,
    )
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "index" / "events.db"

# Reuse the canonical people map from the Jira module — same people.yaml.
import sys
sys.path.insert(0, str(ROOT))
from derive.jira_metrics import load_people_lookup, PEOPLE_YAML  # noqa: E402
import yaml  # noqa: E402


def team_canonicals() -> set[str]:
    """Canonical handles of people with scope == 'team' — the owner's team.

    The friction/quality views default to this set (team_only). Everyone else
    is org-wide chatter we score on demand via --all-authors.
    """
    with open(PEOPLE_YAML) as f:
        data = yaml.safe_load(f).get("people", [])
    return {p["canonical"] for p in data
            if p.get("scope") == "team" and p.get("canonical")}


def team_login_set() -> set[str]:
    """Lowercased logins/aliases/handles of scope:team people, for SQL IN().

    Covers the scalar identity fields plus the github_aliases / git_names lists
    so a PR-author lifecycle actor (a github login) matches regardless of which
    variant people.yaml recorded.
    """
    with open(PEOPLE_YAML) as f:
        data = yaml.safe_load(f).get("people", [])
    out: set[str] = set()
    for p in data:
        if p.get("scope") != "team":
            continue
        for k in ("github", "canonical", "email", "slack_handle", "name", "git_name"):
            v = p.get(k)
            if v:
                out.add(str(v).lower().strip())
        for lst in ("github_aliases", "git_names"):
            for v in (p.get(lst) or []):
                out.add(str(v).lower().strip())
    return out


def is_team_author(author: Optional[str], lookup: dict[str, str],
                   team: set[str]) -> bool:
    """True if a PR-author login resolves to a scope:team canonical."""
    canon = lookup.get((author or "").lower().strip())
    return canon in team


# ──────────────────────────────────────────────────────────────────────────
# Scoring constants (tunable; PRD marks weights "TBD in validation")
# ──────────────────────────────────────────────────────────────────────────

# Root-cause taxonomy weights (config/pr_review_rules.md is the canonical doc).
CATEGORY_WEIGHTS: dict[str, float] = {
    "business-logic": 1.0,
    "correctness":    1.0,
    "security":       1.0,
    "design":         0.6,
    "test-gap":       0.6,
    "question":       0.3,
    "naming":         0.1,
    "nit":            0.1,
    "praise":         0.0,
}
# Human comments are stronger signal than a bot flag.
SOURCE_WEIGHT: dict[str, float] = {"human": 1.0, "matterai": 0.5, "claude": 0.5}

# Mechanical component weights.
W_CHANGES_REQUESTED = 8.0   # per CHANGES_REQUESTED human review
W_EXTRA_ROUND       = 4.0   # per human review round beyond the first
W_REWORK_COMMIT     = 3.0   # per commit pushed after first human review
W_CATEGORY          = 10.0  # multiplier on category-weighted comment mass
TTM_SLOW_HOURS      = 120.0 # >5 days to merge → small penalty
TTM_SLOW_PENALTY    = 8.0

# Substrings identifying a *gating* (merge-blocking) check vs noise.
GATING_CHECK_HINTS = ("ci-gating", "test-and-quality", "run-test-suite", "lint")

# Actors that are not humans. Bots all carry the GitHub "[bot]" suffix.
def is_bot(actor: Optional[str]) -> bool:
    return bool(actor) and actor.endswith("[bot]")


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def get_conn(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _review_state(title: Optional[str]) -> str:
    """Parse review state from the canonical title 'Review on #N: STATE'."""
    if not title or ": " not in title:
        return ""
    return title.rsplit(": ", 1)[-1].strip().upper()


def pr_author(conn: sqlite3.Connection, subject: str) -> Optional[str]:
    """Github login of the PR author (the lifecycle-event actor)."""
    row = conn.execute(
        "SELECT actor FROM events WHERE subject = ? "
        "AND event_type IN ('pr_merged','pr_opened','pr_closed') "
        "AND actor IS NOT NULL LIMIT 1",
        (subject,),
    ).fetchone()
    return row["actor"] if row else None


def pr_title_url(conn: sqlite3.Connection, subject: str) -> tuple[Optional[str], str]:
    """(title, url) for a PR. Title from the pr_opened/pr_merged event; url
    falls back to a constructed github URL when the event row lacks one.

    subject is 'owner/repo#N' — split on '#' to build the canonical pull URL.
    """
    row = conn.execute(
        "SELECT title, url FROM events WHERE subject = ? "
        "AND event_type IN ('pr_opened','pr_merged') "
        "ORDER BY (event_type='pr_opened') DESC LIMIT 1",
        (subject,),
    ).fetchone()
    title = row["title"] if row and row["title"] else None
    url = row["url"] if row and row["url"] else None
    if not url and "#" in subject:
        repo, num = subject.split("#", 1)
        url = f"https://github.com/{repo}/pull/{num}"
    return title, url


# ──────────────────────────────────────────────────────────────────────────
# PR selection
# ──────────────────────────────────────────────────────────────────────────

def merged_prs(conn: sqlite3.Connection, since: Optional[str] = None,
               until: Optional[str] = None, repo: Optional[str] = None,
               team_only: bool = True) -> list[sqlite3.Row]:
    """Merged PRs from pr_meta, optionally windowed by merged_at / repo.

    team_only (default True): keep only PRs whose author resolves to a
    scope:team canonical. Pass team_only=False for the org-wide view.
    """
    q = "SELECT * FROM pr_meta WHERE state = 'merged'"
    args: list = []
    if since:
        q += " AND merged_at >= ?"; args.append(since)
    if until:
        q += " AND merged_at <= ?"; args.append(until)
    if repo:
        q += " AND repo = ?"; args.append(repo)
    q += " ORDER BY merged_at DESC"
    rows = conn.execute(q, args).fetchall()
    if team_only:
        lookup = load_people_lookup()
        team = team_canonicals()
        rows = [r for r in rows
                if is_team_author(pr_author(conn, r["subject"]), lookup, team)]
    return rows


# ──────────────────────────────────────────────────────────────────────────
# Mechanical signals (no LLM)
# ──────────────────────────────────────────────────────────────────────────

def first_human_review_ts(conn: sqlite3.Connection, subject: str,
                          author: Optional[str]) -> Optional[datetime]:
    """Earliest review/comment by a human who is not the PR author."""
    rows = conn.execute(
        "SELECT actor, ts FROM events WHERE subject = ? "
        "AND event_type IN ('review','comment','issue_comment')",
        (subject,),
    ).fetchall()
    best: Optional[datetime] = None
    for r in rows:
        actor = r["actor"]
        if is_bot(actor) or (author and actor == author):
            continue
        t = _parse_ts(r["ts"])
        if t and (best is None or t < best):
            best = t
    return best


def mechanical_signals(conn: sqlite3.Connection, pr: sqlite3.Row) -> dict:
    """Pure mechanical friction signals for one merged PR."""
    subject = pr["subject"]
    author = pr_author(conn, subject)
    fhr = first_human_review_ts(conn, subject, author)

    # Reviews (human only) + changes-requested cycles.
    review_rows = conn.execute(
        "SELECT actor, title FROM events WHERE subject = ? AND event_type = 'review'",
        (subject,),
    ).fetchall()
    human_reviews = [r for r in review_rows if not is_bot(r["actor"]) and r["actor"] != author]
    review_rounds = len(human_reviews)
    changes_requested = sum(1 for r in human_reviews if _review_state(r["title"]) == "CHANGES_REQUESTED")

    # Human discussion comments (inline + issue), excluding the author.
    crow = conn.execute(
        "SELECT actor FROM events WHERE subject = ? AND event_type IN ('comment','issue_comment')",
        (subject,),
    ).fetchall()
    human_comments = sum(1 for r in crow if not is_bot(r["actor"]) and r["actor"] != author)

    # Rework commits: commit_in_pr pushed after the first human review.
    rework_commits = 0
    if fhr:
        for r in conn.execute(
            "SELECT ts FROM events WHERE subject = ? AND event_type = 'commit_in_pr'",
            (subject,),
        ).fetchall():
            t = _parse_ts(r["ts"])
            if t and t > fhr:
                rework_commits += 1

    # Time to merge.
    created, merged = _parse_ts(pr["created_at"]), _parse_ts(pr["merged_at"])
    ttm_hours = round((merged - created).total_seconds() / 3600, 1) if (created and merged) else None

    # CI (captured, not scored — see module docstring).
    failed = []
    try:
        failed = json.loads(pr["checks_failed_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        failed = []
    ci_gating_failed = any(
        any(h in (name or "").lower() for h in GATING_CHECK_HINTS) for name in failed
    )

    return {
        "author": author,
        "review_rounds": review_rounds,
        "changes_requested": changes_requested,
        "human_comments": human_comments,
        "rework_commits": rework_commits,
        "ttm_hours": ttm_hours,
        "additions": pr["additions"],
        "deletions": pr["deletions"],
        "files_changed": pr["files_changed"],
        "checks_status": pr["checks_status"],
        "ci_gating_failed": ci_gating_failed,
    }


# ──────────────────────────────────────────────────────────────────────────
# Comment classification (read pr_comment_class; empty until Phase 4)
# ──────────────────────────────────────────────────────────────────────────

def category_counts(conn: sqlite3.Connection, subject: str) -> dict[str, dict[str, int]]:
    """{category: {human: n, matterai: n}} from pr_comment_class for one PR."""
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        "SELECT category, source, COUNT(*) AS n FROM pr_comment_class "
        "WHERE subject = ? GROUP BY category, source",
        (subject,),
    ).fetchall():
        out.setdefault(r["category"], {})[r["source"]] = r["n"]
    return out


# ──────────────────────────────────────────────────────────────────────────
# Friction score
# ──────────────────────────────────────────────────────────────────────────

def compute_friction(conn: sqlite3.Connection, pr: sqlite3.Row) -> dict:
    """Composite friction for one merged PR. Mechanical-only until classified."""
    mech = mechanical_signals(conn, pr)
    cats = category_counts(conn, pr["subject"])

    raw = 0.0
    raw += W_CHANGES_REQUESTED * mech["changes_requested"]
    raw += W_EXTRA_ROUND * max(0, mech["review_rounds"] - 1)
    raw += W_REWORK_COMMIT * mech["rework_commits"]
    if mech["ttm_hours"] and mech["ttm_hours"] > TTM_SLOW_HOURS:
        raw += TTM_SLOW_PENALTY

    # Category-weighted comment mass (human weighted above matterai).
    cat_contrib: dict[str, float] = {}
    for cat, by_src in cats.items():
        w = CATEGORY_WEIGHTS.get(cat, 0.3)
        mass = sum(SOURCE_WEIGHT.get(src, 0.5) * n for src, n in by_src.items())
        c = W_CATEGORY * w * mass
        cat_contrib[cat] = c
        raw += c

    # Normalize by change size (per-100-LOC), so a 20-comment 1000-LOC PR isn't
    # punished like a 20-comment 30-LOC PR. Floor avoids divide-by-zero / spikes.
    loc = (mech["additions"] or 0) + (mech["deletions"] or 0)
    size_factor = max(1.0, (loc / 100.0)) ** 0.5  # gentle sqrt damping
    score = min(100.0, raw / size_factor)

    # Dominant reason: the heaviest category if classified, else a mechanical tag.
    if cat_contrib:
        dominant = max(cat_contrib, key=cat_contrib.get)
    elif mech["changes_requested"] > 0 or mech["rework_commits"] > 2:
        dominant = "rework"
    elif mech["ttm_hours"] and mech["ttm_hours"] > TTM_SLOW_HOURS:
        dominant = "slow-merge"
    elif mech["human_comments"] == 0 and mech["review_rounds"] <= 1:
        dominant = "clean"
    else:
        dominant = "discussion"

    return {
        "subject": pr["subject"],
        "score": round(score, 1),
        "dominant_category": dominant,
        "mechanical": mech,
        "category_counts": cats,
    }


def populate_pr_friction(conn: sqlite3.Connection, since: Optional[str] = None,
                         repo: Optional[str] = None, team_only: bool = True) -> int:
    """Compute + upsert pr_friction for all (windowed) merged PRs. Returns count."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    n = 0
    for pr in merged_prs(conn, since=since, repo=repo, team_only=team_only):
        f = compute_friction(conn, pr)
        conn.execute(
            """
            INSERT INTO pr_friction (subject, score, dominant_category,
                                     mechanical_json, category_counts_json, computed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject) DO UPDATE SET
                score=excluded.score, dominant_category=excluded.dominant_category,
                mechanical_json=excluded.mechanical_json,
                category_counts_json=excluded.category_counts_json,
                computed_at=excluded.computed_at
            """,
            (f["subject"], f["score"], f["dominant_category"],
             json.dumps(f["mechanical"]), json.dumps(f["category_counts"]), now),
        )
        n += 1
    conn.commit()
    return n


# ──────────────────────────────────────────────────────────────────────────
# Aggregation: per-dev + coverage gap (data only; skill renders prose)
# ──────────────────────────────────────────────────────────────────────────

def aggregate_by_dev(conn: sqlite3.Connection, since: Optional[str] = None,
                     team_only: bool = True) -> dict[str, dict]:
    """Per-author rollup: PR count, avg score, dominant-category frequencies.

    Author github login → canonical via people.yaml. The skill turns the
    category frequencies into 1-2 coaching actionables.
    """
    lookup = load_people_lookup()
    agg: dict[str, dict] = {}
    for pr in merged_prs(conn, since=since, team_only=team_only):
        f = compute_friction(conn, pr)
        author = f["mechanical"]["author"]
        canon = lookup.get((author or "").lower(), author or "unknown")
        d = agg.setdefault(canon, {"prs": 0, "score_sum": 0.0, "categories": {}, "subjects": []})
        d["prs"] += 1
        d["score_sum"] += f["score"]
        d["subjects"].append(f["subject"])
        for cat, by_src in f["category_counts"].items():
            d["categories"][cat] = d["categories"].get(cat, 0) + sum(by_src.values())
    for d in agg.values():
        d["avg_score"] = round(d["score_sum"] / d["prs"], 1) if d["prs"] else 0.0
        del d["score_sum"]
    return agg


def coverage_gap(conn: sqlite3.Connection, since: Optional[str] = None,
                 team_only: bool = True) -> dict[str, dict[str, int]]:
    """Human-vs-agentic coverage per category, in three buckets.

    For each PR×category: bot_covered (both flagged), human_only (humans only —
    the real gap), bot_only (matterai only — value-add or noise). Empty until
    pr_comment_class is populated by the classify pass.
    """
    subjects = [pr["subject"] for pr in merged_prs(conn, since=since, team_only=team_only)]
    buckets: dict[str, dict[str, int]] = {}
    for subj in subjects:
        cats = category_counts(conn, subj)
        for cat, by_src in cats.items():
            b = buckets.setdefault(cat, {"bot_covered": 0, "human_only": 0, "bot_only": 0})
            has_h = by_src.get("human", 0) > 0
            # bot = MatterAI (legacy) OR Claude Code Review (current).
            has_b = (by_src.get("matterai", 0) + by_src.get("claude", 0)) > 0
            if has_h and has_b:
                b["bot_covered"] += 1
            elif has_h:
                b["human_only"] += 1
            elif has_b:
                b["bot_only"] += 1
    return buckets


# ──────────────────────────────────────────────────────────────────────────
# CLI: (re)populate pr_friction
# ──────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Compute pr_friction (mechanical + any classified)")
    ap.add_argument("--since", help="Only merged_at >= this ISO date (e.g. 2026-04-01)")
    ap.add_argument("--repo", help="Limit to one repo")
    ap.add_argument("--all-authors", action="store_true",
                    help="score org-wide PRs, not just scope:team authors (default: team only)")
    args = ap.parse_args()
    conn = get_conn()
    n = populate_pr_friction(conn, since=args.since, repo=args.repo,
                             team_only=not args.all_authors)
    print(f"pr_friction: computed {n} merged PRs"
          + ("" if args.all_authors else " (team only)"))


if __name__ == "__main__":
    main()

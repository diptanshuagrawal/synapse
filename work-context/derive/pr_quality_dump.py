#!/usr/bin/env python3
"""pr_quality_dump.py — dump PR review/comment bodies for /pr-quality classify.

Phase 4 of the PR-quality scorer (PRD: prd/pr-quality-scorer.md). Mirrors
derive/leaves_dump.py: a no-LLM dump that prepares pending items for the
chat-classify pass.

Selects review/comment events on MERGED PRs that:
  - have a non-empty body,
  - are authored by a HUMAN reviewer or matterai (other bots — github-actions,
    codecoverage, qa-bvt — are CI chatter, excluded),
  - are NOT the PR author's own comments (those are replies, not review signal),
  - are NOT already classified (event_id absent from pr_comment_class).

Windowed (--since-days) and chunked (--limit) because there are thousands of
comments; the /pr-quality skill classifies incrementally across runs.

Writes state/pending_pr_comments.json + state/pending_pr_comments.rules.md.
No LLM.

Usage:
    python derive/pr_quality_dump.py --since-days 45
    python derive/pr_quality_dump.py --since-days 45 --limit 200
    python derive/pr_quality_dump.py --all --limit 400
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import get_db  # noqa: E402
from derive.github_metrics import team_login_set  # noqa: E402
from derive.sources_config import matterai_bot  # noqa: E402

STATE_DIR = _REPO_ROOT / "state"
PENDING_JSON = STATE_DIR / "pending_pr_comments.json"
RULES_MD = STATE_DIR / "pending_pr_comments.rules.md"
RULES_SRC = _REPO_ROOT / "config" / "pr_review_rules.md"

MATTERAI = matterai_bot()
BODY_MAX = 1200  # enough context to classify; matterai comments can be long
DEFAULT_LIMIT = 300

# MatterAI posts PR-level meta as ordinary comments (event_type='comment'):
# a badge-headed summary, "PR Review Skipped", "couldn't complete the analysis".
# These are not per-comment review findings — drop them so classification sees
# only substantive feedback. Humans don't post shields.io badges, so it's safe
# to apply to all sources. SQL LIKE keeps the dump + remaining-count consistent.
NOISE_LIKE = [
    "%img.shields.io%",          # badge-headed summary / status post
    "%PR Review Skipped%",
    "%complete the analysis%",   # "I couldn't complete the analysis…"
    "%review skipped as per%",
]
_NOISE_SQL = " ".join(f"AND e.body NOT LIKE '{p}'" for p in NOISE_LIKE)

# Review events whose body is just the state echo carry no classifiable content
# (the substance is in the inline comments). Skipping them stops these from
# re-surfacing in every dump as un-taggable low-confidence noise. The review
# ROUND is still counted mechanically in github_metrics, so nothing is lost.
REVIEW_ECHOES = ("requested changes.", "requested changes", "approved.",
                 "approved", "approving", "approving.", "approving these changes.")
_ECHO_SQL = ("AND NOT (e.event_type = 'review' AND lower(trim(e.body)) IN ("
             + ",".join("'" + s.replace("'", "''") + "'" for s in REVIEW_ECHOES) + "))")


def _team_sql() -> str:
    """Restrict to comments on PRs authored by a scope:team member.

    PR author = the lifecycle-event actor (pr_opened/pr_merged). Matches the
    author's login against the scope:team login set from people.yaml. Returns
    '' when there are no team logins (fail-open rather than dump nothing).
    """
    logins = sorted(team_login_set())
    if not logins:
        return ""
    in_list = ",".join("'" + s.replace("'", "''") + "'" for s in logins)
    return (" AND EXISTS (SELECT 1 FROM events la WHERE la.subject = e.subject "
            "AND la.event_type IN ('pr_opened','pr_merged') AND la.actor IS NOT NULL "
            f"AND lower(la.actor) IN ({in_list}))")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-days", type=int, help="only comments newer than N days")
    ap.add_argument("--all", action="store_true", help="no date window")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"max items to dump this chunk (default {DEFAULT_LIMIT})")
    ap.add_argument("--all-authors", action="store_true",
                    help="classify org-wide PRs, not just scope:team authors (default: team only)")
    args = ap.parse_args()
    if not args.all and args.since_days is None:
        ap.error("pick a window: --since-days N or --all")

    team_sql = "" if args.all_authors else _team_sql()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()

    where_ts, params = "", []
    if args.since_days is not None:
        since = (datetime.now(timezone.utc) - timedelta(days=args.since_days)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        where_ts = "AND e.ts >= ?"
        params.append(since)

    # Human OR matterai; exclude other bots; exclude the PR author's own comments;
    # only merged PRs; non-empty body; not already classified.
    q = f"""
        SELECT e.id, e.subject, e.actor, e.ts, e.event_type, e.title, e.body, e.url
        FROM events e
        WHERE e.source = 'github'
          AND e.event_type IN ('review','comment','issue_comment')
          AND length(trim(COALESCE(e.body,''))) > 0
          AND (e.actor = ? OR e.actor NOT LIKE '%[bot]')
          {_NOISE_SQL}
          {_ECHO_SQL}
          {where_ts}
          AND EXISTS (SELECT 1 FROM pr_meta m WHERE m.subject = e.subject AND m.state = 'merged')
          AND e.actor NOT IN (
              SELECT a.actor FROM events a
              WHERE a.subject = e.subject
                AND a.event_type IN ('pr_merged','pr_opened','pr_closed')
                AND a.actor IS NOT NULL
          )
          {team_sql}
          AND e.id NOT IN (SELECT event_id FROM pr_comment_class)
        ORDER BY e.ts ASC
        LIMIT ?
    """
    rows = conn.execute(q, [MATTERAI, *params, args.limit]).fetchall()

    pending = []
    for r in rows:
        body = (r["body"] or "").strip()
        excerpt = body[:BODY_MAX] + ("…" if len(body) > BODY_MAX else "")
        pending.append({
            "event_id": r["id"],
            "subject": r["subject"],
            "source": "matterai" if r["actor"] == MATTERAI else "human",
            "actor": r["actor"],
            "ts": r["ts"],
            "kind": r["event_type"],
            "body_excerpt": excerpt,
            "url": r["url"],
        })

    # Remaining unclassified count (for chunking visibility).
    remaining = conn.execute(
        f"""SELECT COUNT(*) FROM events e
            WHERE e.source='github' AND e.event_type IN ('review','comment','issue_comment')
              AND length(trim(COALESCE(e.body,'')))>0
              AND (e.actor=? OR e.actor NOT LIKE '%[bot]')
              {_NOISE_SQL}
              {_ECHO_SQL}
              {where_ts}
              AND EXISTS (SELECT 1 FROM pr_meta m WHERE m.subject=e.subject AND m.state='merged')
              AND e.actor NOT IN (SELECT a.actor FROM events a WHERE a.subject=e.subject
                                  AND a.event_type IN ('pr_merged','pr_opened','pr_closed') AND a.actor IS NOT NULL)
              {team_sql}
              AND e.id NOT IN (SELECT event_id FROM pr_comment_class)""",
        [MATTERAI, *params],
    ).fetchone()[0]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": f"last {args.since_days}d" if args.since_days is not None else "all",
        "scope": "all-authors" if args.all_authors else "team",
        "dumped": len(pending),
        "remaining_after_chunk": max(0, remaining - len(pending)),
        "pending": pending,
    }
    PENDING_JSON.write_text(json.dumps(payload, indent=2, sort_keys=False))
    # Rules file is the canonical taxonomy doc, copied verbatim for the chat turn.
    RULES_MD.write_text(RULES_SRC.read_text())

    print(f"[out] wrote {PENDING_JSON.name} + {RULES_MD.name}")
    if not pending:
        print("[summary] nothing to classify")
    else:
        print(f"[summary] {len(pending)} comments to classify "
              f"({payload['remaining_after_chunk']} remain after this chunk)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

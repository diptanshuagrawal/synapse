"""
sample_subjects.py — pick a subset of subjects from events.db for offline work
(validation runs, pilot enrichment, debugging).

Production usage embeds EVERYTHING; sampling is only for validation /
small-batch eyeball before committing to full-corpus LLM spend.

CLI
---
    .venv/bin/python derive/sample_subjects.py \\
        --target-size 50 \\
        --source-ratios "slack=0.6,jira=0.2,confluence=0.1,github=0.1" \\
        --bias has_replies,most_referenced \\
        --must-include slack:C0EXAMPLE:1747967524.701529 EX-2891 page:EXAMPLE_PAGE_ID \\
        --seed 42

    Prints one subject per line. Pipe to anything.

Python
------
    from derive.sample_subjects import sample
    subjects = sample(conn, target_size=50, ...)
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from pathlib import Path
from typing import Optional

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db  # noqa: E402


def _candidates(conn: sqlite3.Connection, source: str, bias: list[str]) -> list[tuple[str, float]]:
    """Return list of (subject, weight) for a source.

    weight = relative likelihood of being sampled. Bias keywords shift it:
      has_replies        → slack: parents with reply_count > 0 weighted 3x
      most_referenced    → jira/page: incoming event_refs count weighted log-scale
      recent             → ts in last 90d weighted 2x
    """
    bias_set = set(bias or [])
    out: list[tuple[str, float]] = []

    if source == "slack":
        rows = conn.execute(
            """SELECT subject, COALESCE(reply_count, 0) AS rc, ts FROM events
                WHERE source='slack' AND event_type='thread_started'
                  AND deleted_ts IS NULL"""
        ).fetchall()
        for subj, rc, ts in rows:
            w = 1.0
            if "has_replies" in bias_set and rc and rc > 0:
                w *= 3.0
            if "recent" in bias_set and ts and ts > "2026-02-19":
                w *= 2.0
            out.append((subj, w))

    elif source == "jira":
        # weight by inbound ref count (popularity proxy)
        rows = conn.execute(
            """SELECT e.subject, COUNT(r.ref_value) AS inbound, MAX(e.ts) AS ts
                FROM events e
                LEFT JOIN event_refs r ON r.ref_type='ticket' AND r.ref_value=e.subject
                WHERE e.source='jira' AND e.event_type='issue_created'
                GROUP BY e.subject"""
        ).fetchall()
        for subj, inbound, ts in rows:
            w = 1.0
            if "most_referenced" in bias_set:
                w *= 1.0 + (inbound or 0) ** 0.5  # log-ish
            if "recent" in bias_set and ts and ts > "2026-02-19":
                w *= 2.0
            out.append((subj, w))

    elif source == "confluence":
        rows = conn.execute(
            """SELECT e.subject, COUNT(r.ref_value) AS inbound, MAX(e.ts) AS ts
                FROM events e
                LEFT JOIN event_refs r ON r.ref_type='page' AND r.ref_value=replace(e.subject,'page:','')
                WHERE e.source='confluence'
                GROUP BY e.subject"""
        ).fetchall()
        for subj, inbound, ts in rows:
            w = 1.0
            if "most_referenced" in bias_set:
                w *= 1.0 + (inbound or 0) ** 0.5
            if "recent" in bias_set and ts and ts > "2026-02-19":
                w *= 2.0
            out.append((subj, w))

    elif source == "github":
        # GitHub subjects come in two shapes:
        #   owner/repo#N   — pull requests (the canonical PR subject)
        #   owner/repo@SHA — raw commits (NOT embeddable as standalone work units)
        # Many PRs are ingested via `pr_merged` / `commit_in_pr` / `review` events
        # but never had a `pr_opened` row in the DB. Filtering on event_type='pr_opened'
        # alone misses ~85% of PR subjects. Pick one row per PR subject by MIN(ts),
        # using ANY PR-related event_type. Content guard moved to embed_subjects.py
        # via the subject_content extractor; sampler should NOT pre-filter on body
        # since some PRs only carry meaningful content in the matterai-summary
        # comment, not the pr_opened body.
        rows = conn.execute(
            """SELECT subject, MIN(ts) AS first_ts FROM events
                WHERE source='github'
                  AND subject LIKE '%#%'
                  AND event_type IN (
                      'pr_opened','pr_merged','pr_closed','review','comment','commit_in_pr'
                  )
                GROUP BY subject"""
        ).fetchall()
        for subj, ts in rows:
            w = 1.0
            if "recent" in bias_set and ts and ts > "2026-02-19":
                w *= 2.0
            out.append((subj, w))

    elif source == "service":
        # Service-brief chunks (source='service'); uniform weight — reference
        # docs, not popularity-ranked activity.
        rows = conn.execute(
            """SELECT subject, MAX(ts) AS ts FROM events
                WHERE source='service' AND event_type='service_brief'
                GROUP BY subject"""
        ).fetchall()
        for subj, ts in rows:
            out.append((subj, 1.0))

    return out


def _corpus_ratio(conn: sqlite3.Connection) -> dict[str, float]:
    """Fallback ratios = proportion of embeddable subjects per source in corpus."""
    counts = {s: 0 for s in ("slack", "jira", "confluence", "github", "service")}
    counts["slack"] = conn.execute(
        "SELECT COUNT(*) FROM events WHERE source='slack' AND event_type='thread_started'"
    ).fetchone()[0]
    counts["jira"] = conn.execute(
        "SELECT COUNT(DISTINCT subject) FROM events WHERE source='jira' AND event_type='issue_created'"
    ).fetchone()[0]
    counts["confluence"] = conn.execute(
        "SELECT COUNT(DISTINCT subject) FROM events WHERE source='confluence'"
    ).fetchone()[0]
    counts["github"] = conn.execute(
        """SELECT COUNT(DISTINCT subject) FROM events
            WHERE source='github'
              AND subject LIKE '%#%'
              AND event_type IN (
                  'pr_opened','pr_merged','pr_closed','review','comment','commit_in_pr'
              )"""
    ).fetchone()[0]
    counts["service"] = conn.execute(
        "SELECT COUNT(DISTINCT subject) FROM events WHERE source='service' AND event_type='service_brief'"
    ).fetchone()[0]
    total = sum(counts.values()) or 1
    return {k: v / total for k, v in counts.items()}


def sample(
    conn: sqlite3.Connection,
    target_size: Optional[int] = None,
    source_ratios: Optional[dict[str, float]] = None,
    bias_to: Optional[list[str]] = None,
    must_include: Optional[list[str]] = None,
    seed: int = 42,
) -> list[str]:
    """Stratified weighted sample across sources.

    target_size       — None → return ALL embeddable subjects (production path)
    source_ratios     — None → auto-proportion to corpus distribution
                        else dict like {'slack': 0.6, 'jira': 0.2, ...}; must sum ~= 1
    bias_to           — list of bias keywords (see _candidates docstring)
    must_include      — subjects to force into output (won't double-count)
    seed              — RNG seed for reproducibility
    """
    rng = random.Random(seed)
    bias_to = bias_to or []
    must_include = list(must_include or [])

    sources = ("slack", "jira", "confluence", "github", "service")
    pools = {s: _candidates(conn, s, bias_to) for s in sources}

    # Production path — return EVERYTHING in stable order.
    if target_size is None:
        out = []
        for s in sources:
            out.extend(subj for subj, _ in pools[s])
        # Forced inclusions are already members if they exist; nothing to add.
        return out

    if source_ratios is None:
        source_ratios = _corpus_ratio(conn)

    # Normalise ratios.
    total = sum(source_ratios.values()) or 1
    source_ratios = {k: v / total for k, v in source_ratios.items()}

    # Reserve slots for must_include (no duplicates).
    must_set = set(must_include)
    remaining = max(0, target_size - len(must_set))

    chosen: list[str] = []
    chosen.extend(must_set)

    # Per-source target (rounded; we'll trim at end if over).
    per_source_target = {s: max(0, round(remaining * source_ratios.get(s, 0.0))) for s in sources}

    for s in sources:
        pool = [(subj, w) for subj, w in pools[s] if subj not in must_set]
        if not pool:
            continue
        n = min(per_source_target[s], len(pool))
        if n <= 0:
            continue
        subjects, weights = zip(*pool)
        picks = _weighted_sample_without_replacement(subjects, weights, n, rng)
        chosen.extend(picks)

    # Trim or pad to exact target_size.
    if len(chosen) > target_size:
        # Keep must_set; trim from the tail (random pool).
        must_in_chosen = [s for s in chosen if s in must_set]
        rest = [s for s in chosen if s not in must_set]
        rng.shuffle(rest)
        chosen = must_in_chosen + rest[: target_size - len(must_in_chosen)]
    elif len(chosen) < target_size:
        # Backfill from any unused pool entries.
        used = set(chosen)
        backfill_pool = [
            (subj, w) for s in sources for subj, w in pools[s] if subj not in used
        ]
        if backfill_pool:
            subjects, weights = zip(*backfill_pool)
            extras = _weighted_sample_without_replacement(
                subjects, weights, target_size - len(chosen), rng
            )
            chosen.extend(extras)

    return chosen


def _weighted_sample_without_replacement(
    items: list[str],
    weights: list[float],
    n: int,
    rng: random.Random,
) -> list[str]:
    """A-ExpJ algorithm (Efraimidis-Spirakis 2006). O(N) for any n ≤ N."""
    keys = []
    for it, w in zip(items, weights):
        if w <= 0:
            continue
        # Larger key = more likely to be in the top-n.
        u = rng.random()
        key = u ** (1.0 / w)
        keys.append((key, it))
    keys.sort(reverse=True)
    return [it for _, it in keys[:n]]


def _parse_ratios(s: str) -> dict[str, float]:
    out = {}
    for pair in s.split(","):
        k, v = pair.split("=")
        out[k.strip()] = float(v)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target-size", type=int, default=None,
                    help="Total subjects to return; omit for ALL (production path).")
    ap.add_argument("--source-ratios", type=str, default=None,
                    help="e.g. 'slack=0.6,jira=0.2,confluence=0.1,github=0.1'. "
                         "Omit to auto-proportion to corpus.")
    ap.add_argument("--bias", type=str, default="",
                    help="Comma-separated: has_replies,most_referenced,recent")
    ap.add_argument("--must-include", nargs="*", default=[],
                    help="Subjects to force into output.")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    source_ratios = _parse_ratios(args.source_ratios) if args.source_ratios else None
    bias = [b for b in args.bias.split(",") if b] if args.bias else []

    conn = get_db()
    subjects = sample(
        conn,
        target_size=args.target_size,
        source_ratios=source_ratios,
        bias_to=bias,
        must_include=args.must_include,
        seed=args.seed,
    )
    for s in subjects:
        print(s)


if __name__ == "__main__":
    main()

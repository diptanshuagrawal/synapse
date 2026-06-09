#!/usr/bin/env python3
"""
cluster_ownership_rollup.py — derive per-cluster ownership distribution.

For each cluster in `topic_brief`:
  - Join topic_brief_member → subject_summary on subject
  - Aggregate `owned_by_primary` counts across all members
  - Compute fractions; write to `topic_brief.owner_distribution_json` as
    {"team_id": fraction, ...}.
  - Members with NULL owned_by_primary bucketed as "(unowned)".

Consumers (retro skill, ask_engine) read `owner_distribution_json` and
pick the team they care about — e.g. retro filters by
`distribution.get("home-team", 0) >= 0.5` for highs.

Run AFTER `apply_verdicts.py` whenever subject-level ownership changes.

Usage:
    .venv/bin/python derive/cluster_ownership_rollup.py
    .venv/bin/python derive/cluster_ownership_rollup.py --dry-run
    .venv/bin/python derive/cluster_ownership_rollup.py --home-team home-team
        # also prints distribution stats for the home team
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH  # noqa: E402
from derive.sources_config import home_team  # noqa: E402


def _ensure_column(conn: sqlite3.Connection) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(topic_brief)").fetchall()]
    if "owner_distribution_json" not in cols:
        conn.execute("ALTER TABLE topic_brief ADD COLUMN owner_distribution_json TEXT")
        conn.commit()


def _cluster_ownership(conn: sqlite3.Connection) -> dict[int, dict[str, float]]:
    """Return {cluster_id: {team_id_or_unowned: fraction}}."""
    rows = conn.execute(
        """
        SELECT m.cluster_id, COALESCE(s.owned_by_primary, '(unowned)') AS owner
        FROM topic_brief_member m
        LEFT JOIN subject_summary s ON s.subject = m.subject
        """
    ).fetchall()

    per_cluster: dict[int, Counter] = {}
    for cluster_id, owner in rows:
        per_cluster.setdefault(cluster_id, Counter())[owner] += 1

    out: dict[int, dict[str, float]] = {}
    for cid, counts in per_cluster.items():
        total = sum(counts.values())
        if total == 0:
            continue
        out[cid] = {team: round(n / total, 3) for team, n in counts.items()}
    return out


def apply(conn: sqlite3.Connection) -> dict:
    """Compute + persist owner_distribution_json for every cluster.

    Importable entrypoint for pipeline hooks (manual-rollup apply +
    finalize_refresh apply). Idempotent. Returns a small summary dict.
    """
    _ensure_column(conn)
    dist = _cluster_ownership(conn)
    for cid, d in dist.items():
        conn.execute(
            "UPDATE topic_brief SET owner_distribution_json = ? WHERE cluster_id = ?",
            (json.dumps(d, sort_keys=True), cid),
        )
    conn.commit()
    return {"clusters_scored": len(dist)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--home-team",
        default=home_team(),
        help="Team for which to print per-cluster ownership-pct summary stats",
    )
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    _ensure_column(conn)

    dist = _cluster_ownership(conn)
    print(f"computed ownership distribution for {len(dist)} clusters")

    n_total = conn.execute("SELECT COUNT(*) FROM topic_brief").fetchone()[0]
    print(f"topic_brief total rows: {n_total}")
    print(f"clusters with no members: {n_total - len(dist)}")

    if not args.dry_run:
        for cid, d in dist.items():
            conn.execute(
                "UPDATE topic_brief SET owner_distribution_json = ? WHERE cluster_id = ?",
                (json.dumps(d, sort_keys=True), cid),
            )
        conn.commit()
        print(f"wrote owner_distribution_json to {len(dist)} clusters")
    else:
        print("DRY-RUN — no writes")

    # Home-team stats
    home = args.home_team
    pcts = sorted(
        (cid, d.get(home, 0.0)) for cid, d in dist.items()
    )
    buckets = {"100%": 0, ">=75%": 0, ">=50%": 0, ">=25%": 0, "<25%": 0, "0%": 0}
    for _, pct in pcts:
        if pct == 1.0:
            buckets["100%"] += 1
        elif pct >= 0.75:
            buckets[">=75%"] += 1
        elif pct >= 0.5:
            buckets[">=50%"] += 1
        elif pct >= 0.25:
            buckets[">=25%"] += 1
        elif pct > 0:
            buckets["<25%"] += 1
        else:
            buckets["0%"] += 1
    print(f"\nhome-team ({home}) ownership distribution across clusters:")
    for k, v in buckets.items():
        print(f"  {k:>6}: {v} clusters")

    # Sample: 5 clusters near each threshold for spot-check
    pcts_sorted = sorted(pcts, key=lambda x: x[1])
    print(f"\nclusters with 0% {home}:")
    for cid, _ in pcts_sorted[:5]:
        label = conn.execute(
            "SELECT label FROM topic_brief WHERE cluster_id=?", (cid,)
        ).fetchone()
        d = dist[cid]
        top_owners = sorted(d.items(), key=lambda x: -x[1])[:3]
        print(f"  c{cid} {label[0] if label else '(no label)'}: {top_owners}")

    print(f"\nclusters with 100% {home}:")
    for cid, _ in [x for x in pcts_sorted if x[1] == 1.0][:5]:
        label = conn.execute(
            "SELECT label FROM topic_brief WHERE cluster_id=?", (cid,)
        ).fetchone()
        print(f"  c{cid} {label[0] if label else '(no label)'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""One-off: build a finalize dump for the size-15 clusters straight from
cluster_diff_plan.json's `fresh_clusters` (NOT topic_brief_member, which still
holds the stale size-5 layout). Writes state/pending_cluster_finalize.json in
the exact shape finalize_refresh.cmd_apply expects.

Scope = new + relabel actions from the plan. Members are SAMPLED per cluster for
content (large size-15 clusters reach 600+ members), but participants_observed
is counted over the FULL member set so roles/attribution stay correct.
"""
from __future__ import annotations

import json
from collections import defaultdict

from ingest.common import get_db
from derive.subject_content import get_content
from derive.actor_behavior import _build_actor_canonical_map
from derive.finalize_refresh import (
    PENDING_PATH, RULES_PATH, RULES_SOURCE, CLUSTER_DIFF_PLAN, _now_iso,
)

SAMPLE = 30          # content blocks per cluster
MEMBER_CHARS = 400   # chars of content per member block


def build():
    plan = json.loads(CLUSTER_DIFF_PLAN.read_text())
    fresh = {int(k): v for k, v in plan["fresh_clusters"].items()}
    scope = sorted(
        int(cid) for cid, e in plan["plan"].items()
        if e.get("action") in ("new", "relabel")
    )
    conn = get_db()
    actor_map = _build_actor_canonical_map()

    payloads = []
    for cid in scope:
        members = fresh.get(cid, [])
        if not members:
            continue
        members = sorted(members)
        # participants over ALL members
        actor_counts: dict[str, int] = defaultdict(int)
        for subj in members:
            for a, n in conn.execute(
                "SELECT actor, COUNT(*) FROM events WHERE subject = ? AND actor IS NOT NULL "
                "GROUP BY actor", (subj,)
            ).fetchall():
                actor_counts[a] += n
        participants: dict[str, int] = defaultdict(int)
        for actor_id, count in actor_counts.items():
            if "[bot]" in (actor_id or "").lower():
                continue
            canon = actor_map.get(actor_id) or f"<raw:{actor_id}>"
            participants[canon] += count

        # content blocks for a sample
        sample = members[:SAMPLE]
        blocks = []
        for subj in sample:
            _, content = get_content(conn, subj)
            cont = " ".join((content or "").split())
            if len(cont) > MEMBER_CHARS:
                cont = cont[:MEMBER_CHARS].rstrip() + "…"
            trow = conn.execute(
                "SELECT title FROM events WHERE subject = ? AND title IS NOT NULL AND title != '' "
                "ORDER BY ts LIMIT 1", (subj,)
            ).fetchone()
            title = (trow[0] or "")[:160] if trow else ""
            blocks.append({"subject": subj, "title": title, "content": cont})

        ph = ",".join("?" * len(members))
        ts_row = conn.execute(
            f"SELECT MIN(ts), MAX(ts) FROM events WHERE subject IN ({ph})", members
        ).fetchone()
        first_ts, last_ts = ts_row if ts_row else (None, None)
        src_rows = conn.execute(
            f"SELECT source, COUNT(*) FROM embedding WHERE subject IN ({ph}) GROUP BY source",
            members,
        ).fetchall()
        src_breakdown = {s: c for s, c in src_rows}

        payloads.append({
            "cluster_id": cid,
            "existing_label": None,
            "existing_summary": None,
            "existing_status": None,
            "source_breakdown": src_breakdown,
            "member_count": len(members),
            "members_sampled": len(members) > len(sample),
            "n_sampled": len(sample),
            "first_ts": first_ts,
            "last_activity_ts": last_ts,
            "participants_observed": dict(sorted(participants.items(), key=lambda kv: -kv[1])),
            "members": blocks,
        })

    out = {"computed_at": _now_iso(), "n_clusters": len(payloads), "clusters": payloads}
    PENDING_PATH.write_text(json.dumps(out, indent=2))
    if RULES_SOURCE.exists():
        RULES_PATH.write_text(RULES_SOURCE.read_text())

    total_members = sum(c["member_count"] for c in payloads)
    sampled = sum(1 for c in payloads if c["members_sampled"])
    print(json.dumps({
        "pending_file": str(PENDING_PATH),
        "n_clusters": len(payloads),
        "scope_requested": len(scope),
        "total_members_true": total_members,
        "clusters_sampled": sampled,
        "bytes": PENDING_PATH.stat().st_size,
    }, indent=2))


if __name__ == "__main__":
    build()

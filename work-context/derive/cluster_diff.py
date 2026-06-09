"""
cluster_diff.py — match new HDBSCAN clusters to old labelled clusters by
member-set similarity, so Phase B labels can survive incremental re-clusters.

Problem
-------
HDBSCAN is not incremental. Adding new subjects reshuffles all `cluster_id`s.
Existing `topic_brief` labels are pinned to old `cluster_id`s. Without a
mapping, every refresh forces a full chat-relabel.

Solution
--------
After a fresh re-cluster, build:

    {new_cluster_id: {
        "best_match_old_cid": int | None,
        "jaccard": float,
        "n_new_members": int,
        "n_old_members": int,
        "n_overlap": int,
        "action": "preserve" | "relabel" | "new",
    }, ...}

Action is `preserve` when Jaccard >= threshold (default 0.8) AND the new
member-set is not dramatically larger. `new` when no old cluster shares ≥3
members. `relabel` otherwise.

Preserving a label = copy old `label`, `summary`, `status`, `decisions_json`,
`blockers_json`, `participants_json`, `root_cause`, `confidence` from the
old `topic_brief` row to the new `cluster_id`. The new row's `member_count`,
`first_ts`, `last_activity_ts`, `source_breakdown_json`, `computed_at` are
recomputed.

CLI
---
    .venv/bin/python derive/cluster_diff.py plan
        Re-cluster now, compute the diff, write plan to
        state/cluster_diff_plan.json. NO writes to topic_brief. Prints
        summary counts.

    .venv/bin/python derive/cluster_diff.py apply
        Reads state/cluster_diff_plan.json. For 'preserve' clusters, copies
        old enrichment to the new cluster_id. Updates topic_brief_member.
        Flags 'new' + 'relabel' clusters into a fresh dump file so chat
        can label only those.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db  # noqa: E402

ROOT = _PKG_ROOT
PLAN_PATH = ROOT / "state" / "cluster_diff_plan.json"
PENDING_NEW_LABELS_PATH = ROOT / "state" / "pending_new_cluster_labels.json"
RULES_SOURCE_PATH = ROOT / "derive" / "cluster_label_rules.md"
PENDING_RULES_PATH = ROOT / "state" / "pending_new_cluster_labels.json.rules.md"


def _unpack(b: bytes):
    n = len(b) // 4
    return list(struct.unpack(f"<{n}f", b))


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── core ────────────────────────────────────────────────────────────────────


def _fresh_clusters(conn, min_cluster_size: int) -> dict[int, list[str]]:
    """Run HDBSCAN on current `embedding` table and return {cluster_id: [subjects]}."""
    import numpy as np
    from sklearn.cluster import HDBSCAN
    # ORDER BY subject pins SQLite row order across runs. HDBSCAN itself is
    # deterministic on identical input, but it renumbers cluster_ids when the
    # input row order shifts. The Jaccard-based preserve logic in cmd_apply
    # tolerates renumbering, but stable ordering also means stable cluster_ids
    # whenever membership is unchanged — fewer spurious "relabel" actions.
    rows = conn.execute("SELECT subject, vector FROM embedding ORDER BY subject").fetchall()
    if not rows:
        return {}
    # Drop automation-noise subjects (alert/recon/digest channels) so real work
    # clusters cleanly at a small min-cluster-size. Decision is snapshotted in
    # cluster_excluded_channel by `cluster_noise_filter.py refresh`; this is the
    # single clustering chokepoint, so cluster_diff + refresh-embeddings both honor it.
    from derive.cluster_noise_filter import excluded_subjects as _excluded_subjects
    excluded = _excluded_subjects(conn)
    if excluded:
        rows = [r for r in rows if r[0] not in excluded]
        if not rows:
            return {}
    subs = [r[0] for r in rows]
    vecs = np.array([_unpack(r[1]) for r in rows], dtype=np.float32)
    # L2-normalize, then cluster with euclidean (NOT cosine). Rationale:
    #   - metric="cosine" forces HDBSCAN to materialise the full N×N distance
    #     matrix (~12 GB at 38k subjects) → OOM / swap-thrash on a 16 GB host.
    #   - On unit-length vectors euclidean distance = sqrt(2 - 2·cos_sim), a
    #     strictly monotonic transform of cosine → HDBSCAN (which depends only
    #     on distance *ordering*) yields the same clustering. Measured Adjusted
    #     Rand Index vs cosine = 0.997 on an 8k sample.
    #   - euclidean lets HDBSCAN use a space-tree → peak RSS ~0.5 GB (no N×N
    #     matrix). Trade-off: slower wall-clock (~15 min at 38k) but memory-safe.
    # OpenAI text-embedding-3 vectors are already ~unit-norm; the explicit
    # normalize makes the cosine-equivalence exact regardless of source.
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    vecs = vecs / np.clip(norms, 1e-12, None)
    labels = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=1,
        metric="euclidean",
    ).fit_predict(vecs)
    groups: dict[int, list[str]] = defaultdict(list)
    for s, lbl in zip(subs, labels):
        if int(lbl) != -1:
            groups[int(lbl)].append(s)
    return dict(groups)


def _old_clusters(conn) -> dict[int, set[str]]:
    rows = conn.execute("SELECT cluster_id, subject FROM topic_brief_member").fetchall()
    out: dict[int, set[str]] = defaultdict(set)
    for cid, subj in rows:
        out[cid].add(subj)
    return dict(out)


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def diff(
    conn,
    min_cluster_size: int,
    jaccard_threshold: float,
    min_overlap_for_match: int = 3,
) -> dict:
    """Compute the new→old mapping. Returns a plan dict."""
    fresh = _fresh_clusters(conn, min_cluster_size)
    old = _old_clusters(conn)

    plan: dict[str, dict] = {}
    matched_old: set[int] = set()
    for new_cid, new_members in fresh.items():
        new_set = set(new_members)
        best_cid: int | None = None
        best_j: float = 0.0
        best_overlap: int = 0
        for old_cid, old_set in old.items():
            if old_cid in matched_old:
                continue  # one-to-one match enforced
            j = _jaccard(new_set, old_set)
            if j > best_j:
                best_j = j
                best_cid = old_cid
                best_overlap = len(new_set & old_set)
        if best_cid is not None and best_j >= jaccard_threshold and best_overlap >= min_overlap_for_match:
            matched_old.add(best_cid)
            action = "preserve"
        elif best_cid is not None and best_overlap >= min_overlap_for_match:
            action = "relabel"  # partial match — old label may be stale
        else:
            action = "new"
        plan[str(new_cid)] = {
            "best_match_old_cid": best_cid,
            "jaccard": round(best_j, 3),
            "n_new_members": len(new_set),
            "n_old_members": len(old.get(best_cid or -999, set())),
            "n_overlap": best_overlap,
            "action": action,
        }

    # Old clusters with no match → dropped (subjects redistributed elsewhere)
    dropped_old = [cid for cid in old if cid not in matched_old]

    return {
        "computed_at": _now_iso(),
        "min_cluster_size": min_cluster_size,
        "jaccard_threshold": jaccard_threshold,
        "n_old_clusters": len(old),
        "n_new_clusters": len(fresh),
        "summary": {
            "preserve": sum(1 for v in plan.values() if v["action"] == "preserve"),
            "relabel": sum(1 for v in plan.values() if v["action"] == "relabel"),
            "new": sum(1 for v in plan.values() if v["action"] == "new"),
            "dropped_old": len(dropped_old),
        },
        "plan": plan,
        "fresh_clusters": {str(k): v for k, v in fresh.items()},  # save for apply
        "dropped_old_cluster_ids": sorted(dropped_old),
    }


# ── CLI: plan ───────────────────────────────────────────────────────────────


def cmd_plan(args):
    conn = get_db()
    p = diff(conn, args.min_cluster_size, args.jaccard_threshold, args.min_overlap)
    PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(json.dumps(p, indent=2))
    print(f"✓ cluster diff plan → {PLAN_PATH}")
    print(f"  old clusters: {p['n_old_clusters']}")
    print(f"  new clusters: {p['n_new_clusters']}")
    print(f"  summary:")
    for k, v in p["summary"].items():
        print(f"    {k:12s} {v}")
    print()
    print(f"Next: review plan, then run `apply`.")
    print(f"  preserve   → label/enrichment copied from old to new cluster_id (no chat needed)")
    print(f"  relabel    → partial match, OLD label may be stale; chat labels these")
    print(f"  new        → fresh cluster, no old match; chat labels these")
    print(f"  dropped    → old cluster_ids that don't carry forward (subjects redistributed)")


# ── CLI: apply ──────────────────────────────────────────────────────────────


def cmd_apply(args):
    if not PLAN_PATH.exists():
        print(f"missing {PLAN_PATH} — run `plan` first")
        return
    plan = json.loads(PLAN_PATH.read_text())
    conn = get_db()

    # Refuse to re-apply on an already-wiped state. The plan's
    # `best_match_old_cid` references topic_brief rows that no longer exist;
    # running apply again would relabel every cluster as `new`/`relabel` and
    # silently destroy the preserve path.
    n_existing_briefs = conn.execute("SELECT COUNT(*) FROM topic_brief").fetchone()[0]
    if n_existing_briefs == 0 and plan["n_old_clusters"] > 0:
        print(
            f"refusing: topic_brief is empty but plan expected "
            f"{plan['n_old_clusters']} old clusters. The plan is stale — "
            f"likely a re-apply on already-applied state. Re-run `plan` to refresh."
        )
        return

    # Load old topic_brief snapshot (keyed by old cluster_id) so we can copy
    # enrichment forward to new cluster_ids.
    old_briefs: dict[int, dict] = {}
    for row in conn.execute(
        """SELECT cluster_id, label, summary, status, root_cause,
                  decisions_json, blockers_json, participants_json,
                  confidence
             FROM topic_brief"""
    ):
        old_briefs[row[0]] = {
            "label": row[1], "summary": row[2], "status": row[3],
            "root_cause": row[4], "decisions_json": row[5], "blockers_json": row[6],
            "participants_json": row[7], "confidence": row[8],
        }

    fresh_clusters = {int(k): v for k, v in plan["fresh_clusters"].items()}
    now = _now_iso()

    # Wrap the entire DELETE+INSERT replace in a single explicit transaction
    # so a mid-apply crash leaves topic_brief / topic_brief_member untouched
    # (instead of wiped-but-partially-repopulated). We drive transaction
    # control manually via `isolation_level=None` to ensure BEGIN IMMEDIATE
    # acquires the write lock upfront — avoids SQLITE_BUSY mid-loop under
    # WAL when another writer holds the reserved lock.
    pre_brief = conn.execute("SELECT COUNT(*) FROM topic_brief").fetchone()[0]
    pre_member = conn.execute("SELECT COUNT(*) FROM topic_brief_member").fetchone()[0]

    preserved = 0
    needs_label: list[int] = []
    needs_relabel: list[int] = []

    prev_isolation = conn.isolation_level
    conn.isolation_level = None  # manual transaction control
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM topic_brief_member")
        conn.execute("DELETE FROM topic_brief")

        for new_cid_str, entry in plan["plan"].items():
            new_cid = int(new_cid_str)
            members = fresh_clusters.get(new_cid, [])
            # Compute first_ts, last_activity_ts, source_breakdown per cluster.
            if not members:
                continue
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

            if entry["action"] == "preserve":
                old = old_briefs.get(entry["best_match_old_cid"])
                if not old:
                    # Defensive — shouldn't happen if plan was just computed.
                    needs_label.append(new_cid)
                    continue
                conn.execute(
                    """INSERT INTO topic_brief
                       (cluster_id, label, summary, status, root_cause,
                        decisions_json, blockers_json, participants_json,
                        source_breakdown_json, member_count,
                        first_ts, last_activity_ts, computed_at, confidence)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        new_cid, old["label"], old["summary"], old["status"],
                        old["root_cause"], old["decisions_json"], old["blockers_json"],
                        old["participants_json"],
                        json.dumps(src_breakdown, sort_keys=True),
                        len(members), first_ts, last_ts, now, old["confidence"],
                    ),
                )
                preserved += 1
            else:
                # 'new' or 'relabel' — insert placeholder so member rows + ids
                # exist, but leave content fields NULL for chat to fill.
                conn.execute(
                    """INSERT INTO topic_brief
                       (cluster_id, label, member_count, source_breakdown_json,
                        first_ts, last_activity_ts, computed_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        new_cid, None, len(members),
                        json.dumps(src_breakdown, sort_keys=True),
                        first_ts, last_ts, now,
                    ),
                )
                if entry["action"] == "new":
                    needs_label.append(new_cid)
                else:
                    needs_relabel.append(new_cid)

            # topic_brief_member rows for the new cluster_id.
            # `source` is NOT NULL per schema — pull from embedding row. If a
            # member is missing from the embedding table the schema invariant
            # is broken and we must fail loud rather than silently insert
            # "unknown" placeholder rows (which would corrupt downstream
            # source-breakdown analytics).
            src_by_sub = dict(conn.execute(
                f"SELECT subject, source FROM embedding WHERE subject IN ({ph})",
                members,
            ).fetchall())
            missing_src = [s for s in members if s not in src_by_sub]
            if missing_src:
                raise RuntimeError(
                    f"topic_brief_member integrity: {len(missing_src)} cluster "
                    f"members missing from `embedding` table "
                    f"(e.g. {missing_src[:3]}). Re-run embed_subjects then "
                    f"cluster_diff plan + apply."
                )
            for sub in members:
                conn.execute(
                    "INSERT INTO topic_brief_member (cluster_id, subject, source) VALUES (?,?,?)",
                    (new_cid, sub, src_by_sub[sub]),
                )

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.isolation_level = prev_isolation
    print(json.dumps({
        "preserved": preserved,
        "needs_new_label": len(needs_label),
        "needs_relabel": len(needs_relabel),
        "pre_topic_brief": pre_brief,
        "pre_topic_brief_member": pre_member,
        "post_topic_brief": conn.execute("SELECT COUNT(*) FROM topic_brief").fetchone()[0],
        "post_topic_brief_member": conn.execute("SELECT COUNT(*) FROM topic_brief_member").fetchone()[0],
    }, indent=2))

    # Build dump file for chat-labeling only the new + relabel clusters.
    if needs_label or needs_relabel:
        _dump_clusters_for_labeling(conn, sorted(needs_label + needs_relabel))

    # Archive the applied plan so accidental re-runs of `apply` are caught by
    # the missing-plan guard at the top of cmd_apply. A fresh `plan` run will
    # overwrite the live PLAN_PATH.
    applied_path = PLAN_PATH.with_suffix(PLAN_PATH.suffix + ".applied")
    PLAN_PATH.rename(applied_path)
    print(f"\n✓ plan archived → {applied_path.name} (re-run `plan` before next `apply`)")


def _dump_clusters_for_labeling(conn, cluster_ids: list[int]) -> None:
    """Re-uses subject_content extractor + label_clusters dump shape, but
    scoped to a specific cluster_id list. Writes to PENDING_NEW_LABELS_PATH."""
    from derive.subject_content import get_content
    if not cluster_ids:
        return
    ph = ",".join("?" * len(cluster_ids))
    rows = conn.execute(
        f"""SELECT m.cluster_id, m.subject, m.source
              FROM topic_brief_member m
             WHERE m.cluster_id IN ({ph})""",
        cluster_ids,
    ).fetchall()
    by_cid: dict[int, list[dict]] = defaultdict(list)
    for cid, subj, src in rows:
        _, content = get_content(conn, subj)
        cont = " ".join((content or "").split())
        if len(cont) > 400:
            cont = cont[:400].rstrip() + "…"
        by_cid[cid].append({"subject": subj, "source": src, "content": cont})
    payload_clusters = []
    for cid in cluster_ids:
        members = by_cid.get(cid, [])
        src_count: dict[str, int] = defaultdict(int)
        for m in members:
            src_count[m["source"]] += 1
        payload_clusters.append({
            "cluster_id": cid,
            "n_members": len(members),
            "sources_breakdown": "  ".join(f"{k}={v}" for k, v in sorted(src_count.items())),
            "members": members,
        })
    PENDING_NEW_LABELS_PATH.write_text(json.dumps({
        "computed_at": _now_iso(),
        "n_clusters": len(payload_clusters),
        "clusters": payload_clusters,
    }, indent=2))
    if RULES_SOURCE_PATH.exists():
        PENDING_RULES_PATH.write_text(RULES_SOURCE_PATH.read_text())
    print(f"\n✓ {len(payload_clusters)} clusters need chat-labeling → {PENDING_NEW_LABELS_PATH}")
    print(f"  rules:  {PENDING_RULES_PATH}")
    print(f"  Next: in chat, produce verdicts at state/verdicts.cluster_labels.json,")
    print(f"        then run `.venv/bin/python derive/label_clusters.py apply`.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="Compute diff plan, write to state/cluster_diff_plan.json")
    p.add_argument("--min-cluster-size", type=int, default=5)
    p.add_argument("--jaccard-threshold", type=float, default=0.8)
    p.add_argument("--min-overlap", type=int, default=3,
                   help="Min new∩old member count to consider a match at all")
    p.set_defaults(fn=cmd_plan)

    a = sub.add_parser("apply", help="Apply diff: preserve labels by Jaccard match, dump rest for chat")
    a.set_defaults(fn=cmd_apply)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

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
APPLIED_PLAN_PATH = PLAN_PATH.with_suffix(PLAN_PATH.suffix + ".applied")


def last_applied_min_cluster_size() -> int | None:
    """min_cluster_size of the last APPLIED plan, or None if never applied.

    Guard input for granularity pinning: running plan/refresh with a different
    min_cluster_size than the live topic_brief was built with re-shards every
    cluster and triggers a mass spurious relabel (observed 2026-06: 5→15
    flipped 574 of 606 clusters into the chat-labeling queue)."""
    if not APPLIED_PLAN_PATH.exists():
        return None
    try:
        return json.loads(APPLIED_PLAN_PATH.read_text()).get("min_cluster_size")
    except (json.JSONDecodeError, OSError):
        return None


def _unpack(b: bytes):
    n = len(b) // 4
    return list(struct.unpack(f"<{n}f", b))


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── core ────────────────────────────────────────────────────────────────────


def _subject_last_ts(conn) -> dict[str, str]:
    """Last event ts per subject — one GROUP BY pass over events."""
    return dict(conn.execute("SELECT subject, MAX(ts) FROM events GROUP BY subject"))


def _fresh_clusters(conn, min_cluster_size: int, window_days: int | None = None) -> dict[int, list[str]]:
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
    # Optional trailing-window cap: clustering cost grows with corpus size
    # (~2 min at 8.5k filtered subjects, ~18 min at 38k) and old inactive
    # subjects re-shuffle every run for no benefit. With window_days set,
    # only subjects with activity inside the window are clustered; old
    # clusters made entirely of out-of-window subjects are FROZEN by diff()
    # and carried verbatim through apply (label + members untouched).
    if window_days:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        last_ts = _subject_last_ts(conn)
        rows = [r for r in rows if (last_ts.get(r[0]) or "") >= cutoff]
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
    containment_threshold: float = 0.8,
    window_days: int | None = None,
) -> dict:
    """Compute the new→old mapping. Returns a plan dict."""
    fresh = _fresh_clusters(conn, min_cluster_size, window_days=window_days)
    old = _old_clusters(conn)

    # Windowed mode: old clusters whose members are ALL outside the window
    # never re-cluster — freeze them (carried verbatim by apply) and exclude
    # them from matching so they can't be spuriously dropped or matched.
    frozen_old: list[int] = []
    if window_days:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        last_ts = _subject_last_ts(conn)
        frozen_old = [
            cid for cid, members in old.items()
            if members and all((last_ts.get(s) or "") < cutoff for s in members)
        ]
        old = {cid: m for cid, m in old.items() if cid not in set(frozen_old)}

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

        # Containment fallback. Jaccard structurally fails when small old
        # clusters MERGE into a bigger new one (a fully-absorbed 10-member
        # cluster inside a 50-member cluster scores j=0.2 → "new", and its
        # label dies). Measured 2026-06: a re-granularisation kept only
        # 32/606 labels and forced a 208-cluster relabel session. So when
        # Jaccard found nothing usable, look for an old cluster mostly
        # CONTAINED in the new one (overlap/|old| ≥ threshold) and donate its
        # label as a draft — action becomes "relabel" (chat confirms) instead
        # of "new" (chat writes from scratch).
        seed_cid: int | None = None
        seed_containment: float = 0.0
        if action == "new":
            best_seed_overlap = 0
            for old_cid, old_set in old.items():
                if old_cid in matched_old or not old_set:
                    continue
                overlap = len(new_set & old_set)
                containment = overlap / len(old_set)
                if (containment >= containment_threshold
                        and overlap >= min_overlap_for_match
                        and overlap > best_seed_overlap):
                    seed_cid = old_cid
                    seed_containment = containment
                    best_seed_overlap = overlap
            if seed_cid is not None:
                matched_old.add(seed_cid)
                action = "relabel"
                best_cid = seed_cid
                best_overlap = best_seed_overlap

        plan[str(new_cid)] = {
            "best_match_old_cid": best_cid,
            "jaccard": round(best_j, 3),
            "containment": round(seed_containment, 3) if seed_cid is not None else None,
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
        "window_days": window_days,
        "n_old_clusters": len(old),
        "n_new_clusters": len(fresh),
        "summary": {
            "preserve": sum(1 for v in plan.values() if v["action"] == "preserve"),
            "relabel": sum(1 for v in plan.values() if v["action"] == "relabel"),
            "new": sum(1 for v in plan.values() if v["action"] == "new"),
            "dropped_old": len(dropped_old),
            "frozen_old": len(frozen_old),
        },
        "plan": plan,
        "fresh_clusters": {str(k): v for k, v in fresh.items()},  # save for apply
        "dropped_old_cluster_ids": sorted(dropped_old),
        "frozen_old_cluster_ids": sorted(frozen_old),
    }


# ── CLI: plan ───────────────────────────────────────────────────────────────


def cmd_plan(args):
    conn = get_db()
    pinned = last_applied_min_cluster_size()
    if pinned is not None and pinned != args.min_cluster_size:
        print(
            f"⚠ GRANULARITY CHANGE: live topic_brief was applied with "
            f"min_cluster_size={pinned}, this plan uses {args.min_cluster_size}. "
            f"Expect a mass relabel — use {pinned} unless re-sharding is intended."
        )
    p = diff(conn, args.min_cluster_size, args.jaccard_threshold, args.min_overlap,
             window_days=getattr(args, "cluster_window_days", None))
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
    # enrichment forward to new cluster_ids. Full-row dynamic copy: the
    # previous fixed 8-column list silently DROPPED every column added since
    # (outcomes/followups/risk_areas/stakeholders/artifacts json, v2 fields) —
    # observed as "ACTIVE clusters with no v2 enrichment" WARNs after the
    # 2026-06 refresh. Carry everything except the per-apply computed fields.
    _computed_cols = {
        "cluster_id", "source_breakdown_json", "member_count",
        "first_ts", "last_activity_ts", "computed_at",
    }
    tb_cols = [r[1] for r in conn.execute("PRAGMA table_info(topic_brief)")]
    carry_cols = [c for c in tb_cols if c not in _computed_cols]
    old_briefs: dict[int, dict] = {}
    for row in conn.execute(
        f"SELECT cluster_id, {', '.join(carry_cols)} FROM topic_brief"
    ):
        old_briefs[row[0]] = dict(zip(carry_cols, row[1:]))

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

    # Windowed plans freeze out-of-window old clusters — snapshot their member
    # rows BEFORE the wipe so they can be re-inserted verbatim (renumbered
    # above the fresh id range to avoid collisions with HDBSCAN numbering).
    frozen_ids = [int(c) for c in plan.get("frozen_old_cluster_ids", [])]
    frozen_members: dict[int, list[tuple[str, str]]] = {}
    if frozen_ids:
        ph_f = ",".join("?" * len(frozen_ids))
        for cid, subj, src in conn.execute(
            f"SELECT cluster_id, subject, source FROM topic_brief_member "
            f"WHERE cluster_id IN ({ph_f})", frozen_ids,
        ):
            frozen_members.setdefault(cid, []).append((subj, src))

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
                cols = ["cluster_id", *carry_cols, "source_breakdown_json",
                        "member_count", "first_ts", "last_activity_ts", "computed_at"]
                vals = [new_cid, *(old[c] for c in carry_cols),
                        json.dumps(src_breakdown, sort_keys=True),
                        len(members), first_ts, last_ts, now]
                conn.execute(
                    f"INSERT INTO topic_brief ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' * len(cols))})",
                    vals,
                )
                preserved += 1
            else:
                # 'new' or 'relabel' — insert placeholder so member rows + ids
                # exist. For 'relabel' with a known donor (partial Jaccard
                # match OR containment seed), carry the donor's label/summary/
                # status forward as a DRAFT: /ask + /retro stay usable during
                # the labeling window, and chat confirms/overwrites via the
                # finalize pass (the dump surfaces existing_label). 'new'
                # stays NULL for chat to fill from scratch.
                donor = old_briefs.get(entry.get("best_match_old_cid") or -999) \
                    if entry["action"] == "relabel" else None
                conn.execute(
                    """INSERT INTO topic_brief
                       (cluster_id, label, summary, status, confidence,
                        member_count, source_breakdown_json,
                        first_ts, last_activity_ts, computed_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        new_cid,
                        donor["label"] if donor else None,
                        donor["summary"] if donor else None,
                        donor["status"] if donor else None,
                        donor["confidence"] if donor else None,
                        len(members),
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

        # Re-insert frozen clusters verbatim, renumbered above the fresh
        # range. Their enrichment row carries fully (carry_cols) and their
        # original first/last ts + source breakdown are recomputed from the
        # snapshotted members so nothing depends on the deleted rows.
        frozen_carried = 0
        next_cid = (max(fresh_clusters) + 1) if fresh_clusters else 0
        for old_cid in frozen_ids:
            old = old_briefs.get(old_cid)
            mem = frozen_members.get(old_cid, [])
            if not old or not mem:
                continue
            subs = [m[0] for m in mem]
            ph = ",".join("?" * len(subs))
            ts_row = conn.execute(
                f"SELECT MIN(ts), MAX(ts) FROM events WHERE subject IN ({ph})", subs
            ).fetchone()
            src_breakdown: dict[str, int] = {}
            for _, src in mem:
                src_breakdown[src] = src_breakdown.get(src, 0) + 1
            cols = ["cluster_id", *carry_cols, "source_breakdown_json",
                    "member_count", "first_ts", "last_activity_ts", "computed_at"]
            vals = [next_cid, *(old[c] for c in carry_cols),
                    json.dumps(src_breakdown, sort_keys=True),
                    len(subs), ts_row[0], ts_row[1], now]
            conn.execute(
                f"INSERT INTO topic_brief ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                vals,
            )
            for subj, src in mem:
                conn.execute(
                    "INSERT INTO topic_brief_member (cluster_id, subject, source) VALUES (?,?,?)",
                    (next_cid, subj, src),
                )
            frozen_carried += 1
            next_cid += 1

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
        "frozen_carried": frozen_carried,
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
    p.add_argument("--cluster-window-days", type=int, default=None,
                   help="Only re-cluster subjects active in the last N days; "
                        "old clusters wholly outside the window are frozen "
                        "(carried verbatim). Default: cluster everything.")
    p.set_defaults(fn=cmd_plan)

    a = sub.add_parser("apply", help="Apply diff: preserve labels by Jaccard match, dump rest for chat")
    a.set_defaults(fn=cmd_apply)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

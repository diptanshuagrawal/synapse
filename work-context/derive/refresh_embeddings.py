"""
refresh_embeddings.py — incremental refresh orchestrator.

Ties the four-step pipeline into one command:

    1. detect    — find embeddable subjects not yet in `embedding` (or whose
                   content_sha drifted since last embed).
    2. embed     — call embed_subjects.embed_subjects() on the delta only.
    3. diff plan — run cluster_diff.diff() to map fresh HDBSCAN clusters back
                   to existing topic_brief cluster_ids by Jaccard overlap.
    4. (optional) apply — call cluster_diff cmd_apply to persist new cluster_ids,
                   preserve old labels where Jaccard ≥ threshold, and dump the
                   `new` + `relabel` clusters for chat labeling.

Each step is idempotent. Stops short of running the LLM-pass — chat does that.

CLI
---
    .venv/bin/python derive/refresh_embeddings.py status
        Show new-subject count vs already-embedded count. No writes.

    .venv/bin/python derive/refresh_embeddings.py refresh
        detect + embed delta + write cluster diff plan. NO topic_brief writes.
        Prints summary + the next command to run.

    .venv/bin/python derive/refresh_embeddings.py refresh --apply
        Same as above, then runs cluster_diff apply (mutates topic_brief).
        Use ONLY when summary shows preserve >> relabel+new and you're OK with
        unlabelled placeholder rows for the delta.

    .venv/bin/python derive/refresh_embeddings.py refresh --skip-embed
        Skip step 2 (use when embed was done separately). Goes straight to diff.

Output: JSON stats block to stdout for every step.

Hard constraints
----------------
- Refuses to mutate topic_brief unless `--apply` is passed.
- Refuses to embed when no OpenAI key is present (delta could be silently
  skipped otherwise).
- Does NOT call any LLM-classifier. Chat labels via `label_clusters apply`
  consuming state/pending_new_cluster_labels.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db  # noqa: E402
from derive.sample_subjects import sample  # noqa: E402
from derive.subject_content import get_content, content_sha, CONTENT_RECIPE_VERSION  # noqa: E402
from derive import embed_subjects as _embed_mod  # noqa: E402
from derive import cluster_diff as _diff_mod  # noqa: E402
from derive import openai_client  # noqa: E402


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── detect ──────────────────────────────────────────────────────────────────


_CACHE_DDL = """CREATE TABLE IF NOT EXISTS embed_content_cache (
    subject        TEXT PRIMARY KEY,
    recipe_version INTEGER NOT NULL,
    n_events       INTEGER NOT NULL,
    last_event_ts  TEXT,
    content_sha    TEXT NOT NULL,
    computed_at    TEXT NOT NULL
)"""


def detect_delta(conn, model: str) -> dict:
    """Return {new: [...], drifted: [...], unchanged: int, no_content: int}.

    new       — embeddable subject not in `embedding` table (any model).
    drifted   — in `embedding` for this model BUT content_sha differs now.
    unchanged — sha matches; no re-embed needed.
    no_content — embeddable corpus member but get_content() returned empty.

    Content shas are memoised in `embed_content_cache`, keyed on a cheap
    change signature (event count + last event ts per subject) plus
    CONTENT_RECIPE_VERSION. A subject's content can only change when its
    events change or the recipe changes — so the expensive get_content()
    pass (per-subject event reads, ~25s over a 38k corpus) runs only for
    changed subjects (typically a few hundred per day). Rows store sha=""
    for no-content subjects so noise stays cheap too.
    """
    all_subjects = sample(conn, target_size=None)
    existing = dict(conn.execute(
        "SELECT subject, content_sha FROM embedding WHERE model = ?", (model,),
    ))

    conn.execute(_CACHE_DDL)
    sig = {s: (n, ts) for s, n, ts in conn.execute(
        "SELECT subject, COUNT(*), MAX(ts) FROM events GROUP BY subject"
    )}
    cache = {s: (rv, n, ts, sha) for s, rv, n, ts, sha in conn.execute(
        "SELECT subject, recipe_version, n_events, last_event_ts, content_sha "
        "FROM embed_content_cache"
    )}

    new: list[str] = []
    drifted: list[str] = []
    unchanged = 0
    no_content = 0
    cache_hits = 0
    upserts: list[tuple] = []
    now = _now_iso()

    for subj in all_subjects:
        n_ev, last_ts = sig.get(subj, (0, None))
        hit = cache.get(subj)
        if hit and hit[0] == CONTENT_RECIPE_VERSION and hit[1] == n_ev and hit[2] == last_ts:
            sha = hit[3]
            cache_hits += 1
        else:
            _, content = get_content(conn, subj)
            sha = content_sha(content) if content else ""
            upserts.append((subj, CONTENT_RECIPE_VERSION, n_ev, last_ts, sha, now))
        if sha == "":
            no_content += 1
            continue
        if subj not in existing:
            new.append(subj)
        elif existing[subj] != sha:
            drifted.append(subj)
        else:
            unchanged += 1

    if upserts:
        conn.executemany(
            "INSERT OR REPLACE INTO embed_content_cache VALUES (?,?,?,?,?,?)", upserts
        )
        conn.commit()

    return {
        "model": model,
        "n_corpus": len(all_subjects),
        "n_existing": len(existing),
        "n_unchanged": unchanged,
        "n_no_content": no_content,
        "n_new": len(new),
        "n_drifted": len(drifted),
        "sha_cache_hits": cache_hits,
        "sha_recomputed": len(upserts),
        "new": new,
        "drifted": drifted,
    }


# ── status ──────────────────────────────────────────────────────────────────


def cmd_status(args):
    conn = get_db()
    delta = detect_delta(conn, args.model)
    out = {k: v for k, v in delta.items() if k not in ("new", "drifted")}
    # Sample the heads only so stdout stays small.
    out["new_head"] = delta["new"][:5]
    out["drifted_head"] = delta["drifted"][:5]
    out["embed_required"] = len(delta["new"]) + len(delta["drifted"])
    print(json.dumps(out, indent=2))


# ── refresh ─────────────────────────────────────────────────────────────────


def cmd_refresh(args):
    conn = get_db()

    # Granularity pin: clustering with a different min_cluster_size than the
    # live topic_brief was applied with re-shards every cluster and triggers
    # a mass spurious relabel (observed 2026-06: 5→15 queued 574 of 606
    # clusters for chat labeling). Refuse unless explicitly overridden.
    pinned = _diff_mod.last_applied_min_cluster_size()
    if (pinned is not None and pinned != args.min_cluster_size
            and not args.allow_granularity_change):
        print(
            f"refusing: live topic_brief was applied with min_cluster_size={pinned}, "
            f"but this run requests {args.min_cluster_size}. A granularity change "
            f"re-shards every cluster (mass relabel). Re-run with "
            f"--min-cluster-size {pinned}, or pass --allow-granularity-change "
            f"if re-sharding is intended.",
            file=sys.stderr,
        )
        sys.exit(3)

    summary: dict[str, object] = {"started_at": _now_iso(), "model": args.model}

    # Step 1 — detect.
    delta = detect_delta(conn, args.model)
    summary["detect"] = {
        "n_corpus": delta["n_corpus"],
        "n_existing": delta["n_existing"],
        "n_unchanged": delta["n_unchanged"],
        "n_no_content": delta["n_no_content"],
        "n_new": delta["n_new"],
        "n_drifted": delta["n_drifted"],
        "sha_cache_hits": delta["sha_cache_hits"],
        "sha_recomputed": delta["sha_recomputed"],
    }
    to_embed = delta["new"] + delta["drifted"]

    # Step 2 — embed delta.
    if args.skip_embed:
        summary["embed"] = {"skipped": True}
    elif not to_embed:
        summary["embed"] = {"skipped": True, "reason": "nothing to embed"}
    else:
        if not openai_client.key_present():
            summary["embed"] = {
                "skipped": True,
                "reason": "OpenAI key missing — delta would be silently dropped. Add key at ~/.secrets/openai_api_key and re-run.",
            }
            print(json.dumps(summary, indent=2))
            sys.exit(2)
        stats = _embed_mod.embed_subjects(
            to_embed,
            model=args.model,
            dry_run=args.dry_run,
            force_reembed=False,
        )
        summary["embed"] = stats

    # Step 2.5 — vacuum orphan vectors. Subjects can leave the corpus (noise
    # rules, channel exclusions, compaction) but their vectors linger and
    # keep participating in clustering. Drop vectors that are neither in the
    # current corpus nor members of a live cluster (protects frozen-window
    # clusters whose members are intentionally out-of-corpus). Skipped on
    # --dry-run.
    corpus_set = set()
    if not args.dry_run:
        corpus_set = set(sample(conn, target_size=None))
        member_set = {s for (s,) in conn.execute("SELECT DISTINCT subject FROM topic_brief_member")}
        orphans = [
            (s,) for (s,) in conn.execute("SELECT subject FROM embedding")
            if s not in corpus_set and s not in member_set
        ]
        if orphans:
            conn.executemany("DELETE FROM embedding WHERE subject = ?", orphans)
            conn.commit()
        summary["vacuum"] = {"orphan_vectors_deleted": len(orphans)}

    # Step 3 — cluster diff plan.
    plan = _diff_mod.diff(
        conn,
        min_cluster_size=args.min_cluster_size,
        jaccard_threshold=args.jaccard_threshold,
        min_overlap_for_match=args.min_overlap,
        window_days=args.cluster_window_days,
    )
    _diff_mod.PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _diff_mod.PLAN_PATH.write_text(json.dumps(plan, indent=2))
    summary["diff_plan"] = {
        "path": str(_diff_mod.PLAN_PATH),
        "n_old_clusters": plan["n_old_clusters"],
        "n_new_clusters": plan["n_new_clusters"],
        "summary": plan["summary"],
    }

    # Step 4 — apply (optional).
    if args.apply:
        # cmd_apply reads PLAN_PATH from disk + writes to DB.
        class _A:  # minimal argparse-like shim; cmd_apply only reads attrs it needs
            pass
        _diff_mod.cmd_apply(_A())
        summary["apply"] = {
            "ran": True,
            "pending_new_labels": str(_diff_mod.PENDING_NEW_LABELS_PATH)
            if _diff_mod.PENDING_NEW_LABELS_PATH.exists() else None,
        }
    else:
        summary["apply"] = {"ran": False}

    summary["finished_at"] = _now_iso()
    print(json.dumps(summary, indent=2, default=str))

    # Next-step hint (stderr so JSON stdout stays parseable).
    needs_chat = plan["summary"]["new"] + plan["summary"]["relabel"]
    if needs_chat == 0:
        print("\n✓ no chat-labeling needed — all new clusters matched old labels.", file=sys.stderr)
    else:
        print(
            f"\n⚠ {needs_chat} clusters need chat-labeling "
            f"({plan['summary']['new']} new + {plan['summary']['relabel']} relabel).",
            file=sys.stderr,
        )
        if args.apply:
            print(
                f"  Dump written to {_diff_mod.PENDING_NEW_LABELS_PATH}.\n"
                f"  In chat, read derive/cluster_label_rules.md, produce verdicts at\n"
                f"  state/verdicts.cluster_labels.json, then run:\n"
                f"    .venv/bin/python derive/label_clusters.py apply",
                file=sys.stderr,
            )
        else:
            print(
                "  Re-run with --apply to mutate topic_brief and emit the\n"
                "  pending_new_cluster_labels.json dump.",
                file=sys.stderr,
            )


# ── main ────────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="Show new/drifted/unchanged counts. No writes.")
    s.add_argument("--model", default=_embed_mod.DEFAULT_MODEL)
    s.set_defaults(fn=cmd_status)

    r = sub.add_parser("refresh", help="Detect delta → embed → diff plan (+ optional apply).")
    r.add_argument("--model", default=_embed_mod.DEFAULT_MODEL)
    r.add_argument("--min-cluster-size", type=int, default=5,
                   help="HDBSCAN min cluster size for fresh clustering (default 5).")
    r.add_argument("--jaccard-threshold", type=float, default=0.8,
                   help="Min Jaccard to preserve old label (default 0.8).")
    r.add_argument("--min-overlap", type=int, default=3,
                   help="Min new∩old member count to consider any match (default 3).")
    r.add_argument("--apply", action="store_true",
                   help="Run cluster_diff apply after planning. Mutates topic_brief.")
    r.add_argument("--skip-embed", action="store_true",
                   help="Skip the embed step (use when embed ran separately).")
    r.add_argument("--dry-run", action="store_true",
                   help="Pass-through to embed_subjects: detect only, no API calls.")
    r.add_argument("--cluster-window-days", type=int, default=None,
                   help="Only re-cluster subjects active in the last N days; "
                        "old clusters wholly outside the window are frozen "
                        "and carried verbatim. Default: cluster everything.")
    r.add_argument("--allow-granularity-change", action="store_true",
                   help="Override the min-cluster-size pin against the last "
                        "applied plan (expect a mass relabel).")
    r.set_defaults(fn=cmd_refresh)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

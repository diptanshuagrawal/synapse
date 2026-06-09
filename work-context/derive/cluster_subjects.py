"""
cluster_subjects.py — cluster embeddings into topic groups.

Pulls vectors from the `embedding` table, runs HDBSCAN with cosine distance,
prints a human-readable tree for eyeball validation. Does NOT persist to
`topic_brief` yet — that comes after LLM labeling. This is the eyeball step.

CLI
---
    .venv/bin/python derive/cluster_subjects.py \\
        --subjects-file /tmp/sample_50.txt \\
        --min-cluster-size 2

    Reads subjects to cluster (or `--all-embedded` for everything in the DB).
    Prints clusters grouped + content snippets.

Why HDBSCAN
-----------
- Finds clusters of arbitrary shape; no preset K.
- Marks weak/border points as "noise" (cluster -1) so we don't force-fit
  outliers into a wrong cluster.
- Works well at small N (50) and large N (5k); same code path.

cosine distance is implemented via L2 distance on unit-normalised vectors
(equivalent up to monotonic transform). sklearn HDBSCAN supports `metric='cosine'`
directly since 1.3+ — we use it explicitly.
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import defaultdict
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db  # noqa: E402
from derive.subject_content import get_content  # noqa: E402


def _unpack_vector(b: bytes) -> list[float]:
    n = len(b) // 4
    return list(struct.unpack(f"<{n}f", b))


def _load_vectors(conn, subjects: list[str] | None) -> tuple[list[str], list[list[float]], list[str]]:
    """Return (subjects, vectors, sources) in matching order, restricted to
    those that have embeddings."""
    # ORDER BY subject pins SQLite row order across runs. HDBSCAN itself is
    # deterministic on identical input but renumbers cluster_ids when row
    # order shifts. Stable ordering ⇒ stable cluster_ids when membership is
    # unchanged — fewer spurious "relabel" actions in cluster_diff.
    if subjects:
        ph = ",".join("?" * len(subjects))
        rows = conn.execute(
            f"SELECT subject, vector, source FROM embedding WHERE subject IN ({ph}) ORDER BY subject",
            tuple(subjects),
        ).fetchall()
    else:
        rows = conn.execute("SELECT subject, vector, source FROM embedding ORDER BY subject").fetchall()
    # Service-brief embeddings are reference docs, not activity — they get
    # vectors for retrieval but must NOT be clustered (would pollute topic
    # clusters + cluster_project_map). Always drop them here.
    rows = [r for r in rows if r[2] != "service"]
    subs = [r[0] for r in rows]
    vecs = [_unpack_vector(r[1]) for r in rows]
    srcs = [r[2] for r in rows]
    return subs, vecs, srcs


def _preview(content: str, max_chars: int = 100) -> str:
    """Best-effort one-line snippet — strips newlines, collapses whitespace,
    truncates."""
    s = " ".join(content.split())
    if len(s) > max_chars:
        s = s[:max_chars].rstrip() + "…"
    return s


def cluster(
    subjects: list[str] | None,
    min_cluster_size: int = 2,
    min_samples: int | None = None,
) -> None:
    import numpy as np
    from sklearn.cluster import HDBSCAN

    conn = get_db()
    subs, vecs, srcs = _load_vectors(conn, subjects)
    if not subs:
        print("no embeddings found for given subjects — embed first")
        return

    X = np.array(vecs, dtype=np.float32)
    n = len(subs)

    # HDBSCAN with cosine metric. For small N, min_cluster_size must be tight.
    hdb = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples or 1,
        metric="cosine",
    )
    labels = hdb.fit_predict(X)

    # Group subjects by cluster id.
    groups: dict[int, list[int]] = defaultdict(list)
    for i, lbl in enumerate(labels):
        groups[int(lbl)].append(i)

    # Report.
    noise = groups.pop(-1, [])
    n_clusters = len(groups)
    print(f"\n=== Cluster report ({n} subjects, {n_clusters} clusters, {len(noise)} noise) ===\n")

    # Sort clusters by size desc.
    for cid, idxs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        src_breakdown = defaultdict(int)
        for i in idxs:
            src_breakdown[srcs[i]] += 1
        src_str = "  ".join(f"{k}={v}" for k, v in sorted(src_breakdown.items()))
        print(f"── Cluster {cid}  ({len(idxs)} members, {src_str})")
        for i in idxs:
            subj = subs[i]
            _, content = get_content(conn, subj)
            print(f"     {srcs[i]:11s} {subj:45s} {_preview(content, 90)}")
        print()

    if noise:
        print(f"── Noise ({len(noise)} subjects unclustered):")
        for i in noise:
            subj = subs[i]
            _, content = get_content(conn, subj)
            print(f"     {srcs[i]:11s} {subj:45s} {_preview(content, 90)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--subjects-file", help="One subject per line")
    src.add_argument("--all-embedded", action="store_true", help="Use every row in `embedding`")

    ap.add_argument("--min-cluster-size", type=int, default=2,
                    help="HDBSCAN min cluster size. Default 2 — tight for small samples.")
    ap.add_argument("--min-samples", type=int, default=None,
                    help="HDBSCAN min_samples (controls noise-tolerance). Default 1.")
    args = ap.parse_args()

    if args.all_embedded:
        subjects = None
    else:
        subjects = [
            line.strip()
            for line in Path(args.subjects_file).read_text().splitlines()
            if line.strip()
        ]

    cluster(
        subjects=subjects,
        min_cluster_size=args.min_cluster_size,
        min_samples=args.min_samples,
    )


if __name__ == "__main__":
    main()

"""
embedding_query.py — analyze the `embedding` table.

Subcommands
-----------
    neighbors  <subject> [--k 10] [--source S]
        Top-K cosine-nearest neighbors for a subject. Tells you "what is this
        most similar to" — duplicate hunting, related-work surfacing.

    similar    --content "<text>" [--k 10]
        Embed an arbitrary string on the fly and find its nearest subjects.
        Useful for "find threads about X" without needing X to already exist
        as a subject. Costs one embedding call (~$0.0000002).

    duplicates [--threshold 0.92] [--source S]
        Pairs with cosine sim above threshold. Catches near-duplicate Jira
        tickets / threads / pages.

    outliers   [--k 5]
        Subjects whose K-th nearest neighbor is farthest (least connected
        to anything). Often: empty bot messages, one-off pings, junk.

    stats
        Per-source counts, mean intra-source similarity, model/dim summary.

Cosine similarity convention: 1.0 = identical, 0.0 = orthogonal. We sort by
similarity desc (closer first). Distance = 1 - similarity.
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


def _unpack(b: bytes):
    n = len(b) // 4
    return list(struct.unpack(f"<{n}f", b))


def _load_all(conn, source_filter: str | None = None):
    import numpy as np
    if source_filter:
        rows = conn.execute(
            "SELECT subject, vector, source FROM embedding WHERE source = ?",
            (source_filter,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT subject, vector, source FROM embedding").fetchall()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32), []
    subs = [r[0] for r in rows]
    vecs = np.array([_unpack(r[1]) for r in rows], dtype=np.float32)
    srcs = [r[2] for r in rows]
    # Normalize → cosine sim becomes plain dot product.
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = vecs / norms
    return subs, vecs, srcs


def _preview(content: str, n: int = 100) -> str:
    s = " ".join(content.split())
    return s[:n].rstrip() + ("…" if len(s) > n else "")


def cmd_neighbors(args):
    import numpy as np
    conn = get_db()
    subs, vecs, srcs = _load_all(conn, args.source)
    if args.subject not in subs:
        print(f"subject {args.subject!r} not in embedding table")
        return
    i = subs.index(args.subject)
    sims = vecs @ vecs[i]
    sims[i] = -1.0  # exclude self
    top = np.argsort(-sims)[: args.k]
    print(f"\nTop {args.k} nearest to {args.subject}  (source={srcs[i]})")
    _, content = get_content(conn, args.subject)
    print(f"  self: {_preview(content, 90)}\n")
    for j in top:
        _, c = get_content(conn, subs[j])
        print(f"  sim={sims[j]:.3f}  {srcs[j]:11s} {subs[j]:45s} {_preview(c, 80)}")


def cmd_similar(args):
    import numpy as np
    from derive.openai_client import embed
    conn = get_db()
    subs, vecs, srcs = _load_all(conn)
    if not subs:
        print("no embeddings — embed first")
        return
    qvec = np.array(embed([args.content])[0], dtype=np.float32)
    qvec = qvec / (np.linalg.norm(qvec) or 1.0)
    sims = vecs @ qvec
    top = np.argsort(-sims)[: args.k]
    print(f'\nTop {args.k} nearest to query: "{args.content[:60]}"\n')
    for j in top:
        _, c = get_content(conn, subs[j])
        print(f"  sim={sims[j]:.3f}  {srcs[j]:11s} {subs[j]:45s} {_preview(c, 80)}")


def cmd_duplicates(args):
    import numpy as np
    conn = get_db()
    subs, vecs, srcs = _load_all(conn, args.source)
    n = len(subs)
    if n < 2:
        print("not enough embeddings")
        return
    sim = vecs @ vecs.T
    np.fill_diagonal(sim, -1.0)
    pairs = []
    iu = np.triu_indices(n, k=1)
    mask = sim[iu] >= args.threshold
    for i, j, s in zip(iu[0][mask], iu[1][mask], sim[iu][mask]):
        pairs.append((float(s), int(i), int(j)))
    pairs.sort(reverse=True)
    print(f"\n{len(pairs)} pairs above sim={args.threshold}\n")
    for s, i, j in pairs[:50]:
        _, ci = get_content(conn, subs[i])
        _, cj = get_content(conn, subs[j])
        print(f"  sim={s:.3f}")
        print(f"    {srcs[i]:10s} {subs[i]:45s} {_preview(ci, 70)}")
        print(f"    {srcs[j]:10s} {subs[j]:45s} {_preview(cj, 70)}")
        print()


def cmd_outliers(args):
    import numpy as np
    conn = get_db()
    subs, vecs, srcs = _load_all(conn)
    n = len(subs)
    if n <= args.k:
        print("too few embeddings")
        return
    sim = vecs @ vecs.T
    np.fill_diagonal(sim, -1.0)
    # K-th nearest sim per row (higher = better connected).
    kth = np.partition(sim, -args.k, axis=1)[:, -args.k]
    order = np.argsort(kth)  # lowest first = most isolated
    print(f"\nMost isolated subjects (k={args.k} nearest-neighbor similarity is low)\n")
    for i in order[:20]:
        _, c = get_content(conn, subs[i])
        print(f"  k{args.k}-sim={kth[i]:+.3f}  {srcs[i]:11s} {subs[i]:45s} {_preview(c, 70)}")


def cmd_stats(args):
    import numpy as np
    conn = get_db()
    rows = conn.execute(
        "SELECT source, model, dim, COUNT(*) FROM embedding GROUP BY source, model, dim"
    ).fetchall()
    print("\n=== embedding table stats ===\n")
    for src, model, dim, c in rows:
        print(f"  {src:11s} {c:5d}  model={model} dim={dim}")
    subs, vecs, srcs = _load_all(conn)
    if not subs:
        return
    by_src = defaultdict(list)
    for i, s in enumerate(srcs):
        by_src[s].append(i)
    print("\n  intra-source mean cosine sim (signal of within-source coherence):")
    for src, idxs in by_src.items():
        if len(idxs) < 2:
            continue
        sub = vecs[idxs]
        sim = sub @ sub.T
        iu = np.triu_indices(len(idxs), k=1)
        print(f"    {src:11s} n={len(idxs):4d}  mean={sim[iu].mean():+.3f}  std={sim[iu].std():.3f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("neighbors")
    n.add_argument("subject")
    n.add_argument("--k", type=int, default=10)
    n.add_argument("--source", default=None)
    n.set_defaults(fn=cmd_neighbors)

    s = sub.add_parser("similar")
    s.add_argument("--content", required=True)
    s.add_argument("--k", type=int, default=10)
    s.set_defaults(fn=cmd_similar)

    d = sub.add_parser("duplicates")
    d.add_argument("--threshold", type=float, default=0.92)
    d.add_argument("--source", default=None)
    d.set_defaults(fn=cmd_duplicates)

    o = sub.add_parser("outliers")
    o.add_argument("--k", type=int, default=5)
    o.set_defaults(fn=cmd_outliers)

    st = sub.add_parser("stats")
    st.set_defaults(fn=cmd_stats)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

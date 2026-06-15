"""
validate_embeddings.py — one-shot validation wrapper.

Runs the full battery of sanity checks against the current `embedding` table
and emits a single readable report. Use this after every batch (50, 500, 5k)
before deciding whether to scale further.

What it runs (in order):

  1. stats             — per-source counts, model/dim, intra-source coherence
  2. cluster           — HDBSCAN on cosine; cluster sizes + content previews
  3. duplicates        — pairs with cosine sim ≥ threshold (default 0.92)
  4. outliers          — least-connected subjects (likely junk / one-off)
  5. cross-source      — clusters that span ≥2 sources (the real prize)
  6. random-neighbors  — pick 3 subjects per source, show top-5 nearest

Each section is delimited so you can grep / pipe to a file.

CLI
---
    .venv/bin/python derive/validate_embeddings.py
    .venv/bin/python derive/validate_embeddings.py --min-cluster-size 2 \\
        --dup-threshold 0.90 --out /tmp/validate_50.txt
"""

from __future__ import annotations

import argparse
import io
import random
import sys
from collections import defaultdict
from contextlib import redirect_stdout
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db  # noqa: E402
from derive.subject_content import get_content  # noqa: E402
from derive.sources_config import slack_workspace, atlassian_host  # noqa: E402


_SLACK_URL = f"https://{slack_workspace()}.slack.com/archives/{{ch}}/p{{ts_nodot}}"
_JIRA_URL = f"https://{atlassian_host()}/browse/{{key}}"
_CONF_URL = f"https://{atlassian_host()}/wiki/pages/{{pid}}"
_GH_URL = "https://github.com/{owner_repo}/pull/{num}"


def subject_url(subject: str) -> str:
    """Best-effort clickable URL per subject form."""
    if subject.startswith("slack:"):
        parts = subject.split(":")
        if len(parts) >= 3:
            ch, ts = parts[1], parts[2]
            return _SLACK_URL.format(ch=ch, ts_nodot=ts.replace(".", ""))
    if subject.startswith("page:"):
        return _CONF_URL.format(pid=subject.split(":", 1)[1])
    if "#" in subject and "/" in subject:
        owner_repo, num = subject.rsplit("#", 1)
        return _GH_URL.format(owner_repo=owner_repo, num=num)
    if "-" in subject and subject.split("-", 1)[0].isalpha():
        return _JIRA_URL.format(key=subject)
    return ""


def _load(conn):
    import numpy as np
    # ORDER BY subject keeps cluster_ids stable across validation runs.
    rows = conn.execute("SELECT subject, vector, source FROM embedding ORDER BY subject").fetchall()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32), []
    subs = [r[0] for r in rows]
    # Bulk-decode every vector blob in one pass: concat the raw little-endian
    # float32 bytes, reinterpret as one (N, dim) array. ~45x faster than per-row
    # struct.unpack into Python lists (2.7s -> 0.06s at 35k vecs), less memory.
    # bytearray() makes the buffer writable so downstream in-place ops are safe.
    vecs = np.frombuffer(
        bytearray(b"".join(r[1] for r in rows)), dtype=np.float32
    ).reshape(len(rows), -1)
    srcs = [r[2] for r in rows]
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = vecs / norms
    return subs, vecs, srcs


def _preview(content: str, n: int = 90) -> str:
    s = " ".join(content.split())
    return s[:n].rstrip() + ("…" if len(s) > n else "")


def _line(subject, source, content, sim=None):
    head = f"sim={sim:+.3f}  " if sim is not None else ""
    url = subject_url(subject)
    url_part = f"  ({url})" if url else ""
    return f"  {head}{source:11s} {subject:45s} {_preview(content, 80)}{url_part}"


def section_stats(conn, subs, vecs, srcs):
    import numpy as np
    print("\n" + "=" * 78)
    print("  1. STATS — table inventory + intra-source coherence")
    print("=" * 78)
    rows = conn.execute(
        "SELECT source, model, dim, COUNT(*) FROM embedding GROUP BY source, model, dim"
    ).fetchall()
    for src, model, dim, c in rows:
        print(f"  {src:11s} {c:5d}  model={model} dim={dim}")
    if len(subs) < 2:
        return
    by_src = defaultdict(list)
    for i, s in enumerate(srcs):
        by_src[s].append(i)
    print("\n  intra-source mean cosine sim (collapse-test; expect 0.2-0.5):")
    for src, idxs in sorted(by_src.items()):
        if len(idxs) < 2:
            continue
        sub = vecs[idxs]
        sim = sub @ sub.T
        iu = np.triu_indices(len(idxs), k=1)
        mean = float(sim[iu].mean())
        std = float(sim[iu].std())
        flag = "  ⚠ likely collapsed" if mean > 0.7 else ("  ⚠ too sparse" if mean < 0.05 else "")
        print(f"    {src:11s} n={len(idxs):4d}  mean={mean:+.3f}  std={std:.3f}{flag}")


def section_cluster(conn, subs, vecs, srcs, min_cluster_size):
    import numpy as np
    from sklearn.cluster import HDBSCAN
    print("\n" + "=" * 78)
    print("  2. CLUSTERS — HDBSCAN, cosine metric")
    print("=" * 78)
    if len(subs) < min_cluster_size:
        print("  too few subjects to cluster")
        return [], []
    hdb = HDBSCAN(min_cluster_size=min_cluster_size, min_samples=1, metric="cosine")
    labels = hdb.fit_predict(vecs)
    groups = defaultdict(list)
    for i, lbl in enumerate(labels):
        groups[int(lbl)].append(i)
    noise = groups.pop(-1, [])
    n_clusters = len(groups)
    print(f"  {len(subs)} subjects → {n_clusters} clusters + {len(noise)} noise\n")

    cross_source_clusters = []
    for cid, idxs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        src_breakdown = defaultdict(int)
        for i in idxs:
            src_breakdown[srcs[i]] += 1
        if len(src_breakdown) >= 2:
            cross_source_clusters.append((cid, idxs, dict(src_breakdown)))
        src_str = "  ".join(f"{k}={v}" for k, v in sorted(src_breakdown.items()))
        print(f"  ── Cluster {cid}  ({len(idxs)} members, {src_str})")
        for i in idxs[:6]:  # cap per-cluster print to keep report skimmable
            _, content = get_content(conn, subs[i])
            print(_line(subs[i], srcs[i], content))
        if len(idxs) > 6:
            print(f"     … {len(idxs) - 6} more")
        print()
    return labels, cross_source_clusters


def section_cross_source(conn, subs, srcs, cross_clusters):
    print("\n" + "=" * 78)
    print("  3. CROSS-SOURCE CLUSTERS — clusters spanning ≥2 sources (the prize)")
    print("=" * 78)
    if not cross_clusters:
        print("  none — embeddings may be source-biased (check stats for collapse)")
        return
    for cid, idxs, breakdown in cross_clusters:
        src_str = "  ".join(f"{k}={v}" for k, v in sorted(breakdown.items()))
        print(f"\n  Cluster {cid} ({len(idxs)} members, {src_str})")
        for i in idxs:
            _, c = get_content(conn, subs[i])
            print(_line(subs[i], srcs[i], c))


def section_duplicates(conn, subs, vecs, srcs, threshold):
    import numpy as np
    print("\n" + "=" * 78)
    print(f"  4. NEAR-DUPLICATES — cosine sim ≥ {threshold}")
    print("=" * 78)
    n = len(subs)
    if n < 2:
        print("  too few embeddings")
        return
    sim = vecs @ vecs.T
    np.fill_diagonal(sim, -1.0)
    iu = np.triu_indices(n, k=1)
    mask = sim[iu] >= threshold
    pairs = list(zip(iu[0][mask], iu[1][mask], sim[iu][mask]))
    pairs.sort(key=lambda p: -p[2])
    print(f"  {len(pairs)} pairs above threshold\n")
    for i, j, s in pairs[:25]:
        _, ci = get_content(conn, subs[int(i)])
        _, cj = get_content(conn, subs[int(j)])
        print(f"  sim={s:.3f}")
        print(_line(subs[int(i)], srcs[int(i)], ci))
        print(_line(subs[int(j)], srcs[int(j)], cj))
        print()


def section_outliers(conn, subs, vecs, srcs):
    import numpy as np
    print("\n" + "=" * 78)
    print("  5. OUTLIERS — most isolated subjects (k=3 nearest neighbor sim)")
    print("=" * 78)
    n = len(subs)
    if n < 5:
        print("  too few embeddings")
        return
    sim = vecs @ vecs.T
    np.fill_diagonal(sim, -1.0)
    kth = np.partition(sim, -3, axis=1)[:, -3]
    order = np.argsort(kth)
    for i in order[:15]:
        _, c = get_content(conn, subs[i])
        print(_line(subs[i], srcs[i], c, sim=float(kth[i])))


def section_random_neighbors(conn, subs, vecs, srcs, seed=42):
    import numpy as np
    print("\n" + "=" * 78)
    print("  6. RANDOM-NEIGHBORS — spot-check top-5 nearest for 2 subjects/source")
    print("=" * 78)
    by_src = defaultdict(list)
    for i, s in enumerate(srcs):
        by_src[s].append(i)
    rng = random.Random(seed)
    for src in sorted(by_src):
        sample = rng.sample(by_src[src], min(2, len(by_src[src])))
        for i in sample:
            _, c = get_content(conn, subs[i])
            print(f"\n  ANCHOR  {srcs[i]:11s} {subs[i]}")
            print(f"          {_preview(c, 100)}")
            url = subject_url(subs[i])
            if url:
                print(f"          {url}")
            sims = vecs @ vecs[i]
            sims[i] = -1.0
            top = np.argsort(-sims)[:5]
            for j in top:
                _, cj = get_content(conn, subs[j])
                print(_line(subs[j], srcs[j], cj, sim=float(sims[j])))


def run(out_path: str | None, min_cluster_size: int, dup_threshold: float):
    conn = get_db()
    subs, vecs, srcs = _load(conn)

    buf = io.StringIO()
    with redirect_stdout(buf):
        print("\n" + "#" * 78)
        print(f"#  EMBEDDING VALIDATION REPORT — n={len(subs)}")
        print("#" * 78)
        section_stats(conn, subs, vecs, srcs)
        _, cross = section_cluster(conn, subs, vecs, srcs, min_cluster_size)
        section_cross_source(conn, subs, srcs, cross)
        section_duplicates(conn, subs, vecs, srcs, dup_threshold)
        section_outliers(conn, subs, vecs, srcs)
        section_random_neighbors(conn, subs, vecs, srcs)
        print()

    report = buf.getvalue()
    print(report)
    if out_path:
        Path(out_path).write_text(report)
        print(f"\n[report also written to {out_path}]")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-cluster-size", type=int, default=2)
    ap.add_argument("--dup-threshold", type=float, default=0.92)
    ap.add_argument("--out", default=None, help="Also write report to this path")
    args = ap.parse_args()
    run(args.out, args.min_cluster_size, args.dup_threshold)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build a k-NN cosine-similarity GraphML from the embedding table for Gephi.

Nodes  = embedded subjects (slack/jira/confluence/github).
Edges  = each subject linked to its top-k nearest neighbours by cosine similarity.
Attrs  = source, cluster_id, cluster_label, role (for colouring/filtering in Gephi).
Output = state/embeddings.graphml
"""
import sqlite3
import struct
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "index" / "events.db"
OUT = ROOT / "state" / "embeddings.graphml"
K = 8  # neighbours per node

conn = sqlite3.connect(str(DB))
rows = conn.execute("SELECT subject, source, dim, vector FROM embedding").fetchall()
print(f"loaded {len(rows)} embeddings")

# one cluster per subject (first membership) + its label
mem = {}
for subj, cid, label in conn.execute(
    "SELECT m.subject, m.cluster_id, t.label "
    "FROM topic_brief_member m JOIN topic_brief t ON t.cluster_id = m.cluster_id"
):
    mem.setdefault(subj, (cid, label or f"cluster {cid}"))
conn.close()

subjects, sources, vecs = [], [], []
for subj, source, dim, blob in rows:
    v = np.frombuffer(blob, dtype=np.float32)
    if v.shape[0] != dim:
        continue
    subjects.append(subj)
    sources.append(source)
    vecs.append(v)

X = np.vstack(vecs).astype(np.float32)
X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-8)  # normalise → dot = cosine
n = X.shape[0]
print(f"matrix {X.shape}")

# top-K neighbours via chunked matmul (memory-safe)
edges = set()
CH = 1000
for i in range(0, n, CH):
    sims = X[i : i + CH] @ X.T  # (chunk, n)
    for r in range(sims.shape[0]):
        gi = i + r
        idx = np.argpartition(-sims[r], K + 1)[: K + 1]
        for j in idx:
            if j == gi:
                continue
            a, b = (gi, int(j)) if gi < j else (int(j), gi)
            edges.add((a, b, round(float(sims[r][j]), 4)))
print(f"{len(edges)} undirected edges")

def attr(subj):
    cid, label = mem.get(subj, (-1, "unclustered"))
    return cid, label

with open(OUT, "w") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    f.write('<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n')
    f.write('<key id="source" for="node" attr.name="source" attr.type="string"/>\n')
    f.write('<key id="cluster" for="node" attr.name="cluster" attr.type="int"/>\n')
    f.write('<key id="cluster_label" for="node" attr.name="cluster_label" attr.type="string"/>\n')
    f.write('<key id="weight" for="edge" attr.name="weight" attr.type="double"/>\n')
    f.write('<graph edgedefault="undirected">\n')
    for i, subj in enumerate(subjects):
        cid, label = attr(subj)
        f.write(
            f'<node id="n{i}">'
            f'<data key="source">{escape(sources[i])}</data>'
            f'<data key="cluster">{cid}</data>'
            f'<data key="cluster_label">{escape(label)}</data>'
            f"</node>\n"
        )
    for e, (a, b, w) in enumerate(edges):
        f.write(f'<edge id="e{e}" source="n{a}" target="n{b}"><data key="weight">{w}</data></edge>\n')
    f.write("</graph>\n</graphml>\n")

print(f"wrote {OUT}  ({n} nodes, {len(edges)} edges)")

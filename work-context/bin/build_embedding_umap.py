#!/usr/bin/env python3
"""2D UMAP scatter of the embedding table, coloured by SOURCE with legend filters.

Projects every embedded subject's vector to 2D (UMAP cosine), then plots one
Plotly trace per (source × clustered?) so the legend doubles as a filter:
click a legend entry to hide/show it, double-click to isolate. Colour = source;
unclustered points are faded. Hover = subject + source + cluster label.

UMAP coords are cached to state/embeddings_umap_coords.npz so re-runs (e.g. to
re-style) are instant. Delete that file to force a fresh projection.
Output: state/embeddings_umap.html
"""
import sqlite3
from pathlib import Path

import numpy as np
import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "index" / "events.db"
OUT = ROOT / "state" / "embeddings_umap.html"
CACHE = ROOT / "state" / "embeddings_umap_coords.npz"

conn = sqlite3.connect(str(DB))
rows = conn.execute("SELECT subject, source, dim, vector FROM embedding").fetchall()
mem = {}
for subj, cid, label in conn.execute(
    "SELECT m.subject, m.cluster_id, t.label "
    "FROM topic_brief_member m JOIN topic_brief t ON t.cluster_id = m.cluster_id"
):
    mem.setdefault(subj, (cid, label or f"cluster {cid}"))
conn.close()

subjects, sources, vecs, labels, clustered = [], [], [], [], []
for subj, source, dim, blob in rows:
    v = np.frombuffer(blob, dtype=np.float32)
    if v.shape[0] != dim:
        continue
    cid, label = mem.get(subj, (-1, "unclustered"))
    subjects.append(subj); sources.append(source); vecs.append(v)
    labels.append(label); clustered.append(cid != -1)

X = np.vstack(vecs).astype(np.float32)
subjects = np.array(subjects); sources = np.array(sources)
labels = np.array(labels); clustered = np.array(clustered)

if CACHE.exists() and np.load(CACHE, allow_pickle=True)["xy"].shape[0] == X.shape[0]:
    xy = np.load(CACHE, allow_pickle=True)["xy"]
    print("reused cached UMAP coords")
else:
    import umap
    print(f"projecting {X.shape} with UMAP…")
    xy = umap.UMAP(n_neighbors=15, min_dist=0.1, metric="cosine", random_state=42).fit_transform(X)
    np.savez(CACHE, xy=xy)
    print("done + cached")

SRC_COLOR = {"slack": "#7C3AED", "jira": "#2684FF", "github": "#16A34A", "confluence": "#EA580C"}

fig = go.Figure()
for src in ["slack", "jira", "confluence", "github"]:
    color = SRC_COLOR.get(src, "#888")
    for is_clu in (True, False):
        m = (sources == src) & (clustered == is_clu)
        if not m.any():
            continue
        tag = "clustered" if is_clu else "unclustered"
        hov = [f"{s}<br>{src}<br>{lab}" for s, lab in zip(subjects[m], labels[m])]
        fig.add_trace(go.Scattergl(
            x=xy[m, 0], y=xy[m, 1], mode="markers", name=f"{src} · {tag}",
            marker=dict(size=4, color=color, opacity=0.8 if is_clu else 0.18),
            text=hov, hoverinfo="text",
        ))

fig.update_layout(
    title=f"Embedding map — {len(subjects)} subjects, coloured by source (click legend to filter)",
    width=1400, height=900, plot_bgcolor="white",
    legend=dict(itemsizing="constant", title="source · cluster status"),
    xaxis=dict(visible=False), yaxis=dict(visible=False),
)
fig.write_html(str(OUT))
print(f"wrote {OUT}")

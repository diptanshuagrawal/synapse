"""derive/validate_embeddings.py — embedding sanity-report sections.

The validation report's numpy sections (stats, duplicates, outliers,
random-neighbors) + the url/preview/line/load helpers are driven with in-test
embedding vectors. section_cluster + run() use sklearn HDBSCAN, so they're
guarded with importorskip (run locally, skip in CI where sklearn isn't installed).
"""

from __future__ import annotations

import struct

import pytest

np = pytest.importorskip("numpy")
from derive import validate_embeddings as ve


def _emb(conn, subject, vec, source="slack"):
    conn.execute(
        "INSERT INTO embedding (subject, source, vector, model, dim, content_sha, computed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (subject, source, struct.pack(f"<{len(vec)}f", *vec), "m", len(vec), "h", "t"))
    conn.commit()


def _seed_vecs(conn, n=6):
    # mixed sources, varied directions (≥2 per source for stats; ≥5 for outliers).
    for i in range(n):
        src = "slack" if i % 2 == 0 else "jira"
        v = [1.0, i * 0.1, 0.0, 0.0]
        _emb(conn, f"{src}:s{i}" if src == "slack" else f"EX-{i}", v, source=src)


# ── subject_url ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,frag", [
    ("slack:C0A:1700000000.000100", "archives/C0A"),
    ("page:123", "123"),
    ("org/repo#10", "org/repo"),
    ("EX-2629", "EX-2629"),
    ("weird", ""),
])
def test_subject_url(subject, frag):
    url = ve.subject_url(subject)
    assert (frag in url) if frag else (url == "")


# ── _preview / _line ─────────────────────────────────────────────────────────

def test_preview_and_line():
    assert ve._preview("a   b", 10) == "a b"
    line = ve._line("EX-1", "jira", "did a thing", sim=0.5)
    assert "EX-1" in line and "sim=+0.500" in line


# ── _load ────────────────────────────────────────────────────────────────────

def test_load_empty(db_conn):
    subs, vecs, srcs = ve._load(db_conn)
    assert subs == [] and vecs.shape[0] == 0


def test_load_normalises(db_conn):
    _emb(db_conn, "s1", [3.0, 4.0, 0.0, 0.0])
    subs, vecs, srcs = ve._load(db_conn)
    assert subs == ["s1"] and np.allclose(np.linalg.norm(vecs, axis=1), 1.0)


# ── numpy sections (capsys) ──────────────────────────────────────────────────

def test_section_stats(db_conn, capsys):
    _seed_vecs(db_conn)
    subs, vecs, srcs = ve._load(db_conn)
    ve.section_stats(db_conn, subs, vecs, srcs)
    out = capsys.readouterr().out
    assert "STATS" in out and "intra-source mean cosine" in out


def test_section_duplicates(db_conn, capsys):
    _emb(db_conn, "s1", [1.0, 0.0, 0.0, 0.0])
    _emb(db_conn, "s2", [0.999, 0.001, 0.0, 0.0])
    subs, vecs, srcs = ve._load(db_conn)
    ve.section_duplicates(db_conn, subs, vecs, srcs, threshold=0.9)
    assert "pairs above threshold" in capsys.readouterr().out


def test_section_outliers(db_conn, capsys):
    _seed_vecs(db_conn)
    subs, vecs, srcs = ve._load(db_conn)
    ve.section_outliers(db_conn, subs, vecs, srcs)
    assert "OUTLIERS" in capsys.readouterr().out


def test_section_random_neighbors(db_conn, capsys):
    _seed_vecs(db_conn)
    subs, vecs, srcs = ve._load(db_conn)
    ve.section_random_neighbors(db_conn, subs, vecs, srcs)
    assert "RANDOM-NEIGHBORS" in capsys.readouterr().out


# ── sklearn-dependent: cluster + full run (skip in CI) ───────────────────────

def test_section_cluster_and_run(db_conn, monkeypatch, capsys):
    pytest.importorskip("sklearn")
    _seed_vecs(db_conn, n=6)
    subs, vecs, srcs = ve._load(db_conn)
    labels, cross = ve.section_cluster(db_conn, subs, vecs, srcs, min_cluster_size=2)
    assert "CLUSTERS" in capsys.readouterr().out
    monkeypatch.setattr(ve, "get_db", lambda *a, **k: db_conn)
    ve.run(out_path=None, min_cluster_size=2, dup_threshold=0.92)
    assert "EMBEDDING VALIDATION REPORT" in capsys.readouterr().out

"""derive/embedding_query.py — vector load + similarity queries.

embedding_query powers ad-hoc neighbour/dup/similarity search over the embedding
table. _load_all is the core (bulk float32 decode + L2-normalise); _preview is
pure; the cmd_* run the numpy similarity math. Driven with float32 vectors
inserted into a temp embedding table + injected get_db.
"""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
from derive import embedding_query as eq


def _emb(conn, subject, vec, source="slack"):
    conn.execute(
        "INSERT INTO embedding (subject, source, vector, model, dim, content_sha, computed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (subject, source, struct.pack(f"<{len(vec)}f", *vec), "m", len(vec), "h",
         "2026-06-10T00:00:00Z"))
    conn.commit()


# ── _preview ─────────────────────────────────────────────────────────────────

def test_preview():
    assert eq._preview("a   b\tc") == "a b c"
    assert eq._preview("x" * 200, n=10).endswith("…")


# ── _load_all ────────────────────────────────────────────────────────────────

def test_load_all_empty(db_conn):
    subs, vecs, srcs = eq._load_all(db_conn)
    assert subs == [] and srcs == [] and vecs.shape[0] == 0


def test_load_all_decodes_and_normalises(db_conn):
    _emb(db_conn, "s1", [3.0, 4.0, 0.0])   # norm 5 → normalises to unit
    _emb(db_conn, "s2", [0.0, 0.0, 1.0])
    subs, vecs, srcs = eq._load_all(db_conn)
    assert subs == ["s1", "s2"] and vecs.shape == (2, 3)
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0)   # L2-normalised


def test_load_all_source_filter(db_conn):
    _emb(db_conn, "s1", [1.0, 0.0], source="slack")
    _emb(db_conn, "j1", [0.0, 1.0], source="jira")
    subs, _v, _s = eq._load_all(db_conn, source_filter="jira")
    assert subs == ["j1"]


# ── cmd_* (numpy sim paths) ──────────────────────────────────────────────────

def test_cmd_neighbors_runs(db_conn, monkeypatch, capsys):
    _emb(db_conn, "s1", [1.0, 0.0, 0.0])
    _emb(db_conn, "s2", [0.9, 0.1, 0.0])
    monkeypatch.setattr(eq, "get_db", lambda *a, **k: db_conn)
    eq.cmd_neighbors(SimpleNamespace(subject="s1", source=None, k=5))
    out = capsys.readouterr().out
    assert "nearest to s1" in out and "s2" in out


def test_cmd_neighbors_unknown_subject(db_conn, monkeypatch, capsys):
    _emb(db_conn, "s1", [1.0, 0.0])
    monkeypatch.setattr(eq, "get_db", lambda *a, **k: db_conn)
    eq.cmd_neighbors(SimpleNamespace(subject="ghost", source=None, k=5))
    assert "not in embedding table" in capsys.readouterr().out


def test_cmd_duplicates_finds_pair(db_conn, monkeypatch, capsys):
    _emb(db_conn, "s1", [1.0, 0.0, 0.0])
    _emb(db_conn, "s2", [0.99, 0.01, 0.0])   # near-duplicate
    monkeypatch.setattr(eq, "get_db", lambda *a, **k: db_conn)
    eq.cmd_duplicates(SimpleNamespace(source=None, threshold=0.9))
    assert "pairs above sim=0.9" in capsys.readouterr().out

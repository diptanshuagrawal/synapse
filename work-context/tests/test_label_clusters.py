"""derive/label_clusters.py — cluster dump/status helpers.

_sources_to_json (pure) + cmd_status (state-file + topic_brief inventory) run
seed-only; cmd_dump (HDBSCAN over the embedding table) is sklearn-guarded
(importorskip) and writes the pending-clusters JSON to a temp path.
"""

from __future__ import annotations

import json
import struct
from types import SimpleNamespace

import pytest

from derive import label_clusters as lc


def _emb(conn, subject, vec, source="slack"):
    conn.execute(
        "INSERT INTO embedding (subject, source, vector, model, dim, content_sha, computed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (subject, source, struct.pack(f"<{len(vec)}f", *vec), "m", len(vec), "h", "t"))
    conn.commit()


# ── _sources_to_json (pure) ──────────────────────────────────────────────────

def test_sources_to_json():
    assert json.loads(lc._sources_to_json("slack=4  jira=1")) == {"jira": 1, "slack": 4}
    assert json.loads(lc._sources_to_json("garbage")) == {}


def test_ensure_tables_noop(db_conn):
    assert lc._ensure_tables(db_conn) is None    # shim, schema lives in get_db


# ── cmd_status (seed) ────────────────────────────────────────────────────────

def test_cmd_status(seeded_db, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(lc, "get_db", lambda *a, **k: seeded_db)
    monkeypatch.setattr(lc, "PENDING_PATH", tmp_path / "p.json")
    monkeypatch.setattr(lc, "VERDICTS_PATH", tmp_path / "v.json")
    lc.cmd_status(SimpleNamespace())
    out = json.loads(capsys.readouterr().out)
    assert out["topic_brief_rows"] == 1 and out["pending_dump"] is False


# ── cmd_dump (sklearn-guarded) ───────────────────────────────────────────────

def test_cmd_dump_writes_pending(db_conn, monkeypatch, tmp_path, capsys):
    pytest.importorskip("sklearn")
    # two tight, well-separated groups of 4 → HDBSCAN forms clusters.
    for i in range(4):
        _emb(db_conn, f"slack:A:{i}", [1.0, 0.0, i * 0.0005, 0.0])
        _emb(db_conn, f"slack:B:{i}", [0.0, 1.0, i * 0.0005, 0.0])
    monkeypatch.setattr(lc, "get_db", lambda *a, **k: db_conn)
    monkeypatch.setattr(lc, "PENDING_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(lc, "RULES_PATH", tmp_path / "rules.md")
    lc.cmd_dump(SimpleNamespace(min_cluster_size=2, min_members=1,
                                max_members=None, member_chars=200))
    out = capsys.readouterr().out
    # cmd_dump ran the full load→cluster→dump path (either outcome is valid
    # coverage; with two separated groups we expect a dump).
    assert "dump complete" in out
    payload = json.loads((tmp_path / "pending.json").read_text())
    assert payload["n_subjects"] == 8 and payload["n_clusters"] >= 1

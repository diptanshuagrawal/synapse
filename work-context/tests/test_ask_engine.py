"""derive/ask_engine.py — window-state logic + person/cluster queries.

ask_engine powers /ask. The pure pieces (vector unpack, lifetime↔window
classification, person→actor-id resolution) are pinned directly; the
DB-backed queries are driven through the seeded DB by injecting it via
ask_engine.get_db (those functions open their own connection and never close
it, so substitution is safe).
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

_DERIVE = Path(__file__).resolve().parent.parent / "derive"
if str(_DERIVE) not in sys.path:
    sys.path.insert(0, str(_DERIVE))

from derive import ask_engine as ae  # noqa: E402

SINCE, UNTIL = "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z"


# ── _unpack ──────────────────────────────────────────────────────────────────

def test_unpack_roundtrips_float32():
    vec = [0.5, -1.25, 3.0]
    blob = struct.pack(f"<{len(vec)}f", *vec)
    assert ae._unpack(blob) == pytest.approx(vec)


# ── _compute_window_state (pure lifetime↔window) ────────────────────────────

def test_window_state_unknown_when_missing_ts():
    assert ae._compute_window_state(None, "x", SINCE, UNTIL) == "unknown"


@pytest.mark.parametrize("first,last,expected", [
    ("2026-06-02", "2026-06-05", "fully_in"),     # born + ended inside
    ("2026-06-02", "2026-07-01", "started_in"),   # born inside, alive after
    ("2026-05-01", "2026-06-05", "ended_in"),     # born before, ended inside
    ("2026-05-01", "2026-07-01", "spans"),        # alive before AND after
    ("2026-05-01", "2026-05-15", "pre_window"),   # fully before
    ("2026-07-01", "2026-07-05", "post_window"),  # fully after
])
def test_window_state_branches(first, last, expected):
    assert ae._compute_window_state(first, last, SINCE, UNTIL) == expected


# ── _resolve_person (people.yaml driven) ─────────────────────────────────────

def test_resolve_person_returns_actor_ids(seeded_db):
    # seeded_db injects SEED_PEOPLE into common._people_config.
    ids = ae._resolve_person("alice")
    assert "U0ALICE" in ids and "alice-gh" in ids


def test_resolve_person_unknown_empty(seeded_db):
    assert ae._resolve_person("nobody") == []


# ── clusters_for_person (seed via injected get_db) ───────────────────────────

def test_clusters_for_person_finds_incident(seeded_db, monkeypatch):
    monkeypatch.setattr(ae, "get_db", lambda *a, **k: seeded_db)
    out = ae.clusters_for_person("alice", SINCE, UNTIL)
    # alice authored the slack thread in cluster 1.
    assert any(c["cluster_id"] == 1 for c in out)


def test_clusters_for_person_unknown_is_empty(seeded_db, monkeypatch):
    monkeypatch.setattr(ae, "get_db", lambda *a, **k: seeded_db)
    assert ae.clusters_for_person("nobody", SINCE, UNTIL) == []


# ── clusters_active_in_window ────────────────────────────────────────────────

def test_clusters_active_in_window_tags_window_state(seeded_db, monkeypatch):
    monkeypatch.setattr(ae, "get_db", lambda *a, **k: seeded_db)
    out = ae.clusters_active_in_window(SINCE, UNTIL)
    assert isinstance(out, list)
    for c in out:
        assert c["window_state"] in (
            "fully_in", "started_in", "ended_in", "spans", "unknown")

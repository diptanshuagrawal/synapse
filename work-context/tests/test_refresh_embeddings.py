"""derive/refresh_embeddings.py — embedding delta detection.

detect_delta is the incremental engine: it classifies the corpus into
new / drifted / unchanged / no_content vs the embedding table, using a
content-sha cache keyed on event-count + last-ts. Driven against the seed with
the corpus sampler stubbed to the seed subjects, so the new→embed→unchanged and
drifted transitions are pinned offline.
"""

from __future__ import annotations

import pytest

from derive import refresh_embeddings as re_mod
from derive import embed_subjects as es
from tests.conftest import SEED_SUBJECTS

SUBJECTS = list(SEED_SUBJECTS.values())
MODEL = es.DEFAULT_MODEL


@pytest.fixture
def wired(seeded_db, monkeypatch):
    # corpus sampler → the seed subjects; get_db → seeded conn for both modules.
    monkeypatch.setattr(re_mod, "sample", lambda conn, target_size=None: SUBJECTS)
    monkeypatch.setattr(re_mod, "get_db", lambda *a, **k: seeded_db)
    return seeded_db


def test_delta_all_new_when_no_embeddings(wired):
    d = re_mod.detect_delta(wired, MODEL)
    # epic has empty body → no_content; the other 4 are new.
    assert d["n_no_content"] == 1
    assert d["n_new"] == 4
    assert d["n_unchanged"] == 0


def test_delta_unchanged_after_embed(wired, monkeypatch):
    monkeypatch.setattr(es, "get_db", lambda *a, **k: wired)
    monkeypatch.setattr(es.openai_client, "key_present", lambda: True)
    monkeypatch.setattr(es.openai_client, "embed",
                        lambda texts, model=None: [[0.1, 0.2, 0.3, 0.4] for _ in texts])
    es.embed_subjects(SUBJECTS, model=MODEL)

    d = re_mod.detect_delta(wired, MODEL)
    assert d["n_new"] == 0 and d["n_drifted"] == 0
    assert d["n_unchanged"] == 4


def test_delta_drift_detected(wired):
    # Plant an embedding row with a stale content_sha → subject reads as drifted.
    wired.execute(
        "INSERT INTO embedding (subject, source, vector, model, dim, content_sha, computed_at) "
        "VALUES (?, 'github', X'00', ?, 1, 'STALE_SHA', '2026-06-01T00:00:00Z')",
        (SEED_SUBJECTS["pr"], MODEL))
    wired.commit()
    d = re_mod.detect_delta(wired, MODEL)
    assert SEED_SUBJECTS["pr"] in d["drifted"]


def test_delta_sha_cache_hit_on_rerun(wired):
    re_mod.detect_delta(wired, MODEL)         # first pass populates embed_content_cache
    d2 = re_mod.detect_delta(wired, MODEL)    # second pass: every corpus subject hits cache
    assert d2["sha_cache_hits"] == len(SUBJECTS)   # all 5 cached (incl. no-content epic)
    assert d2["sha_recomputed"] == 0

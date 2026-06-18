"""derive/embed_subjects.py — embedding pipeline (OpenAI mocked).

embed_subjects resolves each subject's content, skips unchanged (content_sha),
batch-embeds the rest, and upserts the embedding table. The OpenAI call + get_db
are stubbed so the whole skip/embed/upsert flow runs offline against the seed.
_pack_vector/_unpack_vector round-trip and _existing_embeddings are pinned too.
"""

from __future__ import annotations

import pytest

from derive import embed_subjects as es
from tests.conftest import SEED_SUBJECTS

SUBJECTS = list(SEED_SUBJECTS.values())   # all 5 seed subjects (have content)
FAKE_VEC = [0.1, 0.2, 0.3, 0.4]


@pytest.fixture
def wired(seeded_db, monkeypatch):
    """Point embed_subjects at the seeded DB + a fake OpenAI embedder."""
    monkeypatch.setattr(es, "get_db", lambda *a, **k: seeded_db)
    monkeypatch.setattr(es.openai_client, "key_present", lambda: True)
    monkeypatch.setattr(es.openai_client, "embed",
                        lambda texts, model=None: [list(FAKE_VEC) for _ in texts])
    return seeded_db


# ── pack/unpack round-trip ───────────────────────────────────────────────────

def test_pack_unpack_roundtrip():
    assert es._unpack_vector(es._pack_vector(FAKE_VEC)) == pytest.approx(FAKE_VEC)


# ── embed flow ───────────────────────────────────────────────────────────────

def test_embed_writes_rows(wired):
    stats = es.embed_subjects(SUBJECTS)
    assert stats["embedded"] == stats["to_embed"] > 0
    assert stats["dim"] == 4
    n = wired.execute("SELECT COUNT(*) FROM embedding").fetchone()[0]
    assert n == stats["embedded"]


def test_embed_dry_run_writes_nothing(wired):
    stats = es.embed_subjects(SUBJECTS, dry_run=True)
    assert stats["to_embed"] > 0 and stats["embedded"] == 0
    assert wired.execute("SELECT COUNT(*) FROM embedding").fetchone()[0] == 0


def test_embed_skips_unchanged_on_rerun(wired):
    s1 = es.embed_subjects(SUBJECTS)
    s2 = es.embed_subjects(SUBJECTS)   # nothing changed → re-skips exactly what was embedded
    assert s2["skipped_unchanged"] == s1["embedded"] and s2["embedded"] == 0


def test_embed_force_reembed(wired):
    es.embed_subjects(SUBJECTS)
    stats = es.embed_subjects(SUBJECTS, force_reembed=True)
    assert stats["embedded"] > 0   # re-embedded despite unchanged content


def test_embed_skips_no_content(wired):
    stats = es.embed_subjects(["nonexistent-subject-xyz"])
    assert stats["skipped_no_content"] == 1 and stats["to_embed"] == 0


def test_embed_no_key_errors(seeded_db, monkeypatch):
    monkeypatch.setattr(es, "get_db", lambda *a, **k: seeded_db)
    monkeypatch.setattr(es.openai_client, "key_present", lambda: False)
    stats = es.embed_subjects(SUBJECTS)
    assert stats["embedded"] == 0 and any("OpenAI key" in e for e in stats["errors"])


def test_embed_count_mismatch_errors(seeded_db, monkeypatch):
    monkeypatch.setattr(es, "get_db", lambda *a, **k: seeded_db)
    monkeypatch.setattr(es.openai_client, "key_present", lambda: True)
    # return the wrong number of vectors → mismatch guard fires.
    monkeypatch.setattr(es.openai_client, "embed", lambda texts, model=None: [FAKE_VEC])
    stats = es.embed_subjects(SUBJECTS)
    assert any("mismatch" in e for e in stats["errors"]) and stats["embedded"] == 0


# ── _existing_embeddings ─────────────────────────────────────────────────────

def test_existing_embeddings(wired):
    s1 = es.embed_subjects(SUBJECTS)
    ex = es._existing_embeddings(wired, SUBJECTS, es.DEFAULT_MODEL)
    # reports back EXACTLY the embedded subjects = all but the empty-body epic.
    assert set(ex) == set(SUBJECTS) - {SEED_SUBJECTS["epic"]}
    assert len(ex) == s1["embedded"]

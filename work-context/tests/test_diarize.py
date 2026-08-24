"""derive/meetings/diarize.py — over-split cluster merge.

Pins the 2026-07-27 fix: pyannote sometimes splits ONE speaker into several
clusters (a lone far-side voice on a dual-stream call came back as Speaker 1 +
Speaker 2). Clusters that are really the same person have near-identical
voiceprints, so they're merged; genuinely distinct voices are never collapsed.
Only the pure merge helper is tested — the pyannote/torch run needs the
side-loaded models and is exercised live.
"""
from __future__ import annotations

import numpy as np

from derive.meetings import voice_gallery as vg
from derive.meetings.diarize import (
    _absorb_tiny_clusters,
    _merge_by_gallery,
    _merge_oversplit,
)


def _turn(speaker: str) -> dict:
    return {"start_ms": 0, "end_ms": 100, "speaker": speaker}


def _dur_turn(speaker: str, secs: float) -> dict:
    return {"start_ms": 0, "end_ms": int(secs * 1000), "speaker": speaker}


def _spk(turns: list[dict]) -> int:
    return len({t["speaker"] for t in turns})


def test_near_identical_voiceprints_merge():
    # One far-side voice pyannote split in two → near-identical voiceprints → one speaker.
    emb = {"SPEAKER_00": [1.0, 0.0] + [0.0] * 254, "SPEAKER_01": [0.98, 0.02] + [0.0] * 254}
    turns = [_turn("SPEAKER_00"), _turn("SPEAKER_01")]
    out_turns, out_emb, merges = _merge_oversplit(turns, emb, 0.82)
    assert _spk(out_turns) == 1
    assert len(merges) == 1
    assert len(out_emb) == 1


def test_distinct_voices_are_kept():
    # Two genuinely different speakers (orthogonal voiceprints) must NOT merge.
    emb = {"SPEAKER_00": [1.0, 0.0] + [0.0] * 254, "SPEAKER_01": [0.0, 1.0] + [0.0] * 254}
    turns = [_turn("SPEAKER_00"), _turn("SPEAKER_01")]
    out_turns, out_emb, merges = _merge_oversplit(turns, emb, 0.82)
    assert _spk(out_turns) == 2
    assert merges == []


def test_two_same_one_distinct_yields_two():
    emb = {
        "A": [1.0, 0.0] + [0.0] * 254,
        "B": [0.99, 0.01] + [0.0] * 254,   # ~= A → merges with A
        "C": [0.0, 1.0] + [0.0] * 254,     # distinct → stays
    }
    turns = [_turn("A"), _turn("B"), _turn("C")]
    out_turns, _, _ = _merge_oversplit(turns, emb, 0.82)
    assert _spk(out_turns) == 2


def test_no_voiceprints_is_noop():
    turns = [_turn("SPEAKER_00"), _turn("SPEAKER_01")]
    out_turns, out_emb, merges = _merge_oversplit(turns, {}, 0.82)
    assert out_turns == turns and merges == []


# --- _absorb_tiny_clusters: duration-based phantom guard -------------------
# The real bug: a lone far-side voice diarized as a 47.8s real cluster + a 0.4s
# phantom that was too short to embed (no voiceprint), so _merge_oversplit could
# not reach it. The duration pass folds it in.

def test_tiny_phantom_without_voiceprint_absorbed():
    # No voiceprints at all (the phantom never embedded) — must still collapse.
    turns = [_dur_turn("SPEAKER_00", 47.8), _dur_turn("SPEAKER_01", 0.4)]
    out_turns, out_emb, absorbed = _absorb_tiny_clusters(turns, {}, 2.5)
    assert _spk(out_turns) == 1
    assert absorbed == [("SPEAKER_01", "SPEAKER_00", 0.4)]
    assert {t["speaker"] for t in out_turns} == {"SPEAKER_00"}


def test_two_real_speakers_above_floor_kept():
    # Both well above the floor → genuine 2-party call, never collapsed.
    turns = [_dur_turn("SPEAKER_00", 30.0), _dur_turn("SPEAKER_01", 20.0)]
    out_turns, _, absorbed = _absorb_tiny_clusters(turns, {}, 2.5)
    assert _spk(out_turns) == 2 and absorbed == []


def test_tiny_cluster_routes_to_nearest_by_voiceprint():
    # 3 clusters; the tiny one embeds and is acoustically closest to B, not the
    # dominant A → it must fold into B, not the longest-talker.
    turns = [
        _dur_turn("A", 40.0), _dur_turn("B", 10.0), _dur_turn("C", 0.5),
    ]
    emb = {
        "A": [1.0, 0.0] + [0.0] * 254,
        "B": [0.0, 1.0] + [0.0] * 254,
        "C": [0.02, 0.98] + [0.0] * 254,  # ~= B
    }
    out_turns, out_emb, absorbed = _absorb_tiny_clusters(turns, emb, 2.5)
    assert absorbed == [("C", "B", 0.5)]
    assert "C" not in {t["speaker"] for t in out_turns}
    assert "C" not in out_emb


def test_two_mutually_nearest_tiny_clusters_no_cycle():
    # Two tiny clusters whose voiceprints are closest to EACH OTHER must not
    # remap into each other (a cycle the single-pass remap can't resolve) — both
    # fold into a surviving speaker. Result: 1 speaker, consistent emb_map.
    turns = [_dur_turn("A", 40.0), _dur_turn("B", 0.5), _dur_turn("C", 0.4)]
    emb = {
        "A": [1.0, 0.0] + [0.0] * 254,
        "B": [0.0, 1.0] + [0.0] * 254,
        "C": [0.01, 0.99] + [0.0] * 254,  # B and C ~= each other, both far from A
    }
    out_turns, out_emb, absorbed = _absorb_tiny_clusters(turns, emb, 2.5)
    survivors = {t["speaker"] for t in out_turns}
    assert survivors == {"A"}                       # both tiny folded away
    assert all(target not in ("B", "C") for _, target, _ in absorbed)  # no cycle
    assert set(out_emb) == survivors                # emb_map stays consistent


def test_min_sec_zero_disables():
    turns = [_dur_turn("SPEAKER_00", 47.8), _dur_turn("SPEAKER_01", 0.4)]
    out_turns, _, absorbed = _absorb_tiny_clusters(turns, {}, 0.0)
    assert _spk(out_turns) == 2 and absorbed == []


def test_single_cluster_is_noop():
    turns = [_dur_turn("SPEAKER_00", 0.4)]
    out_turns, _, absorbed = _absorb_tiny_clusters(turns, {}, 2.5)
    assert out_turns == turns and absorbed == []


# --- _merge_by_gallery: identity-based far-end merge -----------------------
# The real case: a 1:1 call's far side over-split into two clusters that BOTH
# match the same enrolled person (Sanket 0.77 + 0.50) — cluster-sim can't merge
# them (0.6/0.66 overlap) but the shared gallery identity can.

def _gallery():
    g = {}
    vg.enroll(np, g, "sanket", [1.0, 0.0, 0.0])
    vg.enroll(np, g, "other", [0.0, 0.0, 1.0])
    return g


def test_gallery_merge_same_voice():
    emb = {
        "A": [0.77, (1 - 0.77**2) ** 0.5, 0.0],   # ~0.77 to sanket
        "B": [0.505, (1 - 0.505**2) ** 0.5, 0.0], # ~0.505 to sanket
        "C": [0.0, 0.0, 1.0],                      # = other (lone → stays)
    }
    turns = [_turn("A"), _turn("B"), _turn("C")]
    out_turns, out_emb, merges = _merge_by_gallery(turns, emb, _gallery(), 0.5, 0.65)
    assert _spk(out_turns) == 2                    # A+B collapsed, C kept
    assert "B" not in {t["speaker"] for t in out_turns}
    assert "B" not in out_emb
    assert merges and merges[0][2] == "sanket"


def test_gallery_no_confident_anchor_no_merge():
    # Both match sanket but weakly (max < anchor 0.65) → don't merge on noise.
    emb = {
        "A": [0.55, (1 - 0.55**2) ** 0.5, 0.0],
        "B": [0.52, (1 - 0.52**2) ** 0.5, 0.0],
    }
    turns = [_turn("A"), _turn("B")]
    out_turns, _, merges = _merge_by_gallery(turns, emb, _gallery(), 0.5, 0.65)
    assert _spk(out_turns) == 2 and merges == []


def test_gallery_different_people_not_merged():
    emb = {"A": [1.0, 0.0, 0.0], "B": [0.0, 0.0, 1.0]}  # sanket vs other
    turns = [_turn("A"), _turn("B")]
    out_turns, _, merges = _merge_by_gallery(turns, emb, _gallery(), 0.5, 0.65)
    assert _spk(out_turns) == 2 and merges == []


def test_gallery_floor_zero_disables():
    emb = {"A": [1.0, 0.0, 0.0], "B": [0.99, 0.01, 0.0]}
    turns = [_turn("A"), _turn("B")]
    out_turns, _, merges = _merge_by_gallery(turns, emb, _gallery(), 0.0, 0.65)
    assert out_turns == turns and merges == []


def test_gallery_empty_is_noop():
    emb = {"A": [1.0, 0.0, 0.0], "B": [0.99, 0.01, 0.0]}
    turns = [_turn("A"), _turn("B")]
    out_turns, _, merges = _merge_by_gallery(turns, emb, {}, 0.5, 0.65)
    assert out_turns == turns and merges == []

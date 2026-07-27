"""derive/meetings/diarize.py — over-split cluster merge.

Pins the 2026-07-27 fix: pyannote sometimes splits ONE speaker into several
clusters (a lone far-side voice on a dual-stream call came back as Speaker 1 +
Speaker 2). Clusters that are really the same person have near-identical
voiceprints, so they're merged; genuinely distinct voices are never collapsed.
Only the pure merge helper is tested — the pyannote/torch run needs the
side-loaded models and is exercised live.
"""
from __future__ import annotations

from derive.meetings.diarize import _merge_oversplit


def _turn(speaker: str) -> dict:
    return {"start_ms": 0, "end_ms": 100, "speaker": speaker}


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

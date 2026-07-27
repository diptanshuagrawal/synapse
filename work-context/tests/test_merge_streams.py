"""derive/meetings/merge_streams.py — merge per-speaker whisper transcripts.

Pins the dual-stream merge: tolerant UTF-8 decode (whisper emits invalid bytes
on noisy audio — a mangled char must NOT crash the merge and drop the whole
meeting; regression for the 2026-07-20 fix), hallucination filtering, speaker
tagging, and timeline ordering across the two streams.
"""

from __future__ import annotations

import json

import pytest

from derive.meetings import merge_streams


@pytest.fixture(autouse=True)
def _identity_correct(monkeypatch):
    # Keep text verbatim — fuzzy correction is covered in test_correct.py and
    # would otherwise read the live roster/config here.
    monkeypatch.setattr(merge_streams, "_correct", lambda s: s)


def _seg(text: str, frm: int = 0, to: int = 1000) -> dict:
    return {"text": text, "offsets": {"from": frm, "to": to}}


def test_load_tags_filters_and_keeps_offsets(tmp_path):
    p = tmp_path / "me.json"
    p.write_text(json.dumps({"transcription": [
        _seg("hello there", 0, 500),
        _seg("   ", 500, 600),               # empty → dropped
        _seg("please subscribe", 600, 700),  # whisper hallucination → dropped
        _seg("सब्सक्राइब करें", 700, 800),   # Devanagari caption junk → dropped (HALLU_RE)
    ]}), encoding="utf-8")

    out = merge_streams.load(str(p), "Me")

    assert [s["text"] for s in out] == ["Me: hello there"]
    assert out[0]["offsets"] == {"from": 0, "to": 500}


def test_load_collapses_consecutive_loop(tmp_path):
    # Regression (2026-07-24): whisper repetition loops survived into the merged
    # .json because load() had NO dedup — only the display .txt was collapsed.
    # >2 consecutive identical lines must collapse to the first 2 here too.
    p = tmp_path / "them.json"
    p.write_text(json.dumps({"transcription": [
        _seg("No, I am going to dump it.", i * 100, i * 100 + 90) for i in range(148)
    ]}), encoding="utf-8")

    out = merge_streams.load(str(p), "Them")

    assert [s["text"] for s in out] == ["Them: No, I am going to dump it."] * 2


def test_load_caps_alternating_loop(tmp_path):
    # Alternating A/B/A/B loop never trips the consecutive guard — the total-cap
    # (6 occurrences) is what bounds it. 8 of each → 6 of each survive.
    p = tmp_path / "them.json"
    segs = []
    for i in range(8):
        segs.append(_seg("No, I am going to dump it.", i * 200, i * 200 + 90))
        segs.append(_seg("No need to update the clearing batch.", i * 200 + 100, i * 200 + 190))
    p.write_text(json.dumps({"transcription": segs}), encoding="utf-8")

    texts = [s["text"] for s in merge_streams.load(str(p), "Them")]

    assert texts.count("Them: No, I am going to dump it.") == 6
    assert texts.count("Them: No need to update the clearing batch.") == 6


def test_diarized_caps_total_repeats(tmp_path):
    # The single-mic diarized path caps total repeats too (previously only the
    # consecutive guard ran, so an alternating loop leaked into the merged json).
    whisper = tmp_path / "mic.json"
    segs = []
    for i in range(8):
        segs.append(_seg("dump it", i * 200, i * 200 + 90))
        segs.append(_seg("clearing", i * 200 + 100, i * 200 + 190))
    whisper.write_text(json.dumps({"transcription": segs}), encoding="utf-8")

    texts = [s["text"] for s in merge_streams.load_diarized(str(whisper), None)]

    assert texts.count("dump it") == 6
    assert texts.count("clearing") == 6


def test_load_tolerates_invalid_utf8(tmp_path):
    # Regression: whisper can emit invalid UTF-8 on noisy audio. A strict decode
    # raised UnicodeDecodeError and dropped the entire meeting (fixed 2026-07-20).
    p = tmp_path / "them.json"
    p.write_bytes(b'{"transcription":[{"text":"hello \x80 world","offsets":{"from":0,"to":500}}]}')

    out = merge_streams.load(str(p), "Them")  # must not raise

    assert len(out) == 1
    assert out[0]["text"].startswith("Them: hello")


def test_main_merges_two_streams_in_time_order(tmp_path, monkeypatch):
    me = tmp_path / "me.json"
    them = tmp_path / "them.json"
    me.write_text(json.dumps({"transcription": [_seg("second", 1000, 1200)]}), encoding="utf-8")
    them.write_text(json.dumps({"transcription": [_seg("first", 0, 200)]}), encoding="utf-8")
    out = tmp_path / "merged"

    monkeypatch.setattr(
        "sys.argv",
        ["merge_streams", "--me", str(me), "--them", str(them), "--out", str(out)],
    )
    merge_streams.main()

    data = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    assert [s["text"] for s in data["transcription"]] == ["Them: first", "Me: second"]
    assert (tmp_path / "merged.txt").exists()


def _turn(start_ms: int, end_ms: int, speaker: str) -> dict:
    return {"start_ms": start_ms, "end_ms": end_ms, "speaker": speaker}


def test_diarized_labels_by_dominant_overlap_and_first_appearance(tmp_path):
    # In-person single-mic path: whisper segments get `Speaker N` from the turn
    # they overlap most, numbered by who spoke first (SPEAKER_01 speaks first here
    # → Speaker 1, even though its raw label sorts second).
    whisper = tmp_path / "mic.json"
    whisper.write_text(json.dumps({"transcription": [
        _seg("morning all", 0, 900),      # overlaps SPEAKER_01 (first speaker)
        _seg("morning", 1000, 1400),      # overlaps SPEAKER_00
        _seg("shipping today", 1500, 1900),  # overlaps SPEAKER_01 again
    ]}), encoding="utf-8")
    diar = tmp_path / "mic.diar.json"
    diar.write_text(json.dumps({"turns": [
        _turn(0, 950, "SPEAKER_01"),
        _turn(1000, 1450, "SPEAKER_00"),
        _turn(1460, 2000, "SPEAKER_01"),
    ]}), encoding="utf-8")

    out = merge_streams.load_diarized(str(whisper), str(diar))

    assert [s["text"] for s in out] == [
        "Speaker 1: morning all",
        "Speaker 2: morning",
        "Speaker 1: shipping today",
    ]


def test_diarized_numbering_follows_turns_not_segment_order(tmp_path):
    # Speaker N is numbered by first appearance in the diarization TURNS (so it
    # matches the speakers.json sidecar), NOT by which whisper segment lands
    # first. SPEAKER_00's turn starts earliest even though its transcript line
    # comes second → it must be Speaker 1.
    whisper = tmp_path / "mic.json"
    whisper.write_text(json.dumps({"transcription": [
        _seg("second speaker talks", 700, 1000),   # SPEAKER_01 (segment appears first)
        _seg("first speaker talks", 1200, 1500),    # SPEAKER_00
    ]}), encoding="utf-8")
    diar = tmp_path / "mic.diar.json"
    diar.write_text(json.dumps({"turns": [
        _turn(0, 500, "SPEAKER_00"),     # earliest turn → Speaker 1
        _turn(600, 1050, "SPEAKER_01"),
        _turn(1100, 1600, "SPEAKER_00"),
    ]}), encoding="utf-8")

    out = merge_streams.load_diarized(str(whisper), str(diar))

    assert [s["text"] for s in out] == [
        "Speaker 2: second speaker talks",
        "Speaker 1: first speaker talks",
    ]


def test_diarized_missing_turns_degrades_to_plain_text(tmp_path):
    # Soft-failed diarizer (no turns file) → labels omitted, not a crash.
    whisper = tmp_path / "mic.json"
    whisper.write_text(json.dumps({"transcription": [_seg("hello", 0, 500)]}), encoding="utf-8")

    out = merge_streams.load_diarized(str(whisper), None)

    assert [s["text"] for s in out] == ["hello"]


def test_diarized_gap_segment_takes_nearest_turn(tmp_path):
    # A whisper segment landing in a diarization gap still gets a speaker
    # (nearest turn by start), never an unlabelled orphan mid-transcript.
    whisper = tmp_path / "mic.json"
    whisper.write_text(json.dumps({"transcription": [_seg("uh", 5000, 5200)]}), encoding="utf-8")
    diar = tmp_path / "mic.diar.json"
    diar.write_text(json.dumps({"turns": [_turn(0, 1000, "SPEAKER_00")]}), encoding="utf-8")

    out = merge_streams.load_diarized(str(whisper), str(diar))

    assert out[0]["text"] == "Speaker 1: uh"


# --- dual-stream CALL with the far-side ('them') diarized ---------------------


def test_them_diarize_splits_far_side_speakers(tmp_path, monkeypatch):
    # Two remote voices on one system-audio stream split into `Them · Speaker 1/2`
    # by dominant overlap, numbered by first appearance; the mic stays `Me:`.
    me = tmp_path / "me.json"
    them = tmp_path / "them.json"
    me.write_text(json.dumps({"transcription": [_seg("hi team", 0, 400)]}), encoding="utf-8")
    them.write_text(json.dumps({"transcription": [
        _seg("first remote", 500, 900),     # SPEAKER_00 (first far-side turn)
        _seg("second remote", 1000, 1400),  # SPEAKER_01
        _seg("first again", 1500, 1900),    # SPEAKER_00 again
    ]}), encoding="utf-8")
    diar = tmp_path / "them.diar.json"
    diar.write_text(json.dumps({"turns": [
        _turn(500, 950, "SPEAKER_00"),
        _turn(1000, 1450, "SPEAKER_01"),
        _turn(1460, 2000, "SPEAKER_00"),
    ]}), encoding="utf-8")
    out = tmp_path / "merged"

    monkeypatch.setattr("sys.argv", [
        "merge_streams", "--me", str(me), "--them", str(them),
        "--them-diarize", str(diar), "--out", str(out),
    ])
    merge_streams.main()

    data = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    assert [s["text"] for s in data["transcription"]] == [
        "Me: hi team",
        "Them · Speaker 1: first remote",
        "Them · Speaker 2: second remote",
        "Them · Speaker 1: first again",
    ]


def test_them_diarize_numbers_by_turn_first_appearance(tmp_path):
    # Far-side numbering follows the TURNS (so it matches the speakers.json
    # sidecar), not which whisper segment lands first. SPEAKER_00's turn starts
    # earliest even though its line comes second → `Them · Speaker 1`.
    them = tmp_path / "them.json"
    them.write_text(json.dumps({"transcription": [
        _seg("second speaker", 700, 1000),  # SPEAKER_01 (segment appears first)
        _seg("first speaker", 1200, 1500),  # SPEAKER_00
    ]}), encoding="utf-8")
    diar = tmp_path / "them.diar.json"
    diar.write_text(json.dumps({"turns": [
        _turn(0, 500, "SPEAKER_00"),        # earliest turn → Speaker 1
        _turn(600, 1050, "SPEAKER_01"),
        _turn(1100, 1600, "SPEAKER_00"),
    ]}), encoding="utf-8")

    out = merge_streams.load_them_diarized(str(them), str(diar))

    assert [s["text"] for s in out] == [
        "Them · Speaker 2: second speaker",
        "Them · Speaker 1: first speaker",
    ]


def test_them_without_diarize_stays_flat(tmp_path, monkeypatch):
    # No --them-diarize → today's flat `Them:` (unchanged dual-stream behavior).
    me = tmp_path / "me.json"
    them = tmp_path / "them.json"
    me.write_text(json.dumps({"transcription": [_seg("hi", 0, 400)]}), encoding="utf-8")
    them.write_text(json.dumps({"transcription": [_seg("hello", 500, 900)]}), encoding="utf-8")
    out = tmp_path / "merged"

    monkeypatch.setattr("sys.argv", [
        "merge_streams", "--me", str(me), "--them", str(them), "--out", str(out),
    ])
    merge_streams.main()

    data = json.loads((tmp_path / "merged.json").read_text(encoding="utf-8"))
    assert [s["text"] for s in data["transcription"]] == ["Me: hi", "Them: hello"]


def test_them_diarize_empty_turns_degrades_to_flat_them(tmp_path):
    # Soft-failed diarizer (a `{"turns":[]}` file) → flat `Them:`, never a crash.
    them = tmp_path / "them.json"
    them.write_text(json.dumps({"transcription": [_seg("hello", 0, 500)]}), encoding="utf-8")
    diar = tmp_path / "them.diar.json"
    diar.write_text(json.dumps({"turns": []}), encoding="utf-8")

    out = merge_streams.load_them_diarized(str(them), str(diar))

    assert [s["text"] for s in out] == ["Them: hello"]


def test_them_diarize_missing_file_degrades_to_flat_them(tmp_path):
    # Diarizer unavailable (exit 3/4 → no turns file written) → flat `Them:`.
    them = tmp_path / "them.json"
    them.write_text(json.dumps({"transcription": [_seg("hello", 0, 500)]}), encoding="utf-8")

    out = merge_streams.load_them_diarized(str(them), str(tmp_path / "nope.diar.json"))

    assert [s["text"] for s in out] == ["Them: hello"]

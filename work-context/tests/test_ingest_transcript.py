"""derive/meetings/ingest_transcript.py — whisper json → events.db segments.

Pins _read_segments: hallucination filtering and — the 2026-07-24 fix — the
whisper repetition-loop collapse, so events.db chunks never carry a line
repeated 148x. Mirrors the collapse coverage in test_merge_streams.py.
"""

from __future__ import annotations

import json

import pytest

from derive.meetings import ingest_transcript


@pytest.fixture(autouse=True)
def _identity_correct(monkeypatch):
    # Keep text verbatim — fuzzy correction is covered in test_correct.py.
    monkeypatch.setattr(ingest_transcript, "_correct", lambda s: s)


def _seg(text: str, frm: int = 0, to: int = 1000) -> dict:
    return {"text": text, "offsets": {"from": frm, "to": to}}


def test_read_segments_filters_empty_and_hallucination(tmp_path):
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"transcription": [
        _seg("real speech", 0, 500),
        _seg("   ", 500, 600),               # empty → dropped
        _seg("please subscribe", 600, 700),  # hallucination → dropped
    ]}), encoding="utf-8")

    out = ingest_transcript._read_segments(p)

    assert [s["text"] for s in out] == ["real speech"]
    assert out[0] == {"from_ms": 0, "to_ms": 500, "text": "real speech"}


def test_read_segments_collapses_consecutive_loop(tmp_path):
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"transcription": [
        _seg("No, I am going to dump it.", i * 100, i * 100 + 90) for i in range(148)
    ]}), encoding="utf-8")

    out = ingest_transcript._read_segments(p)

    assert [s["text"] for s in out] == ["No, I am going to dump it."] * 2


def test_read_segments_caps_alternating_loop(tmp_path):
    p = tmp_path / "w.json"
    segs = []
    for i in range(8):
        segs.append(_seg("No, I am going to dump it.", i * 200, i * 200 + 90))
        segs.append(_seg("No need to update the clearing batch.", i * 200 + 100, i * 200 + 190))
    p.write_text(json.dumps({"transcription": segs}), encoding="utf-8")

    texts = [s["text"] for s in ingest_transcript._read_segments(p)]

    assert texts.count("No, I am going to dump it.") == 6
    assert texts.count("No need to update the clearing batch.") == 6

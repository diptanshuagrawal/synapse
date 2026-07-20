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
    ]}), encoding="utf-8")

    out = merge_streams.load(str(p), "Me")

    assert [s["text"] for s in out] == ["Me: hello there"]
    assert out[0]["offsets"] == {"from": 0, "to": 500}


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

"""derive/meetings/voice_gallery.py — voiceprint enroll / identify / resolve.

Uses small synthetic embeddings (not real voiceprints). The gallery path is
monkeypatched to a tmp file so tests never touch the real ~/.steno-diarize gallery.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from derive.meetings import voice_gallery as vg


@pytest.fixture(autouse=True)
def _tmp_gallery(tmp_path, monkeypatch):
    monkeypatch.setattr(vg, "GALLERY", tmp_path / "voices.json")


def test_enroll_then_identify_above_and_below_threshold():
    g = {}
    vg.enroll(np, g, "alex", [1.0, 0.0, 0.0])
    vg.enroll(np, g, "sam", [0.0, 1.0, 0.0])

    # A vector close to alex → alex, above threshold.
    handle, score = vg.identify(np, g, [0.98, 0.05, 0.0], 0.55)
    assert handle == "alex" and score > 0.9

    # An orthogonal vector → no match (below threshold), handle None.
    handle, score = vg.identify(np, g, [0.0, 0.0, 1.0], 0.55)
    assert handle is None and score < 0.55


def test_enroll_running_mean():
    g = {}
    vg.enroll(np, g, "alex", [2.0, 0.0])
    vg.enroll(np, g, "alex", [0.0, 2.0])
    assert g["alex"]["n"] == 2
    assert g["alex"]["centroid"] == [1.0, 1.0]  # mean of the two


def test_save_and_load_roundtrip(tmp_path):
    vg.GALLERY = tmp_path / "voices.json"
    g = {"alex": {"centroid": [1.0, 2.0], "n": 1}}
    vg.save_gallery(g)
    assert vg.load_gallery() == g


def test_display_numbering_by_first_appearance():
    turns = [
        {"start_ms": 1000, "end_ms": 1500, "speaker": "SPEAKER_09"},
        {"start_ms": 0, "end_ms": 500, "speaker": "SPEAKER_03"},   # earliest
        {"start_ms": 2000, "end_ms": 2500, "speaker": "SPEAKER_09"},
    ]
    assert vg._display_numbering(turns) == {"SPEAKER_03": "Speaker 1", "SPEAKER_09": "Speaker 2"}


def test_resolve_matches_gallery_and_preserves_confirmed_name(tmp_path):
    vg.GALLERY = tmp_path / "voices.json"
    vg.save_gallery({"alex": {"centroid": [1.0, 0.0, 0.0], "n": 1}})

    diar = tmp_path / "m.diar.json"
    diar.write_text(json.dumps({
        "turns": [
            {"start_ms": 0, "end_ms": 900, "speaker": "SPEAKER_00"},
            {"start_ms": 1000, "end_ms": 1800, "speaker": "SPEAKER_01"},
        ],
        "embeddings": {"SPEAKER_00": [0.99, 0.02, 0.0], "SPEAKER_01": [0.0, 0.0, 1.0]},
    }))
    speakers = tmp_path / "m.speakers.json"
    # A prior owner confirmation on SPEAKER_01 must survive re-resolve.
    speakers.write_text(json.dumps({"SPEAKER_01": {"display": "Speaker 2", "handle": "sam", "name": "Sam"}}))

    args = type("A", (), {"diar": str(diar), "speakers": str(speakers), "threshold": 0.55})()
    vg.cmd_resolve(args)

    out = json.loads(speakers.read_text())
    assert out["SPEAKER_00"]["display"] == "Speaker 1"
    assert out["SPEAKER_00"]["auto"] == "alex" and out["SPEAKER_00"]["score"] > 0.9
    # confirmed identity untouched; a distinct voice gets no auto-match
    assert out["SPEAKER_01"]["name"] == "Sam" and out["SPEAKER_01"]["handle"] == "sam"
    assert out["SPEAKER_01"]["auto"] is None

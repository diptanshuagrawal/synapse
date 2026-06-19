"""derive/cluster_subjects.py — vector unpack + preview (pure).

The clustering itself is numpy/HDBSCAN over the embedding table; pinned here are
the pure edges: float32 vector round-trip and the content-preview truncation.
"""

from __future__ import annotations

import struct

import pytest

from derive import cluster_subjects as cs


def test_unpack_vector_roundtrip():
    vec = [0.5, -1.0, 2.25]
    assert cs._unpack_vector(struct.pack(f"<{len(vec)}f", *vec)) == pytest.approx(vec)


def test_preview_truncates_and_collapses():
    out = cs._preview("x" * 200, max_chars=10)
    assert len(out) <= 11 and out.startswith("x" * 10)


def test_preview_short_unchanged():
    assert cs._preview("short text") == "short text"

"""derive/cluster_diff.py — pure set-similarity helper.

cluster_diff decides which topic clusters are new/changed between refresh runs;
the apply/plan paths are DB-driven, but the Jaccard overlap that drives the
old↔new cluster matching is pure and easy to get subtly wrong (the empty-set
edge in particular).
"""

from __future__ import annotations

import pytest

from derive import cluster_diff as cd


def test_jaccard_identical():
    assert cd._jaccard({1, 2, 3}, {1, 2, 3}) == 1.0


def test_jaccard_disjoint():
    assert cd._jaccard({1, 2}, {3, 4}) == 0.0


def test_jaccard_partial():
    # |∩|=1, |∪|=3 → 1/3
    assert cd._jaccard({1, 2}, {2, 3}) == pytest.approx(1 / 3)


def test_jaccard_both_empty_is_one():
    assert cd._jaccard(set(), set()) == 1.0


def test_jaccard_one_empty_is_zero():
    assert cd._jaccard({1}, set()) == 0.0

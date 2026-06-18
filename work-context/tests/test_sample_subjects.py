"""derive/sample_subjects.py — embedding-corpus sampler.

sample() decides which subjects get embedded. The two correctness-critical
pieces: _weighted_sample_without_replacement (A-ExpJ — respects weights, drops
≤0, deterministic under a seeded RNG) and _parse_ratios. sample() itself is run
against the seed: the production path (target_size=None → everything) and the
must_include + target-size trim path.
"""

from __future__ import annotations

import random

import pytest

from derive import sample_subjects as ss


# ── _parse_ratios ────────────────────────────────────────────────────────────

def test_parse_ratios():
    assert ss._parse_ratios("slack=0.6, jira=0.4") == {"slack": 0.6, "jira": 0.4}


# ── _weighted_sample_without_replacement ─────────────────────────────────────

def test_weighted_sample_size_and_membership():
    rng = random.Random(1)
    items = ["a", "b", "c", "d"]
    out = ss._weighted_sample_without_replacement(items, [1, 1, 1, 1], 2, rng)
    assert len(out) == 2 and set(out) <= set(items) and len(set(out)) == 2


def test_weighted_sample_drops_zero_weight():
    rng = random.Random(1)
    # 'b' has weight 0 → can never be picked.
    out = ss._weighted_sample_without_replacement(["a", "b"], [5.0, 0.0], 2, rng)
    assert out == ["a"]   # only the positive-weight item survives


def test_weighted_sample_deterministic_under_seed():
    a = ss._weighted_sample_without_replacement(["a", "b", "c"], [1, 2, 3], 2, random.Random(7))
    b = ss._weighted_sample_without_replacement(["a", "b", "c"], [1, 2, 3], 2, random.Random(7))
    assert a == b   # same seed → same picks


# ── sample() on the seed ─────────────────────────────────────────────────────

def test_sample_production_path_returns_all(seeded_db):
    # target_size=None → every embeddable subject, stable order.
    out = ss.sample(seeded_db, target_size=None)
    assert "EX-2301" in out and "org/repo#10" in out
    assert len(out) == len(set(out))   # no duplicates


def test_sample_must_include_forced(seeded_db):
    out = ss.sample(seeded_db, target_size=3, must_include=["EX-2301"], seed=1)
    assert "EX-2301" in out
    assert len(out) <= 3 and len(out) == len(set(out))

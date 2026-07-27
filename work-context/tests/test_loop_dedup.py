"""derive/meetings/loop_dedup.py — shared whisper repetition-loop collapse.

Pins the two guards that keep a decoder loop out of the .txt, the merged .json,
and the events.db chunks: keep at most 2 consecutive identical lines, and cap
any identical line to 6 occurrences overall (alternating A/B loops). Semantics
mirror the awk collapses in bin/transcribe.sh — change both together.
"""

from __future__ import annotations

from derive.meetings.loop_dedup import CONSECUTIVE_MAX, TOTAL_MAX, LoopCollapser


def _run(texts):
    c = LoopCollapser()
    return [t for t in texts if c.keep(t)]


def test_constants_match_transcribe_sh():
    # bin/transcribe.sh: keep first 2 consecutive, cap at 6 total.
    assert CONSECUTIVE_MAX == 2
    assert TOTAL_MAX == 6


def test_consecutive_loop_keeps_first_two():
    assert _run(["a"] * 148) == ["a", "a"]


def test_alternating_loop_capped_at_total_max():
    seq = ["a", "b"] * 8  # never consecutive → only the total cap bounds it
    out = _run(seq)
    assert out.count("a") == TOTAL_MAX
    assert out.count("b") == TOTAL_MAX


def test_varied_content_passes_through_untouched():
    seq = [f"line {i}" for i in range(50)]
    assert _run(seq) == seq


def test_run_resets_between_distinct_lines():
    # a a b a a  — the second 'a a' run is a fresh run (b broke it), so both
    # survive; total 'a' count is 4 (< cap), so nothing is dropped.
    assert _run(["a", "a", "b", "a", "a"]) == ["a", "a", "b", "a", "a"]


def test_dropped_consecutive_do_not_count_toward_total():
    # 20 consecutive 'a' collapse to 2 via the consecutive guard; the 18 dropped
    # lines must NOT be charged against the total cap (matches transcribe.sh, where
    # the consecutive collapse runs before the total-cap pass).
    c = LoopCollapser()
    kept = [t for t in (["a"] * 20) if c.keep(t)]
    assert kept == ["a", "a"]
    # a later, non-consecutive 'a' still counts from 2, so 4 more survive.
    kept2 = [t for t in (["x", "a"] * 6) if c.keep(t)]
    assert kept2.count("a") == TOTAL_MAX - 2


def test_intra_line_word_loop_dropped():
    # Whisper word-level hallucination loop WITHIN one segment (2026-07-23 Positive
    # Pay: "सब्सक्राइब …" x14, "जुड़े …" x24). Language-agnostic — a single line
    # dominated by one repeated token is dropped; real speech is kept.
    from derive.meetings.loop_dedup import is_word_loop

    assert is_word_loop("सब्सक्राइब " * 14 + "कर दो")
    assert is_word_loop("look " * 8)
    assert not is_word_loop("branch banking may not have a UUID store at all")
    assert not is_word_loop("okay okay sure")  # short → below the min-repeat floor

    c = LoopCollapser()
    assert not c.keep("जुड़े " * 24)                       # word-loop → dropped
    assert c.keep("this is a normal sentence of speech")  # real → kept

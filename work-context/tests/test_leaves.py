"""derive/leaves_dump.py + render_leaves.py — team-leave detection + render.

leaves_dump.LEAVE_PATTERN is the regex prefilter that decides which Slack
messages even reach the leave classifier — too loose floods the chat pass, too
tight drops real OOO. render_leaves' formatters (_fmt_range, _days,
_clean_excerpt, _link, _channel_label) shape the rendered team-leaves table.
All pure.
"""

from __future__ import annotations

import pytest

from derive import leaves_dump as ld
from derive import render_leaves as rl


# ── LEAVE_PATTERN prefilter ──────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "I'll be OOO tomorrow",
    "on leave next week",
    "WFH today",
    "taking a day off",
    "back on Monday",
    "going on a break",
    "feeling sick, logging off",
    "half-day today",
    "travelling this week",
])
def test_leave_pattern_matches(text):
    assert ld.LEAVE_PATTERN.search(text)


@pytest.mark.parametrize("text", [
    "deploying the payout fix",
    "reviewing the TRD",
    "standup at 11",
])
def test_leave_pattern_ignores_work_chatter(text):
    assert not ld.LEAVE_PATTERN.search(text)


# ── render: _fmt_range ───────────────────────────────────────────────────────

def test_fmt_range():
    assert rl._fmt_range(None, None) == "_no dates_"
    assert rl._fmt_range("2026-06-10", None) == "2026-06-10"
    assert rl._fmt_range(None, "2026-06-12") == "… → 2026-06-12"
    assert rl._fmt_range("2026-06-10", "2026-06-10") == "2026-06-10"
    assert rl._fmt_range("2026-06-10", "2026-06-12") == "2026-06-10 → 2026-06-12"


# ── render: _days (inclusive) ────────────────────────────────────────────────

def test_days_inclusive():
    assert rl._days("2026-06-10", "2026-06-12") == "3"   # inclusive
    assert rl._days("2026-06-10", "2026-06-10") == "1"
    assert rl._days(None, "2026-06-12") == "-"
    assert rl._days("bad", "worse") == "-"


# ── render: _clean_excerpt ───────────────────────────────────────────────────

def test_clean_excerpt():
    assert rl._clean_excerpt("  multi   space\nhere ") == "multi space here"
    assert rl._clean_excerpt("a | b | c") == "a / b / c"   # pipes → slashes (cell-safe)
    assert rl._clean_excerpt("") == ""
    assert rl._clean_excerpt("x" * 100, max_len=10).endswith("…")


# ── render: _link / _channel_label ───────────────────────────────────────────

def test_link():
    assert rl._link("https://u", "lbl") == "[lbl](https://u)"
    assert rl._link(None, "lbl") == "lbl"


def test_channel_label():
    assert rl._channel_label("eng", "C0X") == "#eng"
    assert rl._channel_label("#already", None) == "#already"
    assert rl._channel_label(None, "C0X") == "#C0X"
    assert rl._channel_label("x" * 40, None).endswith("…")

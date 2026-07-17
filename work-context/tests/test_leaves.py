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


# ── _thread_root_ts (slack id → thread root) ─────────────────────────────────

def test_thread_root_ts_reply_id_uses_encoded_root():
    # 4-part reply id: slack:<cid>:<root_ts>:<reply_ts> → the root_ts.
    assert ld._thread_root_ts("slack:C0X:1700000000.0001:1700000050.0009", None) \
        == "1700000000.0001"


def test_thread_root_ts_root_with_thread_col():
    # 3-part root id + a thread_ts column → the column wins over own ts.
    assert ld._thread_root_ts("slack:C0X:1700000000.0001", "1699999999.0000") \
        == "1699999999.0000"


def test_thread_root_ts_root_without_thread_col_is_own_ts():
    # 3-part root id, no thread_ts → it is itself a root → own ts.
    assert ld._thread_root_ts("slack:C0X:1700000000.0001", None) == "1700000000.0001"


# ── LEAVE_PLAN_PROMPT (leave-plan thread detector) ───────────────────────────

@pytest.mark.parametrize("text", [
    "please share your leave plan for July",
    "Leave Plan thread",
    "drop your leave calendar here",
    "leaveplan",
])
def test_leave_plan_prompt_matches(text):
    assert ld.LEAVE_PLAN_PROMPT.search(text)


@pytest.mark.parametrize("text", ["on leave today", "I'm off tomorrow", "plan the sprint"])
def test_leave_plan_prompt_rejects_plain_leave(text):
    assert not ld.LEAVE_PLAN_PROMPT.search(text)


# ── _load_team_emails: roster = people.yaml scope:team, owner excluded ───────
# Consolidated 2026-07-16 (was a separate team.md roster). Leaves tracks the
# managed team only — the owner's own leaves stay out by design.

def test_team_emails_scope_team_owner_excluded(tmp_path, monkeypatch):
    import yaml
    p = tmp_path / "people.yaml"
    p.write_text(yaml.safe_dump({"people": [
        {"email": "owner@example.com", "scope": "team"},
        {"email": "dev1@example.com", "scope": "team"},
        {"email": "friend@example.com", "scope": "org"},
    ]}))
    monkeypatch.setattr(ld, "PEOPLE_YAML", p)
    monkeypatch.setattr(ld, "OWNER_EMAIL", "owner@example.com")
    assert ld._load_team_emails() == {"dev1@example.com"}


def test_team_emails_empty_when_people_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ld, "PEOPLE_YAML", tmp_path / "missing.yaml")
    monkeypatch.setattr(ld, "OWNER_EMAIL", "owner@example.com")
    assert ld._load_team_emails() == set()

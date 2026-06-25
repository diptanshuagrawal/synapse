"""derive/capacity_engine.py — the deterministic backlog classifier.

_classify_one scores a ticket from type / status / priority / keywords / SP / age;
classify_backlog orders by (-score, key); _adf_text flattens Jira ADF to plain text.
All pure — exercised with synthetic Jira-shaped tickets against a fixed ref date.
Relative assertions (Bug vs Story, P1 vs P2) stay robust to the private keyword tables.
"""

from __future__ import annotations

import datetime as dt

from derive import capacity_engine as ce

REF = dt.date(2026, 6, 24)


def _t(**kw):
    base = {"key": "K-1", "type": "Story", "status": "To Do", "priority": "P3",
            "summary": "alpha bravo", "desc": "charlie delta", "sp": 3,
            "created": "2026-06-10T00:00:00Z"}
    base.update(kw)
    return base


def test_bug_outranks_story_by_type_weight():
    # identical text/fields → keyword + age contributions cancel; only type differs.
    bug = ce._classify_one(_t(type="Bug"), REF)[0]
    story = ce._classify_one(_t(type="Story"), REF)[0]
    assert bug - story == 18          # +22 bug vs +4 story


def test_priority_p1_beats_p2():
    p1 = ce._classify_one(_t(priority="P1"), REF)[0]
    p2 = ce._classify_one(_t(priority="P2"), REF)[0]
    assert p1 - p2 == 17              # +25 vs +8


def test_near_done_status_weight():
    rev = ce._classify_one(_t(status="In Review"), REF)[0]
    todo = ce._classify_one(_t(status="To Do"), REF)[0]
    assert rev - todo == 18          # near-done +18


def test_quick_win_beats_unsized():
    quick = ce._classify_one(_t(sp=2), REF)[0]
    unsized = ce._classify_one(_t(sp=None), REF)[0]
    assert quick - unsized == 10     # SP<=2 +8 vs unsized -2


def test_chore_keyword_demotes_and_is_reasoned():
    chore = ce._classify_one(_t(summary="cleanup stale readme"), REF)
    plain = ce._classify_one(_t(summary="alpha bravo"), REF)
    assert "chore -8" in chore[2]
    assert chore[0] < plain[0]


def test_classify_backlog_sorts_by_score_then_key():
    pool = [_t(key="B-2", type="Story"), _t(key="A-1", type="Bug")]
    out = ce.classify_backlog(pool, REF)
    assert [t["key"] for t in out] == ["A-1", "B-2"]   # higher-scoring Bug first
    assert out[0]["score"] > out[1]["score"]
    assert all("reasons" in t and "category" in t for t in out)


def test_classify_backlog_ties_break_on_key():
    # identical tickets, different keys → score ties → ascending key order.
    pool = [_t(key="Z-9"), _t(key="A-1")]
    out = ce.classify_backlog(pool, REF)
    assert [t["key"] for t in out] == ["A-1", "Z-9"]


def test_adf_text_flattens_nested_nodes():
    node = {"type": "doc", "content": [
        {"type": "heading", "content": [{"text": "Title"}]},
        {"type": "paragraph", "content": [{"text": "a "}, {"text": "b"}]},
    ]}
    assert ce._adf_text(node) == "Title\na b\n"
    assert ce._adf_text("") == ""
    assert ce._adf_text({"text": "bare"}) == "bare"


def test_snap_to_sprint_start_tuesday_end_to_wednesday():
    # current sprint ends Tue Jul 7 → upcoming sprint starts Wed Jul 8.
    assert ce._snap_to_sprint_start(dt.date(2026, 7, 7)) == dt.date(2026, 7, 8)


def test_snap_to_sprint_start_already_wednesday_is_noop():
    assert ce._snap_to_sprint_start(dt.date(2026, 7, 8)) == dt.date(2026, 7, 8)


def test_snap_to_sprint_start_monday_advances_to_wednesday():
    assert ce._snap_to_sprint_start(dt.date(2026, 7, 6)) == dt.date(2026, 7, 8)

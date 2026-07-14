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


def test_month_bounds_regular_and_year_rollover():
    assert ce._month_bounds(2026, 7) == (dt.date(2026, 7, 1), dt.date(2026, 7, 31))
    assert ce._month_bounds(2026, 2) == (dt.date(2026, 2, 1), dt.date(2026, 2, 28))
    assert ce._month_bounds(2026, 12) == (dt.date(2026, 12, 1), dt.date(2026, 12, 31))


def test_oncall_by_week_probes_once_per_wednesday(monkeypatch):
    # rota hands over each Wednesday → probe once per week-Wed, fill the Wed→Tue week.
    calls = []
    monkeypatch.setattr(ce, "_oncall_email_on",
                        lambda d: (calls.append(d), f"p-{d.isoformat()}@x")[1])
    days = [dt.date(2026, 7, d) for d in range(8, 16)]   # Wed Jul 8 .. Wed Jul 15
    out = ce.oncall_by_week(days)
    assert sorted(calls) == [dt.date(2026, 7, 8), dt.date(2026, 7, 15)]   # only Wednesdays probed
    # Thu/Fri and following Mon/Tue all resolve to the Jul-8 Wednesday's person
    for iso in ("2026-07-09", "2026-07-10", "2026-07-13", "2026-07-14"):
        assert out[iso] == "p-2026-07-08@x"
    assert out["2026-07-15"] == "p-2026-07-15@x"
    assert "2026-07-11" not in out and "2026-07-12" not in out   # weekend skipped


def test_budget_fields_from_base_derives_twelve_consecutive_ids():
    # field IDs are config-driven; the helper maps Jan..Dec to consecutive IDs from a base
    m = ce._budget_fields_from_base("customfield_500")
    assert ce.BUDGET_MONTHS[0] == "Jan" and ce.BUDGET_MONTHS[-1] == "Dec"
    assert len(m) == 12
    assert m["Jan"] == "customfield_500"
    assert m["Aug"] == "customfield_507"
    assert m["Dec"] == "customfield_511"
    assert [int(m[x].split("_")[1]) for x in ce.BUDGET_MONTHS] == list(range(500, 512))


def test_budget_fields_from_base_handles_bad_input():
    assert ce._budget_fields_from_base("") == {}
    assert ce._budget_fields_from_base("nonsense") == {}


def test_parse_highs_lows_top_level_items_only():
    md = """# Retro
## Highs
1. **Alpha shipped** — 99.9% success.
    - detail sub-bullet ignored
2. Bravo live.
## Lows
1. Charlie slipped to next month.
- loose bullet ignored
## Metrics
1. not a high or low
"""
    highs, lows = ce._parse_highs_lows(md)
    assert highs == ["Alpha shipped — 99.9% success.", "Bravo live."]
    assert lows == ["Charlie slipped to next month."]


def test_parse_highs_lows_empty():
    assert ce._parse_highs_lows("no sections here") == ([], [])


# --- _opt_str: readable string from select / multi-select / user field values ---

def test_opt_str_select_dict_prefers_value_then_name():
    assert ce._opt_str({"value": "On Track"}) == "On Track"
    assert ce._opt_str({"name": "In Progress"}) == "In Progress"
    assert ce._opt_str({"value": "Green", "name": "ignored"}) == "Green"


def test_opt_str_multiselect_list_joins_values():
    assert ce._opt_str([{"value": "Scope creep"}, {"name": "Attrition"}]) == "Scope creep, Attrition"
    assert ce._opt_str(["plain", {"value": "mixed"}]) == "plain, mixed"


def test_opt_str_scalar_and_empty():
    assert ce._opt_str("Done") == "Done"
    assert ce._opt_str(None) == ""
    assert ce._opt_str("") == ""
    assert ce._opt_str([]) == ""

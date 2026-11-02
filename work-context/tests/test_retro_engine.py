"""Sprint retro: attribution, on-call split, budget months and identity matching.

Every test here guards a bug that was real, not hypothetical, and each one is silent when
it breaks — the view still renders, just with wrong numbers:

* Summing `sprint in (A,B)` counted a carryover once per sprint it ever sat in. Adjacent
  sprints read 175.9 SP where the deduped figure was 123.9.
* On-call work is real but unplanned, and `month_capacity()` has ALREADY removed on-call
  days from the denominator. Counting on-call SP in the numerator rewards the people who
  carried the rota twice over.
* A sprint window straddles calendar months (9 Sep -> 7 Oct is 22 days Sep, 7 days Oct)
  while the Budget fields are per month. Charging the window with every month it touches
  more than doubled the plan.
* Matching developers by DISPLAY NAME split one person in two: the roster's "Dev Two"
  is "K Dev Two" in Jira, giving a capacity row with no delivery and a delivery row
  with no capacity.

The Jira and capacity calls are stubbed, so these run with no network and no secrets.
"""
import datetime as dt
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "derive"))

import capacity_engine as ce
import retro_engine as re_


# ---------------------------------------------------------------- on-call regex

def test_oncall_matches_the_naming_this_team_uses():
    rx = re_.oncall_re()
    for s in ("Oncall", "oncall", "On-call rota", "on call support", "[Oncall Issue] fix X"):
        assert rx.search(s), s


def test_oncall_does_not_match_unrelated_words():
    rx = re_.oncall_re()
    for s in ("Reconcile balances", "Call the payments API", "Oncology"):
        assert not rx.search(s) or "call" in s.lower() and "on" in s.lower(), s
    assert not rx.search("Reconcile balances")


# ---------------------------------------------------------------- attribution

def _issue(key, sprints, sp=1.0, summary="work", res="Done", status="Done", acct="A1"):
    return {"key": key, "fields": {
        "summary": summary, ce.SP_FIELD or "customfield_10051": sp,
        re_.SPRINT_FIELD: [{"id": s} for s in sprints],
        "parent": {"key": "EP-1"}, "assignee": {"displayName": "Dev One", "accountId": acct},
        "status": {"name": status}, "resolution": {"name": res} if res else None,
        "resolutiondate": "2026-09-20T10:00:00.000+0530",
        "issuetype": {"name": "Task"}, "project": {"key": "BOARD"},
        re_.RSE_FIELD: "2026-09-22"}}


def test_a_carryover_is_attributed_to_the_sprint_it_finished_in():
    ends = {1: "2026-08-26", 2: "2026-09-09", 3: "2026-09-23"}
    got = re_._attribute([_issue("A-1", [1, 2, 3])], ends, re_.SPRINT_FIELD)
    assert got["A-1"] == 3, "must land in the LAST sprint of its history, not the first"


def test_each_ticket_is_attributed_exactly_once():
    ends = {1: "2026-08-26", 2: "2026-09-09"}
    got = re_._attribute([_issue("A-1", [1, 2]), _issue("A-2", [1])], ends, re_.SPRINT_FIELD)
    assert got == {"A-1": 2, "A-2": 1}
    assert len(got) == 2


def test_a_ticket_with_no_sprint_history_is_dropped_not_guessed():
    got = re_._attribute([_issue("A-1", [])], {1: "2026-09-09"}, re_.SPRINT_FIELD)
    assert "A-1" not in got


# ---------------------------------------------------------------- budget months

def test_window_months_counts_days_in_each_touched_month(monkeypatch):
    monkeypatch.setattr(ce, "BUDGET_FIELDS", {m: f"cf_{m}" for m in ce.BUDGET_MONTHS})
    rows = re_._window_month_days("2026-09-09", "2026-10-07")
    assert [(r["ym"], r["days"]) for r in rows] == [("2026-09", 22), ("2026-10", 7)]


def test_the_month_holding_most_of_the_window_is_primary(monkeypatch):
    monkeypatch.setattr(ce, "BUDGET_FIELDS", {m: f"cf_{m}" for m in ce.BUDGET_MONTHS})
    rows = re_._window_month_days("2026-09-09", "2026-10-07")
    assert [r["ym"] for r in rows if r["primary"]] == ["2026-09"]


def test_a_window_inside_one_month_has_that_month_primary(monkeypatch):
    monkeypatch.setattr(ce, "BUDGET_FIELDS", {m: f"cf_{m}" for m in ce.BUDGET_MONTHS})
    rows = re_._window_month_days("2026-09-02", "2026-09-15")
    assert len(rows) == 1 and rows[0]["primary"] and rows[0]["days"] == 14


def test_month_rows_carry_the_budget_field_for_that_month(monkeypatch):
    monkeypatch.setattr(ce, "BUDGET_FIELDS", {m: f"cf_{m}" for m in ce.BUDGET_MONTHS})
    rows = re_._window_month_days("2026-09-09", "2026-10-07")
    assert rows[0]["name"] == "Sep" and rows[0]["field"] == "cf_Sep"
    assert rows[1]["name"] == "Oct" and rows[1]["field"] == "cf_Oct"


# ---------------------------------------------------------------- productivity

def _cap(people):
    return {"people": people, "teamSP": sum(p["sp"] for p in people),
            "teamNetDays": sum(p["net"] for p in people), "days": []}


def _row(key, assignee, acct, sp, oncall=False, done=True, cancelled=False):
    return {"key": key, "summary": "x", "sp": sp, "estimated": True, "epic": "EP-1",
            "assignee": assignee, "accountId": acct, "status": "Done",
            "resolution": "Done", "resolved": "2026-09-20", "issuetype": "Task",
            "project": "BOARD", "sprint": 1, "rse": "", "oncall": oncall,
            "done": done, "cancelled": cancelled}


def _people(monkeypatch, rows, carried=(), roster=None, yaml_people=None):
    roster = roster or [{"name": "Dev One", "canonical": "dev-one", "role": "SDE2",
                         "sp": 10.0, "net": 10, "leave": 0, "oncall": 0, "wfh": 0}]
    monkeypatch.setattr(ce, "month_capacity", lambda *a, **k: _cap(roster))
    monkeypatch.setattr(ce, "_yaml", lambda f: {"people": yaml_people or []})
    return re_._people(rows, list(carried), "2026-09-09", "2026-10-07")


def test_oncall_sp_is_reported_but_never_counted_as_delivery(monkeypatch):
    out = _people(monkeypatch, [_row("A-1", "Dev One", "A1", 4.0),
                                _row("A-2", "Dev One", "A1", 6.0, oncall=True)])
    d = out[0]
    assert d["delivered"] == 4.0, "on-call SP must stay out of the numerator"
    assert d["oncallSP"] == 6.0
    # capacity already had the on-call DAYS removed, so counting the SP too double-credits
    assert d["efficiency"] == 40.0


def test_cancelled_work_is_not_delivery(monkeypatch):
    out = _people(monkeypatch, [_row("A-1", "Dev One", "A1", 5.0, cancelled=True)])
    assert out[0]["delivered"] == 0 and out[0]["cancelled"] == 5.0


def test_a_display_name_mismatch_does_not_split_one_person_in_two(monkeypatch):
    """The roster says "Dev Two"; Jira says "K Dev Two". accountId resolves it."""
    roster = [{"name": "Dev Two", "canonical": "dev-two", "role": "SDE2",
               "sp": 16.0, "net": 16, "leave": 0, "oncall": 0, "wfh": 0}]
    ypeople = [{"canonical": "dev-two", "name": "Dev Two", "jira_id": "712020:abc"}]
    out = _people(monkeypatch, [_row("A-1", "K Dev Two", "712020:abc", 4.0)],
                  roster=roster, yaml_people=ypeople)
    assert len(out) == 1, [p["name"] for p in out]
    assert out[0]["capacitySP"] == 16.0 and out[0]["delivered"] == 4.0


def test_without_an_account_match_the_person_is_shown_off_roster_not_merged(monkeypatch):
    out = _people(monkeypatch, [_row("A-1", "Someone Else", "ZZ", 3.0)])
    names = {p["name"]: p for p in out}
    assert names["Someone Else"]["offRoster"] is True
    assert names["Someone Else"]["efficiency"] is None, "no capacity row = no efficiency"
    assert names["Dev One"]["delivered"] == 0


def test_carried_work_is_attributed_to_the_dev_but_not_to_delivery(monkeypatch):
    carried = [{"key": "A-9", "summary": "x", "sp": 7.0, "assignee": "Dev One",
                "accountId": "A1", "status": "In Progress", "epic": "EP-1",
                "landedIn": 2, "resolved": "", "stillOpen": True}]
    out = _people(monkeypatch, [_row("A-1", "Dev One", "A1", 2.0)], carried=carried)
    assert out[0]["delivered"] == 2.0 and out[0]["carried"] == 7.0
    assert out[0]["accounted"] == 9.0


def test_efficiency_is_none_rather_than_zero_when_there_is_no_capacity(monkeypatch):
    out = _people(monkeypatch, [_row("A-1", "Nobody", "QQ", 1.0)])
    off = [p for p in out if p.get("offRoster")][0]
    assert off["efficiency"] is None and off["gap"] is None


def test_an_unestimated_done_ticket_is_counted_as_a_ticket_and_flagged(monkeypatch):
    r = _row("A-1", "Dev One", "A1", 0.0)
    r["estimated"] = False
    out = _people(monkeypatch, [r])
    assert out[0]["unestimated"] == 1
    assert out[0]["delivered"] == 0, "it genuinely carries no points"


# ---------------------------------------------------------------- guards

def test_no_sprints_is_an_error_not_an_empty_report():
    assert "__error__" in re_.sprint_retro([])


def test_schema_is_versioned():
    """The /api/retro2 cache keys on this; a payload change without a bump serves stale."""
    assert isinstance(re_.SCHEMA, int) and re_.SCHEMA >= 1


# ---------------------------------------------------------------- epic tree

def _tree(monkeypatch, epic_fields, rows, months=None, projects=("BOARD",)):
    months = months or [
        {"ym": "2026-09", "name": "Sep", "days": 22, "field": "cf_Sep", "primary": True},
        {"ym": "2026-10", "name": "Oct", "days": 7, "field": "cf_Oct", "primary": False}]
    monkeypatch.setattr(ce, "OINT_PROJECT", "IDEA")
    monkeypatch.setattr(re_, "_search",
                        lambda jql, want, **k: ([] if "is not EMPTY" in jql else
                                                [{"key": "EP-1", "fields": epic_fields}]))
    return re_._epic_tree(rows, [], months, list(projects))


def test_planned_uses_the_primary_month_only(monkeypatch):
    """The window is 22 days Sep and 7 days Oct. Charging it with Sep+Oct budget turned an
    8 SP plan into 26 and made a real overrun look like an underspend."""
    out = _tree(monkeypatch, {"summary": "E", "status": {"name": "In Progress"},
                              "project": {"key": "BOARD"}, "issuelinks": [],
                              "cf_Sep": 8, "cf_Oct": 18},
                [_row("A-1", "Dev One", "A1", 3.0)])
    e = out[0]["epics"][0]
    assert e["plannedTotal"] == 8, "primary month (Sep) only"
    assert e["plannedAll"] == 26, "both touched months stay visible"
    assert e["delta"] == 3.0 - 8


def test_an_epic_budgeted_with_nothing_delivered_still_gets_a_row(monkeypatch):
    """These are the rows a retro exists to find — in September, 10 epics held 61 SP of
    budget and produced no ticket at all, so a tickets-only tree hid every one of them."""
    monkeypatch.setattr(ce, "OINT_PROJECT", "IDEA")
    monkeypatch.setattr(ce, "BUDGET_FIELDS", {m: f"cf_{m}" for m in ce.BUDGET_MONTHS})
    seen = {}

    def fake(jql, want, **k):
        seen["budget_pull"] = seen.get("budget_pull") or ("is not EMPTY" in jql)
        if "is not EMPTY" in jql:
            return [{"key": "EP-9", "fields": {}}]
        return [{"key": "EP-9", "fields": {"summary": "Budgeted, untouched",
                                           "status": {"name": "To Do"},
                                           "project": {"key": "BOARD"},
                                           "issuelinks": [], "cf_Sep": 14}}]
    monkeypatch.setattr(re_, "_search", fake)
    months = [{"ym": "2026-09", "name": "Sep", "days": 22, "field": "cf_Sep", "primary": True}]
    out = re_._epic_tree([], [], months, ["BOARD"])
    assert seen["budget_pull"], "must look up budgeted epics, not only ones with tickets"
    e = out[0]["epics"][0]
    assert e["key"] == "EP-9" and e["plannedTotal"] == 14 and e["delivered"] == 0


def test_oncall_and_cancelled_are_split_out_of_epic_delivery(monkeypatch):
    out = _tree(monkeypatch, {"summary": "E", "status": {"name": "Done"},
                              "project": {"key": "BOARD"}, "issuelinks": [], "cf_Sep": 5},
                [_row("A-1", "Dev One", "A1", 2.0),
                 _row("A-2", "Dev One", "A1", 6.0, oncall=True),
                 _row("A-3", "Dev One", "A1", 4.0, cancelled=True)])
    e = out[0]["epics"][0]
    assert (e["delivered"], e["oncall"], e["cancelled"]) == (2.0, 6.0, 4.0)


def test_an_epic_with_no_initiative_link_is_grouped_not_dropped(monkeypatch):
    out = _tree(monkeypatch, {"summary": "E", "status": {"name": "Done"},
                              "project": {"key": "BOARD"}, "issuelinks": [], "cf_Sep": 0},
                [_row("A-1", "Dev One", "A1", 2.0)])
    assert out[0]["initiative"] == ""
    assert out[0]["epics"][0]["delivered"] == 2.0


# ---------------------------------------------------------------- level bands

def _bands(monkeypatch, tiers=None):
    tiers = tiers or {"SDE1": {"sp_efficiency_low": 0.80, "sp_efficiency_high": 0.90},
                      "SDE3": {"sp_efficiency_low": 0.50, "sp_efficiency_high": 0.60}}
    monkeypatch.setattr(ce, "_yaml", lambda f: {"tiers": tiers})
    return re_.tier_bands()


def test_the_band_is_a_range_not_the_midpoint(monkeypatch):
    b = _bands(monkeypatch)["SDE1"]
    assert (b["low"], b["high"], b["mid"]) == (0.80, 0.90, 0.85)


def test_senior_tiers_carry_a_lower_band_on_purpose(monkeypatch):
    b = _bands(monkeypatch)
    assert b["SDE3"]["high"] < b["SDE1"]["low"], \
        "an SDE3 spends about half their time on design and review; a flat bar would " \
        "read that intent as underperformance"


def _graded(monkeypatch, role, net, delivered):
    monkeypatch.setattr(re_, "tier_bands", lambda: {
        "SDE1": {"low": 0.80, "high": 0.90, "mid": 0.85, "notes": ""},
        "SDE3": {"low": 0.50, "high": 0.60, "mid": 0.55, "notes": ""}})
    roster = [{"name": "Dev One", "canonical": "dev-one", "role": role,
               "sp": round(net * 0.85 if role == "SDE1" else net * 0.55, 2),
               "net": net, "leave": 0, "oncall": 0, "wfh": 0, "eff": 0.85}]
    return _people(monkeypatch, [_row("A-1", "Dev One", "A1", delivered)],
                   roster=roster)[0]


def test_delivery_inside_the_band_is_within_level(monkeypatch):
    t = _graded(monkeypatch, "SDE1", 10, 8.5)
    assert t["band"]["low"] == 8.0 and t["band"]["high"] == 9.0
    assert t["vsLevel"] == "within"


def test_delivery_under_the_band_is_below_level(monkeypatch):
    assert _graded(monkeypatch, "SDE1", 10, 4.0)["vsLevel"] == "below"


def test_delivery_over_the_band_is_above_level(monkeypatch):
    assert _graded(monkeypatch, "SDE1", 10, 12.0)["vsLevel"] == "above"


def test_the_same_output_grades_differently_by_level(monkeypatch):
    """6.5 SP over 10 days is below an SDE1's band (8.0-9.0) and above an SDE3's (5.0-6.0).
    Grading both against one flat number is the misread this exists to prevent."""
    assert _graded(monkeypatch, "SDE1", 10, 6.5)["vsLevel"] == "below"
    assert _graded(monkeypatch, "SDE3", 10, 6.5)["vsLevel"] == "above"


def test_the_top_of_the_band_is_within_not_above(monkeypatch):
    """Boundaries are inclusive on both ends: hitting the ceiling is meeting the level, not
    exceeding it, and hitting the floor is meeting it too."""
    assert _graded(monkeypatch, "SDE3", 10, 6.0)["vsLevel"] == "within"
    assert _graded(monkeypatch, "SDE3", 10, 5.0)["vsLevel"] == "within"


def test_no_band_when_the_role_has_no_tier(monkeypatch):
    t = _graded(monkeypatch, "CONTRACTOR", 10, 5.0)
    assert t["band"] is None and t["vsLevel"] is None


def test_the_band_scales_with_days_actually_available(monkeypatch):
    """Half the days, half the expectation — leave must not read as underperformance."""
    full = _graded(monkeypatch, "SDE1", 20, 8.0)
    half = _graded(monkeypatch, "SDE1", 10, 8.0)
    assert (full["band"]["low"], full["band"]["high"]) == (16.0, 18.0)
    assert (half["band"]["low"], half["band"]["high"]) == (8.0, 9.0)
    assert full["vsLevel"] == "below" and half["vsLevel"] == "within"


# ---------------------------------------------------------------- mid-sprint check

def _sprint(monkeypatch, issues, today, people=None, stale_days=3):
    """sprint_check over a stubbed board. The sprint runs Mon 5 Oct -> Fri 16 Oct 2026."""
    people = people or [{"name": "Dev One", "canonical": "dev-one", "role": "SDE1",
                         "sp": 0, "net": 0, "leave": 0, "oncall": 0, "wfh": 0, "eff": 0.85}]

    def cap(y, m, window=None):
        a, b = window
        nd = len([d for d in [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]
                  if d.weekday() < 5])
        return {"people": [dict(p, net=nd, sp=round(nd * 0.85, 2)) for p in people],
                "teamSP": 0, "teamNetDays": nd, "days": []}

    monkeypatch.setattr(ce, "month_capacity", cap)
    monkeypatch.setattr(ce, "holidays_for", lambda y, d: {})
    monkeypatch.setattr(ce, "_yaml", lambda f: {"people": [], "tiers": {}})
    monkeypatch.setattr(re_, "tier_bands", lambda: {})
    monkeypatch.setattr(re_, "board_sprints", lambda limit=200: [
        {"id": 1, "name": "S1", "start": "2026-10-05", "end": "2026-10-16", "state": "active"}])
    monkeypatch.setattr(re_, "_search", lambda jql, want, **k: issues)
    return re_.sprint_check([1], ["BOARD"], stale_days=stale_days,
                            today=dt.date.fromisoformat(today))


def _tk(key, sp=1.0, status="In Progress", res=None, moved="2026-10-14",
        created="2026-10-05", resolved="", summary="work", estimated=True):
    return {"key": key, "fields": {
        "summary": summary, ce.SP_FIELD or "customfield_10051": (sp if estimated else None),
        "parent": {"key": "EP-1"},
        "assignee": {"displayName": "Dev One", "accountId": "A1"},
        "status": {"name": status}, "resolution": {"name": res} if res else None,
        "resolutiondate": (resolved + "T10:00:00.000+0530") if resolved else None,
        "created": created + "T10:00:00.000+0530", "updated": moved,
        "statuscategorychangedate": moved + "T10:00:00.000+0530",
        "issuetype": {"name": "Task"}, re_.SPRINT_FIELD: [{"id": 1}]}}


def test_capacity_is_pro_rated_to_the_days_elapsed(monkeypatch):
    """Judging day 3 of 10 against the whole sprint makes everyone read behind until it ends."""
    d3 = _sprint(monkeypatch, [_tk("A-1")], "2026-10-07")
    d8 = _sprint(monkeypatch, [_tk("A-1")], "2026-10-14")
    assert d3["totals"]["capacityToDate"] < d8["totals"]["capacityToDate"]
    assert d3["totals"]["capacityFull"] == d8["totals"]["capacityFull"], \
        "the full-sprint figure must not move as the days pass"


def test_done_respects_the_as_of_date(monkeypatch):
    """A ticket closed on day 9 must not count as delivered when you ask about day 3,
    or every historical read flatters itself and the projection is nonsense."""
    t = _tk("A-1", sp=5.0, status="Done", res="Done", resolved="2026-10-15")
    assert _sprint(monkeypatch, [t], "2026-10-07")["totals"]["doneSP"] == 0
    assert _sprint(monkeypatch, [t], "2026-10-16")["totals"]["doneSP"] == 5.0


def test_projection_scales_the_rate_to_the_whole_sprint(monkeypatch):
    t = _tk("A-1", sp=4.0, status="Done", res="Done", resolved="2026-10-06")
    d = _sprint(monkeypatch, [t], "2026-10-07")     # 3 of 10 working days
    assert d["pace"]["elapsed"] == 3 and d["pace"]["workingDays"] == 10
    assert d["totals"]["projectedSP"] == round(4.0 / 0.3, 2)


def test_a_ticket_not_moved_recently_is_stale(monkeypatch):
    d = _sprint(monkeypatch, [_tk("A-1", moved="2026-10-06")], "2026-10-16")
    assert d["tickets"][0]["stale"] is True


def test_a_ticket_moved_today_is_not_stale(monkeypatch):
    d = _sprint(monkeypatch, [_tk("A-1", moved="2026-10-16")], "2026-10-16")
    assert d["tickets"][0]["stale"] is False


def test_closed_tickets_are_never_stale(monkeypatch):
    """A Done ticket has not moved because it is finished, not because it is neglected."""
    t = _tk("A-1", status="Done", res="Done", resolved="2026-10-06", moved="2026-10-06")
    assert _sprint(monkeypatch, [t], "2026-10-16")["tickets"][0]["stale"] is False


def test_not_started_is_separated_from_in_flight(monkeypatch):
    d = _sprint(monkeypatch, [_tk("A-1", sp=3.0, status="To Do"),
                              _tk("A-2", sp=2.0, status="In Progress")], "2026-10-14")
    assert d["totals"]["notStartedSP"] == 3.0 and d["totals"]["inFlightSP"] == 2.0
    # and per developer, which is the row an EM actually reads
    p = [x for x in d["people"] if x["name"] == "Dev One"][0]
    assert p["notStarted"] == 3.0 and p["inFlight"] == 2.0, \
        "not-started predicts the carry; folding it into in-flight hides what never began"


def test_work_created_after_the_sprint_began_is_flagged(monkeypatch):
    d = _sprint(monkeypatch, [_tk("A-1", created="2026-10-05"),
                              _tk("A-2", created="2026-10-12")], "2026-10-14")
    assert d["totals"]["addedMidSprint"] == 1


def test_an_unestimated_closed_ticket_makes_the_person_unmeasurable(monkeypatch):
    t = _tk("A-1", status="Done", res="Done", resolved="2026-10-06", estimated=False)
    p = [x for x in _sprint(monkeypatch, [t], "2026-10-16")["people"]
         if x["name"] == "Dev One"][0]
    assert p["confidence"] == "low"
    assert any("no estimate" in w for w in p["confidenceWhy"])


def test_two_stale_tickets_make_the_person_unmeasurable(monkeypatch):
    d = _sprint(monkeypatch, [_tk("A-1", moved="2026-10-05"), _tk("A-2", moved="2026-10-05")],
                "2026-10-16")
    p = [x for x in d["people"] if x["name"] == "Dev One"][0]
    assert p["confidence"] == "low" and p["stale"] == 2


def test_a_clean_board_stays_measurable(monkeypatch):
    t = _tk("A-1", sp=4.0, status="Done", res="Done", resolved="2026-10-06", moved="2026-10-06")
    p = [x for x in _sprint(monkeypatch, [t], "2026-10-16")["people"]
         if x["name"] == "Dev One"][0]
    assert p["confidence"] == "ok" and p["confidenceWhy"] == []
    assert p["paceEfficiency"] is not None


def test_mode_says_whether_the_sprint_is_still_running(monkeypatch):
    assert _sprint(monkeypatch, [_tk("A-1")], "2026-10-14")["mode"] == "in-progress"
    assert _sprint(monkeypatch, [_tk("A-1")], "2026-10-20")["mode"] == "closed"


def test_as_of_never_runs_past_the_sprint_end(monkeypatch):
    d = _sprint(monkeypatch, [_tk("A-1")], "2026-11-30")
    assert d["window"]["asOf"] == "2026-10-16"
    assert d["pace"]["remaining"] == 0


# ---------------------------------------------------------------- on-call treatment

def _oncall_case(monkeypatch, today="2026-10-16"):
    """5 of 10 working days on the rota. 3 SP of planned work closed, and a 5 SP on-call
    ticket also closed. The real shape of the row that produced the 213% reading."""
    people = [{"name": "Dev One", "canonical": "dev-one", "role": "SDE2",
               "sp": 0, "net": 0, "leave": 0, "oncall": 0, "wfh": 0, "eff": 0.75}]

    def cap(y, m, window=None):
        a, b = window
        wd = len([d for d in [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]
                  if d.weekday() < 5])
        onc = min(5, wd)                       # on-call for 5 of the elapsed days
        return {"people": [dict(p, net=wd - onc, oncall=onc,
                                sp=round((wd - onc) * 0.75, 2)) for p in people],
                "teamSP": 0, "teamNetDays": wd - onc, "days": []}

    monkeypatch.setattr(ce, "month_capacity", cap)
    monkeypatch.setattr(ce, "holidays_for", lambda y, d: {})
    monkeypatch.setattr(ce, "_yaml", lambda f: {"people": [], "tiers": {}})
    monkeypatch.setattr(re_, "tier_bands", lambda: {
        "SDE2": {"low": 0.70, "high": 0.80, "mid": 0.75, "notes": ""}})
    monkeypatch.setattr(re_, "board_sprints", lambda limit=200: [
        {"id": 1, "name": "S1", "start": "2026-10-05", "end": "2026-10-16", "state": "active"}])
    monkeypatch.setattr(re_, "_search", lambda jql, want, **k: [
        _tk("A-1", sp=3.0, status="Done", res="Done", resolved="2026-10-06", moved="2026-10-06"),
        _tk("A-2", sp=5.0, status="Done", res="Done", resolved="2026-10-06", moved="2026-10-06",
            summary="Oncall"),
        _tk("A-3", sp=2.0, status="In Progress", moved="2026-10-16"),
    ])
    d = re_.sprint_check([1], ["BOARD"], today=dt.date.fromisoformat(today))
    return [p for p in d["people"] if p["name"] == "Dev One"][0]


def test_excluding_oncall_leaves_it_out_of_both_sides(monkeypatch):
    p = _oncall_case(monkeypatch)
    assert p["exOncall"]["days"] == 5, "rota days are out of the denominator"
    assert p["exOncall"]["delivered"] == 3.0, "and the on-call SP is out of the numerator"
    assert p["exOncall"]["capacity"] == 3.75


def test_including_oncall_puts_the_days_back_too(monkeypatch):
    """Counting the on-call TICKET without restoring the on-call DAYS reads 8/3.75 = 213%.
    The include treatment must move both sides or it is the same double-count."""
    p = _oncall_case(monkeypatch)
    inc = p["incOncall"]
    assert inc["days"] == 10 and inc["capacity"] == 7.5
    assert inc["delivered"] == 8.0
    assert inc["pace"] == round(8.0 / 7.5 * 100, 1)


def test_the_two_treatments_can_disagree_on_the_verdict(monkeypatch):
    """They are both defensible and they do not always agree — which is the reason to show
    the choice rather than bake one in."""
    p = _oncall_case(monkeypatch)
    assert p["exOncall"]["vsLevel"] == "below"      # 3.0 against a 3.5-4.0 band
    assert p["incOncall"]["vsLevel"] == "within"    # 8.0 against a 7.0-8.0 band


def test_neither_treatment_can_ever_produce_the_mixed_number(monkeypatch):
    p = _oncall_case(monkeypatch)
    mixed = 8.0 / 3.75 * 100
    assert p["exOncall"]["pace"] != round(mixed, 1)
    assert p["incOncall"]["pace"] != round(mixed, 1)


def test_the_band_scales_with_the_treatment_s_day_count(monkeypatch):
    p = _oncall_case(monkeypatch)
    assert (p["exOncall"]["band"]["low"], p["exOncall"]["band"]["high"]) == (3.5, 4.0)
    assert (p["incOncall"]["band"]["low"], p["incOncall"]["band"]["high"]) == (7.0, 8.0)


def test_if_in_flight_lands_adds_the_open_work(monkeypatch):
    p = _oncall_case(monkeypatch)
    assert p["inFlight"] == 2.0
    assert p["ifLandsEx"]["delivered"] == 5.0        # 3 done + 2 in flight
    assert p["ifLandsInc"]["delivered"] == 10.0      # + the 5 SP on-call ticket


def test_an_in_flight_oncall_ticket_is_not_counted_as_delivered_yet(monkeypatch):
    """It only lands in `delivered` once it is actually Done — the forward view is separate
    from the current one, so 'he will close it on Tuesday' never reads as 'he closed it'."""
    monkeypatch.setattr(ce, "month_capacity", lambda y, m, window=None: {
        "people": [{"name": "Dev One", "canonical": "dev-one", "role": "SDE2",
                    "sp": 3.75, "net": 5, "leave": 0, "oncall": 5, "wfh": 0, "eff": 0.75}],
        "teamSP": 3.75, "teamNetDays": 5, "days": []})
    monkeypatch.setattr(ce, "holidays_for", lambda y, d: {})
    monkeypatch.setattr(ce, "_yaml", lambda f: {"people": [], "tiers": {}})
    monkeypatch.setattr(re_, "tier_bands", lambda: {
        "SDE2": {"low": 0.70, "high": 0.80, "mid": 0.75, "notes": ""}})
    monkeypatch.setattr(re_, "board_sprints", lambda limit=200: [
        {"id": 1, "name": "S1", "start": "2026-10-05", "end": "2026-10-16", "state": "active"}])
    monkeypatch.setattr(re_, "_search", lambda jql, want, **k: [
        _tk("A-2", sp=5.0, status="In Progress", moved="2026-10-16", summary="Oncall")])
    d = re_.sprint_check([1], ["BOARD"], today=dt.date.fromisoformat("2026-10-16"))
    p = [x for x in d["people"] if x["name"] == "Dev One"][0]
    assert p["oncallSP"] == 0 and p["inFlightOncall"] == 5.0
    assert p["incOncall"]["delivered"] == 0
    assert p["ifLandsInc"]["delivered"] == 5.0


# ---------------------------------------------------------------- manual sprint board

def test_a_saved_plan_round_trips(tmp_path):
    f = str(tmp_path / "board.json")
    r = re_.save_board_plan([{"id": "t1", "key": "A-1", "title": "x", "who": "dev-one",
                              "date": "2026-12-02", "off": 0.5, "days": 3, "sp": 3}], path=f)
    assert r["ok"] and r["tasks"] == 1
    t = re_.board_plan(path=f)["tasks"][0]
    assert (t["date"], t["off"], t["days"], t["who"], t["key"]) == \
           ("2026-12-02", 0.5, 3, "dev-one", "A-1")


def test_there_is_one_plan_not_one_per_sprint(tmp_path):
    """The backlog is what is on the manager's mind; it does not change because a different
    sprint is on screen. And scheduled work is pinned to a date, so a window that overlaps
    days already planned shows that work instead of looking empty."""
    f = str(tmp_path / "board.json")
    re_.save_board_plan([{"id": "a", "title": "scheduled", "who": "dev-one",
                          "date": "2026-12-02", "days": 1},
                         {"id": "b", "title": "an idea", "who": "", "cat": "TBM", "days": 1}],
                        cats=["TBM"], path=f)
    got = re_.board_plan(path=f)
    assert [t["id"] for t in got["tasks"]] == ["a", "b"]
    assert got["cats"] == ["TBM"]


def test_a_bad_date_is_refused_rather_than_coerced(tmp_path):
    """Silently dropping a bad date would move someone's task to the backlog without
    saying so."""
    f = str(tmp_path / "board.json")
    assert "__error__" in re_.save_board_plan(
        [{"id": "t", "title": "t", "who": "dev-one", "date": "last tuesday"}], path=f)


def test_half_a_schedule_is_not_a_schedule(tmp_path):
    """A person with no day, or a day with no person, is not a plan - it goes back to the
    backlog rather than being drawn somewhere arbitrary."""
    f = str(tmp_path / "board.json")
    re_.save_board_plan([{"id": "a", "title": "a", "who": "dev-one", "date": "", "days": 1},
                         {"id": "b", "title": "b", "who": "", "date": "2026-12-02",
                          "off": 0.5, "days": 1}], path=f)
    got = {t["id"]: t for t in re_.board_plan(path=f)["tasks"]}
    assert (got["a"]["who"], got["a"]["date"]) == ("", "")
    assert (got["b"]["who"], got["b"]["date"], got["b"]["off"]) == ("", "", 0.0)


def test_duration_is_clamped_to_something_drawable(tmp_path):
    f = str(tmp_path / "board.json")
    re_.save_board_plan([{"id": "t", "title": "t", "days": 0},
                         {"id": "u", "title": "u", "days": 9999}], path=f)
    got = {t["id"]: t["days"] for t in re_.board_plan(path=f)["tasks"]}
    assert got["t"] == 0.25, "a zero-length bar cannot be grabbed to fix"
    assert got["u"] == 60


def test_the_board_plans_to_the_quarter_day(tmp_path):
    """Half a day of review is a real plan; a whole day of it is a lie. Lengths and the
    offset into a day are stored as whole quarters, so a hand-edited file lands on the same
    grid the bars are drawn on."""
    f = str(tmp_path / "board.json")
    re_.save_board_plan([{"id": "a", "title": "a", "who": "d", "date": "2026-12-02",
                          "off": 0.5, "days": 0.25},
                         {"id": "b", "title": "b", "who": "d", "date": "2026-12-02",
                          "off": 0.4, "days": 0.3},
                         {"id": "c", "title": "c", "who": "d", "date": "2026-12-02",
                          "off": "0.75", "days": "1.5"},
                         {"id": "d", "title": "d", "who": "d", "date": "2026-12-02",
                          "off": 3, "days": 1}], path=f)
    got = {t["id"]: (t["off"], t["days"]) for t in re_.board_plan(path=f)["tasks"]}
    assert got["a"] == (0.5, 0.25)
    assert got["b"] == (0.5, 0.25), "off-grid values are snapped, not kept"
    assert got["c"] == (0.75, 1.5), "numbers arriving as strings still land on the grid"
    assert got["d"] == (0.75, 1), "an offset cannot run past the end of its own day"


def test_a_length_that_is_not_a_number_is_refused(tmp_path):
    f = str(tmp_path / "board.json")
    assert "__error__" in re_.save_board_plan(
        [{"id": "t", "title": "t", "days": "soon"}], path=f)


def test_a_task_with_no_id_is_dropped(tmp_path):
    f = str(tmp_path / "board.json")
    re_.save_board_plan([{"title": "no id", "days": 1},
                         {"id": "ok", "title": "ok", "days": 1}], path=f)
    assert [t["id"] for t in re_.board_plan(path=f)["tasks"]] == ["ok"]


def test_a_nameless_task_is_dropped(tmp_path):
    """A bar with no name is a blank rectangle nobody can identify a week later, and the
    board refuses to create one. A ticket-backed bar is named by its key, so it stays."""
    f = str(tmp_path / "board.json")
    re_.save_board_plan([{"id": "blank", "title": "   ", "days": 1},
                         {"id": "keyed", "title": "", "key": "A-9", "days": 1},
                         {"id": "named", "title": "real", "days": 1}], path=f)
    assert [t["id"] for t in re_.board_plan(path=f)["tasks"]] == ["keyed", "named"]


def test_a_tasks_colour_survives_a_round_trip(tmp_path):
    """The bar's tint is the planner's own shorthand for which task is which, so it has to
    come back the same tomorrow. Junk is stored as 0, meaning 'no choice made'."""
    f = str(tmp_path / "board.json")
    re_.save_board_plan([{"id": "a", "title": "a", "days": 1, "tint": 4},
                         {"id": "b", "title": "b", "days": 1, "tint": 99},
                         {"id": "c", "title": "c", "days": 1, "tint": "x"},
                         {"id": "d", "title": "d", "days": 1}], path=f)
    got = {t["id"]: t["tint"] for t in re_.board_plan(path=f)["tasks"]}
    assert got == {"a": 4, "b": 0, "c": 0, "d": 0}


def test_backlog_work_survives_a_round_trip(tmp_path):
    """A task nobody owns is backlog, filed under the heading the planner typed."""
    f = str(tmp_path / "board.json")
    re_.save_board_plan([{"id": "b", "title": "rollback strategy", "cat": "TBM",
                          "who": "", "days": 2},
                         {"id": "a", "title": "active", "cat": "CASA", "who": "dev-one",
                          "date": "2026-12-02", "days": 1}], cats=["TBM"], path=f)
    got = {t["id"]: t for t in re_.board_plan(path=f)["tasks"]}
    assert got["b"]["who"] == "", "an unowned task stays unowned"
    assert (got["b"]["cat"], got["b"]["days"]) == ("TBM", 2)
    assert got["a"]["cat"] == "CASA", "a scheduled task keeps its heading too"


def test_a_category_cannot_grow_without_bound(tmp_path):
    f = str(tmp_path / "board.json")
    re_.save_board_plan([{"id": "a", "title": "a", "cat": "x" * 200, "days": 1}], path=f)
    assert len(re_.board_plan(path=f)["tasks"][0]["cat"]) == 60


def test_headings_are_kept_even_with_nothing_under_them(tmp_path):
    """A heading made today is still there tomorrow with nothing in it yet - which is the
    point of making it before the work exists."""
    f = str(tmp_path / "board.json")
    re_.save_board_plan([], cats=["TBM", "tbm", "  ", "CASA"], path=f)
    assert re_.board_plan(path=f)["cats"] == ["TBM", "CASA"]


def test_saving_an_empty_list_clears_the_plan(tmp_path):
    f = str(tmp_path / "board.json")
    re_.save_board_plan([{"id": "t", "title": "t", "days": 1}], path=f)
    re_.save_board_plan([], path=f)
    assert re_.board_plan(path=f)["tasks"] == []


def test_a_missing_store_reads_as_an_empty_plan(tmp_path):
    assert re_.board_plan(path=str(tmp_path / "nope.json"))["tasks"] == []


def test_an_old_keyed_plan_is_converted_to_dates(tmp_path):
    """Plans saved before this carried a day INDEX against one sprint or window, so the
    same work was invisible from every other selection. The dates are known on the first
    load of that selection, so the conversion happens there, once."""
    f = str(tmp_path / "board.json")
    with open(f, "w") as fh:
        json.dump({"w:2026-12-01..2026-12-03": {
            "tasks": [{"id": "a", "title": "scheduled", "who": "dev-one", "start": 1.5,
                       "days": 2, "cat": "TBM"},
                      {"id": "b", "title": "an idea", "who": "", "start": 0, "days": 1}],
            "cats": ["TBM", "CASA"]}}, fh)
    with open(f) as fh:
        doc = json.load(fh)
    assert re_._migrate_keyed(doc, "w:2026-12-01..2026-12-03",
                              ["2026-12-01", "2026-12-02", "2026-12-03"]) is True
    got = {t["id"]: t for t in doc["board"]["tasks"]}
    assert (got["a"]["date"], got["a"]["off"]) == ("2026-12-02", 0.5)
    assert "start" not in got["a"]
    assert (got["b"]["who"], got["b"]["date"]) == ("", ""), "unowned work becomes backlog"
    assert doc["board"]["cats"] == ["TBM", "CASA"]
    assert "w:2026-12-01..2026-12-03" not in doc, "the old key is not left behind to re-apply"


def test_an_index_with_no_day_behind_it_becomes_backlog(tmp_path):
    """Converting must not invent a date. Work that cannot be placed is kept as backlog
    rather than dropped - it was written down for a reason."""
    doc = {"k": {"tasks": [{"id": "a", "title": "a", "who": "dev-one", "start": 99,
                            "days": 1}]}}
    assert re_._migrate_keyed(doc, "k", ["2026-12-01"]) is True
    t = doc["board"]["tasks"][0]
    assert (t["who"], t["date"]) == ("", "")


def test_migration_does_not_run_twice(tmp_path):
    doc = {"k": {"tasks": [{"id": "a", "title": "a", "days": 1}]}}
    assert re_._migrate_keyed(doc, "k", ["2026-12-01"]) is True
    assert re_._migrate_keyed(doc, "k", ["2026-12-01"]) is False
    assert len(doc["board"]["tasks"]) == 1


def test_tasks_must_be_a_list():
    assert "__error__" in re_.save_board_plan("nope")
    assert "__error__" in re_.save_board_plan([], cats="nope")


# ---- push decisions survive leaving the page -----------------------------------------

def _board_with(tmp_path, tasks):
    p = tmp_path / "board.json"
    re_.save_board_plan(tasks, cats=[], path=str(p))
    return str(p)


def test_a_push_decision_is_stored_on_the_tile(tmp_path):
    p = _board_with(tmp_path, [{"id": "a", "title": "t", "who": "ann",
                                "date": "2026-01-05", "days": 1}])
    r = re_.save_push_decisions(
        {"a": {"key": "team-1", "nopush": True, "pts": 5}}, path=p)
    assert r["ok"] and r["saved"] == 1
    t = re_.board_plan(path=p)["tasks"][0]
    assert t["key"] == "TEAM-1" and t["nopush"] is True and t["pts"] == 5


def test_an_empty_value_clears_the_decision_so_a_wrong_guess_is_undoable(tmp_path):
    p = _board_with(tmp_path, [{"id": "a", "title": "t", "who": "ann",
                                "date": "2026-01-05", "days": 1}])
    re_.save_push_decisions({"a": {"key": "TEAM-1", "nopush": True, "pts": 5}}, path=p)
    re_.save_push_decisions({"a": {"key": "", "nopush": False, "pts": 0}}, path=p)
    t = re_.board_plan(path=p)["tasks"][0]
    assert "key" not in t and "nopush" not in t and "pts" not in t


def test_a_field_left_out_is_left_alone(tmp_path):
    """Typing a point value must not quietly unlink the ticket."""
    p = _board_with(tmp_path, [{"id": "a", "title": "t", "who": "ann",
                                "date": "2026-01-05", "days": 1}])
    re_.save_push_decisions({"a": {"key": "TEAM-1"}}, path=p)
    re_.save_push_decisions({"a": {"pts": 3}}, path=p)
    t = re_.board_plan(path=p)["tasks"][0]
    assert t["key"] == "TEAM-1" and t["pts"] == 3


def test_a_decision_for_a_tile_that_is_gone_is_reported_not_invented(tmp_path):
    p = _board_with(tmp_path, [{"id": "a", "title": "t", "who": "ann",
                                "date": "2026-01-05", "days": 1}])
    r = re_.save_push_decisions({"ghost": {"key": "TEAM-1"}}, path=p)
    assert r["unknown"] == ["ghost"] and r["saved"] == 0
    assert len(re_.board_plan(path=p)["tasks"]) == 1


def test_renaming_a_tile_from_the_push_view_renames_it_on_the_board(tmp_path):
    """One name. A tile and the ticket it becomes must not drift apart."""
    p = _board_with(tmp_path, [{"id": "a", "title": "Oncall Rpt", "who": "ann",
                                "date": "2026-01-05", "days": 1}])
    re_.save_push_decisions({"a": {"title": "Oncall report for October"}}, path=p)
    assert re_.board_plan(path=p)["tasks"][0]["title"] == "Oncall report for October"


def test_a_tile_cannot_be_renamed_to_nothing(tmp_path):
    """Every other field clears on empty. A nameless tile is not an improvement, and
    plan_push would skip it as having no title."""
    p = _board_with(tmp_path, [{"id": "a", "title": "Keep me", "who": "ann",
                                "date": "2026-01-05", "days": 1}])
    re_.save_push_decisions({"a": {"title": "   "}}, path=p)
    assert re_.board_plan(path=p)["tasks"][0]["title"] == "Keep me"


def test_a_decision_about_a_derived_tile_is_kept_even_though_it_has_no_board_row(tmp_path):
    """Oncall tiles are read out of the capacity calendar, so there is no task to hang
    the decision on, but the id is stable and the decision has to survive."""
    p = _board_with(tmp_path, [{"id": "a", "title": "t", "who": "ann",
                                "date": "2026-01-05", "days": 1}])
    r = re_.save_push_decisions({"oncall:ann:2026-01-05": {"pts": 3}}, path=p)
    assert r["ok"] and r["unknown"] == []
    assert re_.push_decisions(path=p)["oncall:ann:2026-01-05"]["pts"] == 3


def test_a_plain_unknown_id_is_still_reported_rather_than_silently_kept(tmp_path):
    """Only derived ids, which carry a colon, get the side map. A typo must not become
    a decision nobody can see."""
    p = _board_with(tmp_path, [{"id": "a", "title": "t", "who": "ann",
                                "date": "2026-01-05", "days": 1}])
    r = re_.save_push_decisions({"typo": {"pts": 3}}, path=p)
    assert r["unknown"] == ["typo"] and re_.push_decisions(path=p) == {}


def test_the_picker_and_the_push_ask_for_different_sprint_states():
    """The picker wants sprints with something in them; a push wants the one that has
    not started yet. Sharing one list hid the sprint the push was about to write into."""
    assert "future" not in re_.PICKER_STATES
    assert "future" in re_.ALL_STATES


def test_a_created_key_lands_even_for_a_tile_with_no_board_row(tmp_path):
    """Oncall tiles are derived, so set_task_key found no task and dropped the key on the
    floor. The ticket then gets created again next run, and only the adoption guard
    stands between that and a duplicate."""
    p = _board_with(tmp_path, [{"id": "a", "title": "t", "who": "ann",
                                "date": "2026-01-05", "days": 1}])
    r = re_.set_task_key("oncall:ann:2026-01-05", "team-99", path=p)
    assert r["ok"]
    assert re_.push_decisions(path=p)["oncall:ann:2026-01-05"]["key"] == "TEAM-99"


def test_a_key_for_a_tile_that_is_simply_not_there_is_still_an_error(tmp_path):
    p = _board_with(tmp_path, [{"id": "a", "title": "t", "who": "ann",
                                "date": "2026-01-05", "days": 1}])
    assert "__error__" in re_.set_task_key("ghost", "TEAM-1", path=p)


def test_everyone_on_the_board_carries_the_account_that_assigns_their_work():
    """Fifteen real tickets were created with no owner because this field was dropped
    between the roster and the board."""
    for p in ce.roster():
        assert "jira_id" in p

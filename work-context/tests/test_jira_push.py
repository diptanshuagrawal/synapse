"""Turning the sprint board into a Jira sprint.

The decision layer is pure, so everything that matters is tested here without a Jira:
what it would create, what it would adopt rather than duplicate, what it refuses and why.
The write layer is tested through a recorder, so the exact payloads AND their order are
pinned without sending anything.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "derive"))
import jira_push as jp


WINDOW = ["2026-11-04", "2026-11-17"]
PEOPLE = [{"canonical": "dev-one", "name": "Dev One", "jira_id": "acc-1", "eff": 0.5},
          {"canonical": "dev-two", "name": "Dev Two", "jira_id": "acc-2", "eff": 1.0}]


def task(**kw):
    base = {"id": "t1", "title": "Rollback strategy", "who": "dev-one",
            "date": "2026-11-04", "off": 0, "days": 2, "sp": 0, "cat": "TBM", "key": ""}
    base.update(kw)
    return base


def snapshot(**kw):
    base = {"epics": [{"key": "TEAM-10", "summary": "Transactions"},
                      {"key": "TEAM-20", "summary": "BAU"}],
            "sprints": [], "issues": {}, "byEpic": {}}
    base.update(kw)
    return base


def plan(tasks, **kw):
    base = {"tasks": tasks, "epics": {"TBM": "TEAM-10"}, "effort": False}
    base.update(kw)
    return base


# ---------------------------------------------------------------- what it would do

def test_a_scheduled_task_with_no_ticket_is_created():
    p = jp.plan_push(plan([task()]), PEOPLE, WINDOW, snapshot())
    row = p["rows"][0]
    assert row["action"] == jp.CREATE
    assert row["epic"] == "TEAM-10"
    assert row["assignee"] == "Dev One"
    assert row["accountId"] == "acc-1"


def test_backlog_tiles_are_not_commitments():
    """A tile nobody owns, or one not on a day, is an idea. It does not become a ticket."""
    p = jp.plan_push(plan([task(id="a", who=""), task(id="b", date="")]),
                     PEOPLE, WINDOW, snapshot())
    assert p["rows"] == []


def test_the_sprint_is_named_from_the_window():
    p = jp.plan_push(plan([task()]), PEOPLE, WINDOW, snapshot())
    assert p["sprint"]["name"] == "2026-11-04 to 2026-11-17"
    assert p["sprint"]["exists"] is False


def test_an_existing_sprint_of_that_name_is_reused_not_remade():
    s = snapshot(sprints=[{"id": 77, "name": "2026-11-04 to 2026-11-17", "state": "future"}])
    p = jp.plan_push(plan([task()]), PEOPLE, WINDOW, s)
    assert p["sprint"]["exists"] is True and p["sprint"]["id"] == 77


def test_a_sprint_name_matches_loosely_enough_to_be_useful():
    s = snapshot(sprints=[{"id": 9, "name": "  2026-11-04 TO 2026-11-17 "}])
    assert jp.plan_push(plan([task()]), PEOPLE, WINDOW, s)["sprint"]["id"] == 9


# ---------------------------------------------------------------- epics

def test_a_heading_with_no_epic_stops_the_push():
    p = jp.plan_push(plan([task(cat="CASA")]), PEOPLE, WINDOW, snapshot())
    assert p["rows"][0]["action"] == jp.SKIP
    assert p["unmapped"] == ["CASA"]
    assert p["ready"] is False, "nothing is pushed while a heading has nowhere to go"


def test_a_task_can_override_its_heading():
    p = jp.plan_push(plan([task(cat="CASA", epic="TEAM-20")]), PEOPLE, WINDOW, snapshot())
    assert p["rows"][0]["epic"] == "TEAM-20" and p["rows"][0]["action"] == jp.CREATE


def test_a_fallback_catches_what_is_unmapped():
    p = jp.plan_push(plan([task(cat="CASA")]), PEOPLE, WINDOW, snapshot(),
                     {"fallbackEpic": "TEAM-20"})
    assert p["rows"][0]["epic"] == "TEAM-20" and not p["unmapped"]


def test_an_epic_that_is_not_an_epic_is_refused():
    p = jp.plan_push(plan([task(epic="TEAM-999")]), PEOPLE, WINDOW, snapshot())
    assert p["rows"][0]["action"] == jp.SKIP
    assert "not an epic" in p["rows"][0]["why"]


def test_heading_matching_ignores_case_and_spacing():
    p = jp.plan_push(plan([task(cat=" tbm ")], epics={"TBM": "TEAM-10"}),
                     PEOPLE, WINDOW, snapshot())
    assert p["rows"][0]["epic"] == "TEAM-10"


def test_a_tile_with_no_heading_is_a_group_of_its_own():
    """Tiles typed straight onto the board carry no heading. That is a real group, not a
    mistake, and pointing it at one epic beats correcting every row."""
    p = jp.plan_push(plan([task(cat="")]), PEOPLE, WINDOW, snapshot())
    assert p["unmapped"] == [jp.NO_HEADING]

    mapped = jp.plan_push(plan([task(cat="")], epics={"": "TEAM-20"}),
                          PEOPLE, WINDOW, snapshot())
    assert mapped["rows"][0]["epic"] == "TEAM-20" and not mapped["unmapped"]


# ---------------------------------------------------------------- running it twice

def test_a_ticket_of_the_same_name_in_that_epic_is_adopted_not_duplicated():
    """What a failed write-back leaves behind. Creating again would quietly double it."""
    s = snapshot(byEpic={"TEAM-10": [{"key": "TEAM-55", "summary": "rollback  STRATEGY"}]})
    row = jp.plan_push(plan([task()]), PEOPLE, WINDOW, s)["rows"][0]
    assert row["action"] == jp.ADOPT and row["key"] == "TEAM-55"


def test_a_same_name_ticket_in_a_DIFFERENT_epic_is_not_adopted():
    s = snapshot(byEpic={"TEAM-20": [{"key": "TEAM-55", "summary": "Rollback strategy"}]})
    assert jp.plan_push(plan([task()]), PEOPLE, WINDOW, s)["rows"][0]["action"] == jp.CREATE


def test_two_tiles_of_the_same_name_under_one_epic_are_called_out():
    """Both would be created on the first run and both would adopt the same ticket on the
    second. Better to say so than to do either."""
    p = jp.plan_push(plan([task(id="a"), task(id="b")]), PEOPLE, WINDOW, snapshot())
    assert p["rows"][0]["action"] == jp.CREATE
    assert p["rows"][1]["action"] == jp.SKIP
    assert "same name" in p["rows"][1]["why"]


# ---------------------------------------------------------------- existing tickets

def test_a_task_that_carries_a_key_is_updated():
    s = snapshot(issues={"TEAM-5": {"summary": "Rollback strategy", "epic": "TEAM-20",
                                    "assignee": "", "sp": 0, "sprintIds": []}})
    row = jp.plan_push(plan([task(key="TEAM-5")]), PEOPLE, WINDOW, s)["rows"][0]
    assert row["action"] == jp.UPDATE
    what = [c[0] for c in row["changes"]]
    assert "epic" in what and "assignee" in what and "sprint" in what


def test_a_ticket_already_as_planned_is_left_alone():
    s = snapshot(sprints=[{"id": 77, "name": "2026-11-04 to 2026-11-17"}],
                 issues={"TEAM-5": {"summary": "Rollback strategy", "epic": "TEAM-10",
                                    "assignee": "acc-1", "sp": 1.0, "sprintIds": [77]}})
    row = jp.plan_push(plan([task(key="TEAM-5")]), PEOPLE, WINDOW, s)["rows"][0]
    assert row["action"] == jp.SKIP and row["why"] == "already as planned"


def test_someone_elses_estimate_is_reported_not_taken():
    s = snapshot(issues={"TEAM-5": {"summary": "x", "epic": "TEAM-10",
                                    "assignee": "acc-1", "sp": 8, "sprintIds": []}})
    row = jp.plan_push(plan([task(key="TEAM-5")]), PEOPLE, WINDOW, s)["rows"][0]
    assert not any(c[0] == "points" for c in row["changes"])
    assert any("8" in w for w in row["warn"])


def test_an_estimate_is_overwritten_only_when_separately_accepted():
    s = snapshot(issues={"TEAM-5": {"summary": "x", "epic": "TEAM-10",
                                    "assignee": "acc-1", "sp": 8, "sprintIds": []}})
    row = jp.plan_push(plan([task(key="TEAM-5")]), PEOPLE, WINDOW, s,
                       {"overwritePoints": True})["rows"][0]
    assert any(c[0] == "points" for c in row["changes"])


def test_a_key_jira_has_never_heard_of_is_refused():
    row = jp.plan_push(plan([task(key="TEAM-404")]), PEOPLE, WINDOW, snapshot())["rows"][0]
    assert row["action"] == jp.SKIP and "not found" in row["why"]


# ---------------------------------------------------------------- estimates

def test_in_days_mode_the_days_go_across_untouched():
    """This used to convert through efficiency, which disagreed with the board: the
    Planned column in days mode shows the tile's length as typed. Converting here meant
    the plan approved on the board and the plan pushed to Jira were different plans."""
    row = jp.plan_push(plan([task(days=2, sp=0)]), PEOPLE, WINDOW, snapshot())["rows"][0]
    assert row["sp"] == 2.0


def test_in_effort_mode_the_number_on_the_tile_is_the_estimate():
    row = jp.plan_push(plan([task(days=4, sp=2)], effort=True),
                       PEOPLE, WINDOW, snapshot())["rows"][0]
    assert row["sp"] == 2


def test_a_task_with_no_estimate_is_flagged_but_not_blocked():
    row = jp.plan_push(plan([task(days=0, sp=0)], effort=True),
                       PEOPLE, WINDOW, snapshot())["rows"][0]
    assert row["action"] == jp.CREATE and "no estimate" in row["warn"]


def test_a_person_with_no_jira_account_is_flagged_not_dropped():
    ppl = [{"canonical": "dev-one", "name": "Dev One", "eff": 1}]
    row = jp.plan_push(plan([task()]), ppl, WINDOW, snapshot())["rows"][0]
    assert row["action"] == jp.CREATE and not row["accountId"]
    assert any("unassigned" in w for w in row["warn"])


# ---------------------------------------------------------------- the write layer

class Recorder:
    def __init__(self, replies=None, fail_on=None):
        self.calls = []
        self.replies = replies or {}
        self.fail_on = fail_on or ()

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path in self.fail_on:
            raise RuntimeError("jira said no")
        if path == "/rest/agile/1.0/sprint":
            return {"id": 91}
        if path == "/rest/api/3/issue":
            n = sum(1 for c in self.calls if c[1] == "/rest/api/3/issue")
            return {"key": f"TEAM-{100 + n}"}
        return {}


FIELDS = {"story_points": "customfield_10051", "issuetype_id": "10001"}


def test_nothing_is_sent_without_an_explicit_confirmation():
    p = jp.plan_push(plan([task()]), PEOPLE, WINDOW, snapshot())
    rec = Recorder()
    assert "__error__" in jp.apply_push(p, rec, 349, "TEAM", FIELDS)
    assert rec.calls == [], "not one call"


def test_a_push_is_refused_while_a_heading_has_no_epic():
    p = jp.plan_push(plan([task(cat="CASA")]), PEOPLE, WINDOW, snapshot())
    rec = Recorder()
    assert "__error__" in jp.apply_push(p, rec, 349, "TEAM", FIELDS, confirm=True)
    assert rec.calls == []


def test_the_sprint_is_made_first_then_the_tickets_then_the_move():
    p = jp.plan_push(plan([task()]), PEOPLE, WINDOW, snapshot())
    rec = Recorder()
    out = jp.apply_push(p, rec, 349, "TEAM", FIELDS, confirm=True)
    assert [c[1] for c in rec.calls] == ["/rest/agile/1.0/sprint", "/rest/api/3/issue",
                                         "/rest/agile/1.0/sprint/91/issue"]
    assert out["ok"] is True and out["created"][0]["key"] == "TEAM-101"


def test_the_created_ticket_carries_what_the_preview_promised():
    p = jp.plan_push(plan([task(sp=3)], effort=True), PEOPLE, WINDOW, snapshot())
    rec = Recorder()
    jp.apply_push(p, rec, 349, "TEAM", FIELDS, confirm=True)
    body = [c[2] for c in rec.calls if c[1] == "/rest/api/3/issue"][0]["fields"]
    assert body["project"] == {"key": "TEAM"}
    assert body["parent"] == {"key": "TEAM-10"}
    assert body["summary"] == "Rollback strategy"
    assert body["assignee"] == {"accountId": "acc-1"}
    assert body["customfield_10051"] == 3


def test_a_new_key_is_written_back_before_anything_else_is_tried():
    """A key that only exists in a response is a duplicate waiting for the next run."""
    p = jp.plan_push(plan([task()]), PEOPLE, WINDOW, snapshot())
    seen = []
    rec = Recorder()
    jp.apply_push(p, rec, 349, "TEAM", FIELDS, confirm=True,
                  on_key=lambda tid, key: seen.append((tid, key, len(rec.calls))))
    assert seen == [("t1", "TEAM-101", 2)], "written back on the call that created it"


def test_an_existing_sprint_is_not_created_again():
    s = snapshot(sprints=[{"id": 77, "name": "2026-11-04 to 2026-11-17"}])
    p = jp.plan_push(plan([task()]), PEOPLE, WINDOW, s)
    rec = Recorder()
    jp.apply_push(p, rec, 349, "TEAM", FIELDS, confirm=True)
    assert "/rest/agile/1.0/sprint" not in [c[1] for c in rec.calls]
    assert "/rest/agile/1.0/sprint/77/issue" in [c[1] for c in rec.calls]


def test_an_adopted_ticket_is_moved_but_not_remade():
    s = snapshot(byEpic={"TEAM-10": [{"key": "TEAM-55", "summary": "Rollback strategy"}]})
    p = jp.plan_push(plan([task()]), PEOPLE, WINDOW, s)
    rec = Recorder()
    out = jp.apply_push(p, rec, 349, "TEAM", FIELDS, confirm=True)
    assert "/rest/api/3/issue" not in [c[1] for c in rec.calls]
    assert out["adopted"][0]["key"] == "TEAM-55"
    assert rec.calls[-1][2] == {"issues": ["TEAM-55"]}


def test_one_failure_does_not_stop_the_rest():
    p = jp.plan_push(plan([task(id="a"), task(id="b", title="Second")]),
                     PEOPLE, WINDOW, snapshot())
    rec = Recorder(fail_on=("/rest/api/3/issue",))
    out = jp.apply_push(p, rec, 349, "TEAM", FIELDS, confirm=True)
    assert len(out["failed"]) == 2 and out["ok"] is False
    assert out["sprint"]["id"] == 91, "the sprint still exists, and the receipt says so"


def test_a_sprint_that_cannot_be_made_stops_before_any_ticket_is():
    p = jp.plan_push(plan([task()]), PEOPLE, WINDOW, snapshot())
    rec = Recorder(fail_on=("/rest/agile/1.0/sprint",))
    out = jp.apply_push(p, rec, 349, "TEAM", FIELDS, confirm=True)
    assert "__error__" in out
    assert "/rest/api/3/issue" not in [c[1] for c in rec.calls]


def test_tickets_are_moved_in_batches_rather_than_one_by_one():
    tasks = [task(id=f"t{i}", title=f"Task {i}") for i in range(120)]
    p = jp.plan_push(plan(tasks), PEOPLE, WINDOW, snapshot())
    rec = Recorder()
    jp.apply_push(p, rec, 349, "TEAM", FIELDS, confirm=True)
    moves = [c for c in rec.calls if c[1].endswith("/issue") and c[1].startswith("/rest/agile")]
    assert len(moves) == 3
    assert [len(m[2]["issues"]) for m in moves] == [50, 50, 20]


def test_a_stale_preview_is_refused():
    assert "__error__" in jp.apply_push({"v": 0}, Recorder(), 349, "TEAM", FIELDS, confirm=True)


def test_a_resolved_epic_is_not_reported_as_the_tasks_own_override():
    """The bug this guards: the page offered the RESOLVED epic back in the per-task
    override box, so saving the mapping a second time froze it onto every task and
    re-mapping the heading silently stopped working."""
    plan = {"epics": {"BAU": "P-1"},
            "tasks": [{"id": "a", "title": "from heading", "who": "ann",
                       "date": "2026-01-05", "days": 1, "cat": "BAU"},
                      {"id": "b", "title": "overridden", "who": "ann",
                       "date": "2026-01-05", "days": 1, "cat": "BAU", "epic": "P-2"}]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": 1}]
    jira = {"epics": [{"key": "P-1"}, {"key": "P-2"}], "sprints": [], "issues": {}, "byEpic": {}}
    rows = {r["id"]: r for r in jp.plan_push(plan, people, ["2026-01-05", "2026-01-09"], jira)["rows"]}

    # Resolution happened, and it is reported.
    assert rows["a"]["epic"] == "P-1"
    # But the task itself overrides nothing, and must not claim to.
    assert rows["a"]["ownEpic"] == ""
    # A real override is reported as both.
    assert rows["b"]["epic"] == "P-2"
    assert rows["b"]["ownEpic"] == "P-2"


def test_the_override_box_is_bound_to_the_override_not_the_resolution():
    """The page half of the same bug. Asserted at the source because the distinction is
    one attribute and nothing else in the suite would notice it flipping back."""
    page = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "derived", "jira-push.html")
    with open(page, encoding="utf-8") as fh:
        src = fh.read()
    row_input = re.search(r'<input class="ep" list="epicList" data-task=.*?>', src, re.S)
    assert row_input, "the per-ticket epic input is gone"
    box = row_input.group(0)
    assert "value=\"${esc(r.ownEpic" in box, "the box must hold the override"
    assert "value=\"${esc(r.epic" not in box, "the box must never hold the resolution"


# ---- suggesting an epic for a tile ---------------------------------------------------
# The weights are /resolve-initiatives', deliberately: this project already decided how
# free text finds an epic, and a second answer would be the one nobody trusts.

def test_an_epic_whose_title_matches_outvotes_a_single_ticket():
    out = jp.rank_epics([{"key": "E-1", "summary": "Rollback", "status": "In Progress"}],
                        [{"parent": "E-2", "summary": "x", "parentSummary": "Other",
                          "status": "To Do"}])
    assert [c["epic"] for c in out] == ["E-1", "E-2"]
    assert out[0]["votes"] == 2 and out[1]["votes"] == 1


def test_several_tickets_under_one_epic_outvote_a_loose_title_match():
    """The case that makes this useful: a tile named after the work, not after the epic."""
    out = jp.rank_epics(
        [{"key": "E-1", "summary": "Migration", "status": "In Progress"}],
        [{"parent": "E-2", "summary": "a", "parentSummary": "Real home", "status": "To Do"},
         {"parent": "E-2", "summary": "b", "parentSummary": "Real home", "status": "To Do"},
         {"parent": "E-2", "summary": "c", "parentSummary": "Real home", "status": "To Do"}])
    assert out[0]["epic"] == "E-2" and out[0]["votes"] == 3


def test_closed_work_does_not_vote():
    """Done tickets are evidence about last cycle, not about where this tile belongs."""
    out = jp.rank_epics([{"key": "E-1", "summary": "Rollback", "status": "Done"}],
                        [{"parent": "E-2", "summary": "x", "parentSummary": "o",
                          "status": "Cancelled"}])
    assert out == []


def test_a_tile_matches_an_epic_by_name_even_when_jira_text_search_misses_it():
    """Jira's `summary ~` wants every word; a board tile is terse. Local matching is the
    half that catches 'TXN Guardrails' against 'Transaction Safety Guardrails'."""
    epics = [{"key": "E-1", "summary": "Transaction Safety Guardrails", "status": "Open"},
             {"key": "E-2", "summary": "Completely unrelated billing work", "status": "Open"}]
    hits = jp.title_matches("TXN Guardrails", epics)
    assert [h["key"] for h in hits] == ["E-1"]


def test_a_long_epic_name_does_not_win_on_length_alone():
    """Jaccard, not a raw count: one shared word out of twenty is not a match."""
    epics = [{"key": "E-1",
              "summary": "migration of every legacy ledger record across all of the "
                         "historical partitions and their many downstream consumers",
              "status": "Open"}]
    assert jp.title_matches("EP Migration", epics) == []


def test_noise_words_alone_never_match():
    epics = [{"key": "E-1", "summary": "Update the test fixes", "status": "Open"}]
    assert jp.title_matches("update the fixes", epics) == []


# ---- linking a tile to a ticket that already exists -----------------------------------
# A board tile is shorthand for the ticket's real name, so these match on containment
# rather than on shared words.

ISSUES = [
    {"key": "P-1", "summary": "Rollback TRD", "epic": "E-1", "status": "QA Testing"},
    {"key": "P-2", "summary": "Rollback implementation", "epic": "E-1", "status": "To Do"},
    {"key": "P-3", "summary": "AA FIP: PAN + DOB discovery", "epic": "E-2", "status": "To Do"},
    # P-9 is the tighter summary AND the later key, so key order alone would pick P-5.
    {"key": "P-9", "summary": "Backfill Activity / Sub-activity", "epic": "E-3", "status": "To Do"},
    {"key": "P-5", "summary": "Backfill : add activity and account_balance entry rows",
     "epic": "", "status": "To Do"},
    {"key": "P-6", "summary": "Deceased Marking", "epic": "E-4", "status": "Done"},
]


def test_an_identical_name_is_the_strongest_link():
    out = jp.match_tickets("rollback   TRD", ISSUES)
    assert out[0]["key"] == "P-1" and out[0]["score"] == 1.0


def test_a_tile_that_is_the_start_of_a_ticket_name_links_to_it():
    """'AA FIP' is shorthand for 'AA FIP: PAN + DOB discovery'. Word overlap scores this
    at 0.17 and would miss it entirely."""
    out = jp.match_tickets("AA FIP", ISSUES)
    assert out[0]["key"] == "P-3"
    assert out[0]["score"] >= jp.CONTAINS


def test_the_tightest_summary_wins_a_tie_rather_than_the_lowest_ticket_number():
    """Both contain every word of the tile, so both land in the same band. P-9 is the
    one a human means, and it has to win on having less else in it rather than on its
    key, which would have picked the other one."""
    out = jp.match_tickets("Activity backfill", ISSUES)
    assert [r["key"] for r in out[:2]] == ["P-9", "P-5"]


def test_a_link_carries_the_epic_the_ticket_already_sits_in():
    assert jp.match_tickets("AA FIP", ISSUES)[0]["epic"] == "E-2"


def test_finished_work_still_matches_but_ranks_below_live_work():
    """Hiding it would make a tile look new when it is not."""
    out = jp.match_tickets("Deceased marking", ISSUES)
    assert [r["key"] for r in out] == ["P-6"]
    assert out[0]["closed"] is True
    assert out[0]["score"] < 1.0        # penalised for being Done, not dropped


def test_an_unrelated_tile_links_to_nothing():
    assert jp.match_tickets("NEFT power error message", ISSUES) == []


def test_a_linked_ticket_supplies_the_epic_when_no_heading_maps():
    """The whole point of linking: the ticket already knows its epic, so the push does
    not need a heading mapping for that tile."""
    plan = {"epics": {}, "tasks": [{"id": "a", "title": "Rollback TRD", "who": "ann",
                                    "date": "2026-01-05", "days": 1, "key": "P-1"}]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": 1}]
    jira = {"epics": [{"key": "E-1"}], "sprints": [], "byEpic": {},
            "issues": {"P-1": {"summary": "Rollback TRD", "epic": "E-1",
                               "assignee": "x", "sp": 1, "sprintIds": []}}}
    pv = jp.plan_push(plan, people, ["2026-01-05", "2026-01-09"], jira)
    row = pv["rows"][0]
    assert row["epic"] == "E-1"
    assert row["epicFrom"] == "inherited from P-1"
    assert pv["unmapped"] == []         # nothing left to map, so it can publish


# ---- the points must be the number the board shows ------------------------------------
# Mirrors daysOf()/effortOf() in derived/sprint-board.html. If these drift, the plan you
# approved on the board and the plan that lands in Jira are different plans.

def _one(effort, days, sp, eff=0.5):
    plan = {"epics": {"H": "E-1"}, "effort": effort,
            "tasks": [{"id": "a", "title": "t", "who": "ann", "cat": "H",
                       "date": "2026-01-05", "days": days, "sp": sp}]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": eff}]
    jira = {"epics": [{"key": "E-1"}], "sprints": [], "issues": {}, "byEpic": {}}
    return jp.plan_push(plan, people, ["2026-01-05", "2026-01-09"], jira)["rows"][0]


def test_in_days_mode_the_points_are_the_days_as_typed():
    """Days mode shows a calendar length. Efficiency is already accounted for in the
    capacity those days are measured against, so applying it again here halves the plan."""
    assert _one(effort=False, days=5, sp=0)["sp"] == 5
    assert _one(effort=False, days=2.25, sp=0, eff=0.85)["sp"] == 2.25


def test_days_mode_ignores_a_stale_effort_left_on_a_tile():
    """The toggle decides which number is real, not whichever field happens to be set."""
    assert _one(effort=False, days=4, sp=99)["sp"] == 4


def test_in_effort_mode_the_points_are_the_effort_typed_on_the_tile():
    assert _one(effort=True, days=4, sp=2)["sp"] == 2


def test_in_effort_mode_a_tile_predating_the_toggle_is_read_as_a_span():
    """It carries no effort of its own, so its length is a span and converting it is the
    only honest reading. This is exactly what effortOf() falls back to."""
    assert _one(effort=True, days=4, sp=0, eff=0.5)["sp"] == 2


# ---- decisions you make on the push page ---------------------------------------------

def _dec(**task):
    base = {"id": "a", "title": "t", "who": "ann", "cat": "H",
            "date": "2026-01-05", "days": 2, "sp": 0}
    base.update(task)
    plan = {"epics": {"H": "E-1"}, "tasks": [base]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": 0.5}]
    jira = {"epics": [{"key": "E-1"}], "sprints": [], "issues": {}, "byEpic": {}}
    return jp.plan_push(plan, people, ["2026-01-05", "2026-01-09"], jira)


def test_a_tile_marked_as_handled_elsewhere_creates_nothing():
    """The gap this closes: a ticket that exists but nothing matched would otherwise be
    created a second time on publish."""
    pv = _dec(nopush=True)
    row = pv["rows"][0]
    assert row["action"] == jp.SKIP
    assert "handled outside the push" in row["why"]
    assert pv["counts"]["create"] == 0


def test_a_skipped_tile_is_still_reported():
    """Dropped silently, the count you confirm would not add up to the board in front
    of you."""
    assert len(_dec(nopush=True)["rows"]) == 1


def test_points_typed_on_a_row_beat_everything_else():
    row = _dec(days=2, pts=8)["rows"][0]
    assert row["sp"] == 8 and row["spFrom"] == "set by hand"


def test_the_row_carries_the_decisions_back_so_the_page_can_show_them():
    row = _dec(nopush=True, pts=3)["rows"][0]
    assert row["nopush"] is True and row["pts"] == 3


# ---- standing work -------------------------------------------------------------------

# Ordered, specific before general, exactly as config lists them.
STANDING = [{"match": "oncall report", "points": 2},
            {"match": "oncall", "points": 5}]


def _standing(title, **extra):
    base = {"id": "a", "title": title, "who": "ann", "cat": "H",
            "date": "2026-01-05", "days": 0.5, "sp": 0}
    base.update(extra)
    plan = {"epics": {"H": "E-1"}, "tasks": [base]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": 0.5}]
    jira = {"epics": [{"key": "E-1"}], "sprints": [], "issues": {}, "byEpic": {}}
    return jp.plan_push(plan, people, ["2026-01-05", "2026-01-09"], jira,
                        {"standing": STANDING})["rows"][0]


def test_standing_work_is_sized_by_the_rota_not_by_the_days_it_occupies():
    row = _standing("Secondary Oncall")
    assert row["sp"] == 5 and "standing work" in row["spFrom"]


def test_standing_work_matching_is_case_insensitive():
    assert _standing("secondary ONCALL")["sp"] == 5


def test_other_work_is_untouched_by_a_standing_rule():
    assert _standing("Cheque instrument backfill")["sp"] == 0.5


def test_a_number_typed_on_the_row_still_beats_a_standing_rule():
    """The rule is a default, not a verdict: 'Oncall Report' is not the rota."""
    row = _standing("Oncall Report", pts=1)
    assert row["sp"] == 1 and row["spFrom"] == "set by hand"


def test_no_rules_configured_changes_nothing():
    plan = {"epics": {"H": "E-1"},
            "tasks": [{"id": "a", "title": "Secondary Oncall", "who": "ann", "cat": "H",
                       "date": "2026-01-05", "days": 0.5, "sp": 0}]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": 0.5}]
    jira = {"epics": [{"key": "E-1"}], "sprints": [], "issues": {}, "byEpic": {}}
    row = jp.plan_push(plan, people, ["2026-01-05", "2026-01-09"], jira)["rows"][0]
    assert row["sp"] == 0.5 and "spFrom" not in row


def test_the_epic_picker_fires_the_event_the_save_listens_for():
    """The bug this guards: the picker dispatched only `input`, the autosave listens on
    `change`, so an epic chosen from the dropdown was never saved and was gone on the way
    back. Asserted at the source because it is two event names and no behaviour test in
    this suite drives a browser."""
    page = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "derived", "jira-push.html")
    with open(page, encoding="utf-8") as fh:
        src = fh.read()
    take = re.search(r"const take = e=>\{.*?\};", src, re.S)
    assert take, "the picker's selection handler is gone"
    body = take.group(0)
    assert "'input'" in body, "the name beside the box stops updating without it"
    assert "'change'" in body, "without this the choice is never saved"


def test_the_first_matching_standing_rule_wins():
    """Writing the oncall report is not carrying the pager, and the only thing keeping
    them apart is rule order."""
    assert _standing("Oncall Report")["sp"] == 2
    assert _standing("Secondary Oncall")["sp"] == 5


def test_a_general_rule_still_catches_what_the_specific_one_misses():
    assert _standing("Primary oncall cover")["sp"] == 5


# ---- which sprint this window already has --------------------------------------------

SPRINTS = [
    {"id": 1, "name": "TEAM OND 26 S1", "state": "active",
     "start": "2026-10-07T00:00:00.000Z", "end": "2026-10-21T18:30:00.000Z"},
    {"id": 2, "name": "TEAM JAS 26 S7", "state": "closed",
     "start": "2026-09-23", "end": "2026-10-07"},
    # The one being planned. Not started, and its end is the morning after the window
    # anyone would type, which is the normal case rather than the awkward one.
    {"id": 3, "name": "TEAM OND 26 S2", "state": "future",
     "start": "2026-10-21", "end": "2026-11-04"},
]


def test_a_sprint_is_found_by_its_dates_not_by_the_name_we_would_have_given_it():
    """The bug this guards cost a duplicate sprint: a team names a sprint
    "TEAM OND 26 S1", this page derives "2026-10-07 to 2026-10-21", and matching on the
    name alone reported none for a sprint sitting right there."""
    sp, how = jp.find_sprint(["2026-10-07", "2026-10-21"], SPRINTS, "2026-10-07 to 2026-10-21")
    assert sp["id"] == 1 and how == "dates"


def test_a_timestamp_still_matches_a_plain_date():
    """Jira returns full timestamps, and the end one lands in the previous evening UTC."""
    sp, _ = jp.find_sprint(["2026-10-07", "2026-10-21"], SPRINTS)
    assert sp and sp["id"] == 1


def test_a_live_sprint_beats_a_closed_one_covering_the_same_window():
    sprints = [dict(SPRINTS[0], id=9, state="closed"), dict(SPRINTS[0], id=10)]
    sp, _ = jp.find_sprint(["2026-10-07", "2026-10-21"], sprints)
    assert sp["id"] == 10


def test_the_name_is_still_a_fallback_when_no_dates_line_up():
    sp, how = jp.find_sprint(["2026-11-04", "2026-11-17"], SPRINTS, "TEAM JAS 26 S7")
    assert sp["id"] == 2 and how == "name"


def test_no_sprint_for_this_window_is_reported_as_such():
    assert jp.find_sprint(["2027-01-01", "2027-01-14"], SPRINTS, "nope") == (None, "")


def test_the_preview_names_the_sprint_jira_has_rather_than_the_one_we_derived():
    plan = {"epics": {"H": "E-1"},
            "tasks": [{"id": "a", "title": "t", "who": "ann", "cat": "H",
                       "date": "2026-10-08", "days": 1}]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": 1}]
    jira = {"epics": [{"key": "E-1"}], "sprints": SPRINTS, "issues": {}, "byEpic": {}}
    sp = jp.plan_push(plan, people, ["2026-10-07", "2026-10-21"], jira)["sprint"]
    assert sp["exists"] is True and sp["id"] == 1
    assert sp["name"] == "TEAM OND 26 S1"
    assert sp["derivedName"] == "2026-10-07 to 2026-10-21"
    assert sp["matchedBy"] == "dates"


# ---- oncall, read out of the capacity calendar ----------------------------------------
# Nobody draws an oncall tile. It is "O" in the calendar, and those days are already
# subtracted from capacity, so a tile for it as well would charge the fortnight twice.

WINDOW_OCT = ["2026-10-07", "2026-10-21"]
#               W  T  F  WE  WE  M  T   W   T   F  WE  WE   M   T   W
STINT = ["O", "O", "O", "WE", "WE", "O", "O", "", "", "", "WE", "WE", "", "", "H"]


def test_an_oncall_stint_becomes_one_tile():
    out = jp.oncall_tiles([{"canonical": "tk", "name": "T", "statuses": STINT}], WINDOW_OCT)
    assert len(out) == 1
    assert out[0]["who"] == "tk" and out[0]["date"] == "2026-10-07"


def test_a_weekend_inside_a_stint_does_not_split_it():
    """The pager does not stop on Saturday, and two tickets for one rotation is not what
    anyone means."""
    assert len(jp.oncall_tiles(
        [{"canonical": "tk", "name": "T", "statuses": STINT}], WINDOW_OCT)) == 1


def test_the_weekend_is_not_charged_as_a_day_carried():
    """Five O days across a weekend is five, not seven."""
    out = jp.oncall_tiles([{"canonical": "tk", "name": "T", "statuses": STINT}], WINDOW_OCT)
    assert out[0]["days"] == 5


def test_two_separate_stints_become_two_tiles():
    st = ["O", "O", "", "", "", "", "", "O", "O", ""]
    out = jp.oncall_tiles([{"canonical": "tk", "name": "T", "statuses": st}],
                          ["2026-10-07", "2026-10-16"])
    assert [t["date"] for t in out] == ["2026-10-07", "2026-10-14"]


def test_nobody_on_call_means_no_tile():
    assert jp.oncall_tiles(
        [{"canonical": "pj", "name": "P", "statuses": ["", "", "L", "WE", "WE"]}],
        WINDOW_OCT) == []


def test_the_id_is_derived_so_a_decision_about_it_survives_the_next_preview():
    a = jp.oncall_tiles([{"canonical": "tk", "name": "T", "statuses": STINT}], WINDOW_OCT)
    b = jp.oncall_tiles([{"canonical": "tk", "name": "T", "statuses": STINT}], WINDOW_OCT)
    assert a[0]["id"] == b[0]["id"] == "oncall:tk:2026-10-07"


def test_oncall_is_sized_by_the_rota_and_a_standing_rule_does_not_resize_it():
    """The standing rule exists for tiles someone typed. These are already the rota."""
    tile = jp.oncall_tiles([{"canonical": "ann", "name": "Ann",
                             "statuses": ["O", "O", "O"]}], WINDOW_OCT)[0]
    plan = {"epics": {"": "E-1"}, "tasks": [tile]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": 0.5}]
    jira = {"epics": [{"key": "E-1"}], "sprints": [], "issues": {}, "byEpic": {}}
    row = jp.plan_push(plan, people, WINDOW_OCT, jira,
                       {"standing": [{"match": "oncall", "points": 99}]})["rows"][0]
    assert row["sp"] == 3 and "on the rota" in row["spFrom"]


# ---- only this window's work ----------------------------------------------------------

def test_work_scheduled_outside_the_window_is_not_pushed():
    """The board plan is one list across every sprint. Without this filter the push swept
    up every scheduled tile and put the lot into whichever sprint the window matched: a
    whole fortnight of S2 work was about to land in S1."""
    plan = {"epics": {"H": "E-1"},
            "tasks": [{"id": "in", "title": "this sprint", "who": "ann", "cat": "H",
                       "date": "2026-10-09", "days": 1},
                      {"id": "out", "title": "next sprint", "who": "ann", "cat": "H",
                       "date": "2026-10-28", "days": 1}]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": 1}]
    jira = {"epics": [{"key": "E-1"}], "sprints": [], "issues": {}, "byEpic": {}}
    pv = jp.plan_push(plan, people, ["2026-10-07", "2026-10-21"], jira)
    assert [r["id"] for r in pv["rows"]] == ["in"]
    assert pv["elsewhere"] == 1


def test_the_window_edges_are_included():
    plan = {"epics": {"H": "E-1"},
            "tasks": [{"id": "a", "title": "first day", "who": "ann", "cat": "H",
                       "date": "2026-10-07", "days": 1},
                      {"id": "b", "title": "last day", "who": "ann", "cat": "H",
                       "date": "2026-10-21", "days": 1}]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": 1}]
    jira = {"epics": [{"key": "E-1"}], "sprints": [], "issues": {}, "byEpic": {}}
    pv = jp.plan_push(plan, people, ["2026-10-07", "2026-10-21"], jira)
    assert sorted(r["id"] for r in pv["rows"]) == ["a", "b"] and pv["elsewhere"] == 0


# ---- naming a sprint that does not exist yet ------------------------------------------

def test_the_next_sprint_carries_the_series_forward():
    """A correct "2026-11-04 to 2026-11-17" is still the wrong name: it leaves a sprint
    on the board nobody recognises."""
    assert jp.next_sprint_name(SPRINTS, ["2026-11-04", "2026-11-17"]) == "TEAM OND 26 S3"


def test_the_number_is_taken_from_the_sprint_that_ends_where_this_one_starts():
    assert jp.next_sprint_name(SPRINTS, ["2026-10-07", "2026-10-21"]) == "TEAM JAS 26 S8"


def test_a_name_with_no_number_is_not_guessed_at():
    sprints = [{"id": 1, "name": "Hardening", "start": "2026-09-01", "end": "2026-09-15"}]
    assert jp.next_sprint_name(sprints, ["2026-10-07", "2026-10-21"]) == ""


def test_with_no_prior_sprint_there_is_nothing_to_carry_forward():
    assert jp.next_sprint_name([], ["2026-10-07", "2026-10-21"]) == ""


def test_an_existing_sprint_is_never_offered_a_new_name():
    plan = {"epics": {"H": "E-1"},
            "tasks": [{"id": "a", "title": "t", "who": "ann", "cat": "H",
                       "date": "2026-10-08", "days": 1}]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": 1}]
    jira = {"epics": [{"key": "E-1"}], "sprints": SPRINTS, "issues": {}, "byEpic": {}}
    sp = jp.plan_push(plan, people, ["2026-10-07", "2026-10-21"], jira)["sprint"]
    assert sp["exists"] and sp["suggestName"] == ""


def test_a_sprint_that_has_not_started_yet_is_still_found():
    """The sprint a push writes into is usually the one nobody has opened. Reusing the
    retro picker's sprint list hid it, so the push declared none existed and would have
    created a second one beside it."""
    sp, how = jp.find_sprint(["2026-10-21", "2026-11-03"], SPRINTS)
    assert sp["id"] == 3 and sp["state"] == "future" and how == "dates"


def test_a_sprint_running_a_day_longer_than_the_window_still_matches():
    """Jira's end date is routinely the morning after the fortnight anyone would type."""
    sp, _ = jp.find_sprint(["2026-10-21", "2026-11-03"], SPRINTS)
    assert sp["id"] == 3


def test_a_sprint_running_a_day_SHORTER_than_the_window_still_matches():
    """The case exact matching cannot reach: the sprint does not cover the whole window,
    so nothing short of a coverage threshold finds it."""
    sprints = [{"id": 5, "name": "short", "state": "future",
                "start": "2026-10-22", "end": "2026-11-02"}]
    sp, how = jp.find_sprint(["2026-10-21", "2026-11-03"], sprints)
    assert sp["id"] == 5 and how == "dates"


def test_a_sprint_that_merely_touches_the_window_does_not_win_it():
    """S1 ends on the day S2 starts. One shared day is not coverage."""
    sp, _ = jp.find_sprint(["2026-10-21", "2026-11-03"], SPRINTS)
    assert sp["id"] != 1


def test_the_sprint_covering_most_of_the_window_wins():
    sprints = [{"id": 7, "name": "half", "state": "future",
                "start": "2026-10-21", "end": "2026-10-28"},
               {"id": 8, "name": "all", "state": "future",
                "start": "2026-10-21", "end": "2026-11-04"}]
    sp, _ = jp.find_sprint(["2026-10-21", "2026-11-03"], sprints)
    assert sp["id"] == 8


def test_nothing_covering_the_window_is_still_nothing():
    assert jp.find_sprint(["2027-01-01", "2027-01-14"], SPRINTS, "nope") == (None, "")


# ---- the type a created ticket gets ---------------------------------------------------

def test_creating_without_a_configured_issue_type_is_refused():
    """It used to default to one. The default was Story, the team wanted Task, and
    fifteen real tickets were created as the wrong type with nothing on the page saying
    so. A missing setting is now a refusal, not a guess."""
    pv = {"v": jp.SCHEMA, "sprint": {"id": 1, "name": "s"}, "rows": [], "unmapped": []}
    r = jp.apply_push(pv, lambda *a: {}, 1, "P", {"story_points": "sp"}, confirm=True)
    assert "__error__" in r and "task_issuetype_id" in r["__error__"]


def test_an_existing_ticket_of_the_wrong_type_is_offered_as_a_change():
    plan = {"epics": {"H": "E-1"},
            "tasks": [{"id": "a", "title": "t", "who": "ann", "cat": "H",
                       "date": "2026-01-05", "days": 1, "key": "P-1"}]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": 1}]
    jira = {"epics": [{"key": "E-1"}], "sprints": [], "byEpic": {},
            "issues": {"P-1": {"summary": "t", "epic": "E-1", "assignee": "x", "sp": 1,
                               "sprintIds": [], "issuetype": "10001",
                               "issuetypeName": "Story"}}}
    row = jp.plan_push(plan, people, ["2026-01-05", "2026-01-09"], jira,
                       {"issuetype": "10002", "issuetypeName": "Task"})["rows"][0]
    change = [c for c in row["changes"] if c[0] == "issuetype"]
    assert change == [("issuetype", "Story", "Task")]


def test_a_ticket_already_of_the_right_type_is_left_alone():
    plan = {"epics": {"H": "E-1"},
            "tasks": [{"id": "a", "title": "t", "who": "ann", "cat": "H",
                       "date": "2026-01-05", "days": 1, "key": "P-1"}]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": 1}]
    jira = {"epics": [{"key": "E-1"}], "sprints": [], "byEpic": {},
            "issues": {"P-1": {"summary": "t", "epic": "E-1", "assignee": "x", "sp": 1,
                               "sprintIds": [], "issuetype": "10002",
                               "issuetypeName": "Task"}}}
    row = jp.plan_push(plan, people, ["2026-01-05", "2026-01-09"], jira,
                       {"issuetype": "10002", "issuetypeName": "Task"})["rows"][0]
    assert not [c for c in row["changes"] if c[0] == "issuetype"]


def test_the_type_change_is_actually_written():
    sent = []
    pv = {"v": jp.SCHEMA, "unmapped": [], "sprint": {"id": 7, "name": "s"},
          "rows": [{"id": "a", "key": "P-1", "action": jp.UPDATE, "title": "t",
                    "accountId": "x", "sp": 1,
                    "changes": [("issuetype", "Story", "Task")]}]}
    jp.apply_push(pv, lambda m, p, b: (sent.append((m, p, b)), {"key": "P-1"})[1],
                  1, "P", {"story_points": "sp", "issuetype_id": "10002"}, confirm=True)
    put = [b for m, p, b in sent if m == "PUT"]
    assert put and put[0]["fields"]["issuetype"] == {"id": "10002"}


def test_a_linked_ticket_missing_from_the_snapshot_is_refused_not_quietly_created():
    """Why this matters: the oncall rows carried real keys, but the Jira snapshot was
    taken before the derived tiles were added to the plan, so those keys were never
    looked up and both rows came back "not found" and were skipped on every publish.
    Skipping is the right behaviour here; the fix is that the caller must snapshot the
    plan it is actually pushing."""
    plan = {"epics": {"H": "E-1"},
            "tasks": [{"id": "a", "title": "t", "who": "ann", "cat": "H",
                       "date": "2026-01-05", "days": 1, "key": "P-1"}]}
    people = [{"canonical": "ann", "name": "Ann", "jira_id": "x", "eff": 1}]
    jira = {"epics": [{"key": "E-1"}], "sprints": [], "byEpic": {}, "issues": {}}
    row = jp.plan_push(plan, people, ["2026-01-05", "2026-01-09"], jira)["rows"][0]
    assert row["action"] == jp.SKIP and "not found" in row["why"]

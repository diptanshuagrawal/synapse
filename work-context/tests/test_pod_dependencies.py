"""Cross-pod dependency register (/deps), from the COUNTERPART pod's side.

An initiative carries a multi-value PODs field; any pod on it beyond your own is a team you
must coordinate with. The question that matters is not whether YOU are ready — it is whether
THEY have created a delivery epic in their own project, dated it and budgeted it. So a row is
(initiative x tagged pod) carrying that pod's epic, and "Missing epic" means the team is
tagged but has nothing of its own yet.

A pod's Jira project is the prefix of its label ("BOPS - BRANCH-BANKING" -> BOPS). Two pods
can share a project, and those rows are flagged `ambiguous` rather than guessed at.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "derive"))

import capacity_engine as ce

MINE = "BOARD - Core Payments Platform"
COREP = "COREP - Core Platform"
BOPS = "BOPS - BRANCH-BANKING"


def _init(key, pods, dri="Me", epic=None, budgets=None, due="", cycles=None, linked=None):
    return {"key": key, "url": "u/" + key, "summary": key + " work", "status": "Open",
            "orgPriority": "Prog 0", "engDri": dri, "prodDri": "P",
            "epic": ({"key": epic, "url": "u/" + epic, "summary": "e",
                      "status": "To Do", "dueDate": due, "health": ""} if epic else None),
            "podTags": [MINE] + pods, "cycles": cycles or [],
            "linkedKeys": linked if linked is not None else ([epic] if epic else []),
            "budgets": budgets or {m: 0 for m in ce.BUDGET_MONTHS}}


def _epic(key, status="To Do", due="", budgets=None, owner=""):
    return {"key": key, "url": "u/" + key, "summary": key, "status": status,
            "dueDate": due, "assignee": owner, "health": "",
            "budgets": budgets or {}}


@pytest.fixture
def stub(monkeypatch):
    def use(inits, epics=None):
        monkeypatch.setattr(ce, "pod_initiatives",
                            lambda pods=None: {"pods": [MINE], "initiatives": inits})
        monkeypatch.setattr(ce, "current_user",
                            lambda: {"accountId": "a", "name": "Me", "email": "me@x"})
        monkeypatch.setattr(ce, "_fetch_epics", lambda keys, months: dict(epics or {}))
    return use


def test_only_other_pods_become_dependencies(stub):
    stub([_init("OINT-1", [COREP], linked=["COREP-1"]),
          _init("OINT-2", [])],                      # self-contained -> excluded
         {"COREP-1": _epic("COREP-1", due="d", budgets={"Oct": 5})})
    r = ce.pod_dependencies(months=["2026-10"])
    assert [g["pod"] for g in r["groups"]] == [COREP]
    assert MINE not in [g["pod"] for g in r["groups"]], "own pod is not a dependency"
    assert r["cards"]["initiatives"] == 1


def test_one_initiative_appears_under_each_pod(stub):
    stub([_init("OINT-1", [COREP, BOPS], linked=[])])
    r = ce.pod_dependencies(months=["2026-10"])
    assert sorted(x["pod"] for x in r["rows"]) == sorted([BOPS, COREP])
    assert r["cards"]["initiatives"] == 1, "counted once, not once per pod"
    assert {g["pod"]: g["count"] for g in r["groups"]} == {COREP: 1, BOPS: 1}


def test_groups_put_the_worst_gap_first(stub):
    """Two deps both ready beats one dep with no epic — chase the gap, not the volume."""
    stub([_init("OINT-1", [COREP], linked=["COREP-1"]),
          _init("OINT-2", [COREP], linked=["COREP-2"]),
          _init("OINT-3", [BOPS], linked=[])],
         {"COREP-1": _epic("COREP-1", due="d", budgets={"Oct": 1}),
          "COREP-2": _epic("COREP-2", due="d", budgets={"Oct": 1})})
    r = ce.pod_dependencies(months=["2026-10"])
    assert [g["pod"] for g in r["groups"]] == [BOPS, COREP]
    assert r["cards"]["topPod"] == BOPS and r["cards"]["topCount"] == 1


def test_dri_filter(stub):
    stub([_init("OINT-1", [COREP], dri="Me", linked=[]),
          _init("OINT-2", [COREP], dri="Someone Else", linked=[])])
    mine = ce.pod_dependencies(months=["2026-10"], dri_only=True)
    assert [x["key"] for x in mine["rows"]] == ["OINT-1"] and mine["driOnly"] is True
    everyone = ce.pod_dependencies(months=["2026-10"], dri_only=False)
    assert sorted(x["key"] for x in everyone["rows"]) == ["OINT-1", "OINT-2"]
    assert everyone["driOnly"] is False


def test_state_ladder_is_about_their_epic(stub):
    stub([_init("OINT-1", [COREP], linked=[]),                       # they created nothing
          _init("OINT-2", [COREP], linked=["COREP-2"]),              # epic, no budget
          _init("OINT-3", [COREP], linked=["COREP-3"]),              # budget, no due date
          _init("OINT-4", [COREP], linked=["COREP-4"])],             # dated + budgeted
         {"COREP-2": _epic("COREP-2"),
          "COREP-3": _epic("COREP-3", budgets={"Oct": 4}),
          "COREP-4": _epic("COREP-4", due="2026-10-30", budgets={"Oct": 4})})
    r = ce.pod_dependencies(months=["2026-10"])
    state = {x["key"]: x["state"] for x in r["rows"]}
    assert state == {"OINT-1": "Missing epic", "OINT-2": "Missing budget",
                     "OINT-3": "No due date", "OINT-4": "Ready"}
    action = {x["key"]: x["nextAction"] for x in r["rows"]}
    assert "COREP" in action["OINT-1"], "the action must name the team to chase"
    assert "COREP-2" in action["OINT-2"], "the action must name their epic"
    assert r["cards"]["missingEpic"] == 1 and r["cards"]["missingBudget"] == 1


def test_my_own_epic_does_not_satisfy_their_dependency(stub):
    """The whole point: I have a BOARD epic, COREP has nothing -> still Missing."""
    stub([_init("OINT-1", [COREP], epic="BOARD-1", budgets={"Oct": 30}, due="2026-10-30")],
         {"BOARD-1": _epic("BOARD-1", due="2026-10-30", budgets={"Oct": 30})})
    r = ce.pod_dependencies(months=["2026-10"])
    row = r["rows"][0]
    assert row["state"] == "Missing epic", "my readiness is not theirs"
    assert row["epic"] == "" and row["myEpic"] == "BOARD-1"
    assert row["sp"] == 0, "their SP, not mine"
    assert row["myMonths"] == {"Oct-26": 30.0}


def test_their_epic_is_matched_by_project_prefix(stub):
    stub([_init("OINT-1", [COREP, BOPS], epic="BOARD-1",
                linked=["BOARD-1", "COREP-9", "BOPS-7"])],
         {"BOARD-1": _epic("BOARD-1"), "COREP-9": _epic("COREP-9", due="d", budgets={"Oct": 4}),
          "BOPS-7": _epic("BOPS-7", due="d", budgets={"Oct": 6})})
    r = ce.pod_dependencies(months=["2026-10"])
    got = {x["pod"]: (x["epic"], x["sp"]) for x in r["rows"]}
    assert got[COREP] == ("COREP-9", 4.0)
    assert got[BOPS] == ("BOPS-7", 6.0), "each pod gets ITS OWN epic, not the first link"


def test_shared_project_is_flagged_not_guessed(stub):
    """BOARD Ledger shares your project, so a BOARD epic cannot be attributed to one pod."""
    other_cbst = "BOARD - Core Ledger Platform"
    stub([_init("OINT-1", [other_cbst], epic="BOARD-1", linked=["BOARD-1"])],
         {"BOARD-1": _epic("BOARD-1", due="d", budgets={"Oct": 5})})
    r = ce.pod_dependencies(months=["2026-10"])
    assert r["rows"][0]["ambiguous"] is True
    assert r["groups"][0]["ambiguous"] is True


def test_sp_totals_per_month_and_group(stub):
    stub([_init("OINT-1", [COREP], linked=["COREP-1"]),
          _init("OINT-2", [COREP], linked=["COREP-2"])],
         {"COREP-1": _epic("COREP-1", due="d", budgets={"Oct": 5, "Nov": 3}),
          "COREP-2": _epic("COREP-2", due="d", budgets={"Oct": 2, "Nov": 0})})
    r = ce.pod_dependencies(months=["2026-10", "2026-11"])
    assert [m["label"] for m in r["months"]] == ["Oct-26", "Nov-26"]
    g = r["groups"][0]
    assert g["months"] == {"Oct-26": 7.0, "Nov-26": 3.0}
    assert g["sp"] == 10.0 and r["cards"]["sp"] == 10.0


def test_months_outside_the_window_are_ignored(stub):
    stub([_init("OINT-1", [COREP], linked=["COREP-1"])],
         {"COREP-1": _epic("COREP-1", due="d", budgets={"Oct": 5, "Dec": 99})})
    r = ce.pod_dependencies(months=["2026-10"])
    assert r["rows"][0]["sp"] == 5.0, "December must not leak into an October window"


def test_default_window_is_this_month_and_next():
    import datetime as dt
    assert ce._default_window(dt.date(2026, 10, 15)) == ["2026-10", "2026-11"]
    assert ce._default_window(dt.date(2026, 12, 3)) == ["2026-12", "2027-01"], "year rollover"


def test_engine_error_is_passed_through(monkeypatch):
    monkeypatch.setattr(ce, "pod_initiatives", lambda pods=None: {"__error__": "jira down"})
    assert ce.pod_dependencies()["__error__"] == "jira down"


def test_page_has_no_month_window_and_multi_selects_cycles():
    """The months whose budgets are shown ARE the selected planning cycles — one control,
    not two. A separate month-window picker would be a second source of truth."""
    page = os.path.join(os.path.dirname(__file__), "..", "derived", "deps.html")
    if not os.path.exists(page):
        pytest.skip("derived/ is gitignored — no deps.html in this checkout")
    html = open(page, encoding="utf-8").read()
    assert "monthSel" not in html, "the month-window picker should be gone"
    assert "ddCycles" in html and 'type="checkbox"' in html, "cycles must be multi-select"
    assert "function selectedMonths()" in html, "months are derived from the chosen cycles"
    assert "cycleToYm" in html, "a cycle label must map to the month it budgets"


def test_page_has_reload_and_shows_data_age():
    """A cached register must never look live: the byline states the age, and there is an
    explicit way to rebuild from Jira."""
    page = os.path.join(os.path.dirname(__file__), "..", "derived", "deps.html")
    if not os.path.exists(page):
        pytest.skip("derived/ is gitignored — no deps.html in this checkout")
    html = open(page, encoding="utf-8").read()
    assert 'id="reload"' in html, "the register needs a reload-from-Jira control"
    assert "fresh" in html and "load(true)" in html, "reload must force a rebuild"
    assert "function freshness()" in html, "the page must say how old the data is"


def test_dependencies_cache_is_keyed_and_versioned():
    server = open(os.path.join(os.path.dirname(__file__), "..", "derive", "sprint_server.py"),
                  encoding="utf-8").read()
    assert "deps-{hashlib" in server, "cache file must be keyed by pods/months/dri"
    assert "DEPENDENCIES_SCHEMA" in server, "a stale-shaped cache must be discarded"
    assert 'q.get("fresh"' in server, "fresh=1 must bypass the cache"
    # the initiatives-cache glob must not swallow the new deps-*.json files
    import re as _re
    name = _re.compile(r"^initiatives-(default|[0-9a-f]{8})\.json$")
    assert not name.match("deps-58b206f8.json")


def test_page_can_copy_a_chase_list():
    """The register exists to be sent to another EM, so it must be copyable as plain text
    with real Jira links — a table pasted into Slack does not survive."""
    page = os.path.join(os.path.dirname(__file__), "..", "derived", "deps.html")
    if not os.path.exists(page):
        pytest.skip("derived/ is gitignored — no deps.html in this checkout")
    html = open(page, encoding="utf-8").read()
    assert 'id="copyAll"' in html, "a copy control for the filtered list"
    assert 'data-copy=' in html, "a per-team copy control, to send one EM their own list"
    assert "function copyText(" in html and "r.url" in html, "the text must carry Jira links"
    assert "function visibleRows()" in html, \
        "copy and table must share one definition of what is on screen"
    assert "execCommand('copy')" in html, "clipboard fallback for non-secure contexts"
    assert "e.target.closest('button.copy')" in html, \
        "copying a team must not also expand/collapse it"


def test_register_columns_are_configurable():
    """PODs and the rest are opt-in columns; the row identity column always shows."""
    page = os.path.join(os.path.dirname(__file__), "..", "derived", "deps.html")
    if not os.path.exists(page):
        pytest.skip("derived/ is gitignored — no deps.html in this checkout")
    html = open(page, encoding="utf-8").read()
    assert 'id="ddCols"' in html, "a column picker"
    assert "const REG_COLS" in html and "id:'pods'" in html, "PODs must be an available column"
    assert "DEFAULT_COLS" in html and "depsCols" in html, "selection persists"
    # the team header spans whatever columns are on, or the block header misaligns
    assert 'colspan="${span}"' in html and "const span = 1 + cols.reduce" in html


def test_rows_carry_the_full_pod_list(monkeypatch):
    """The PODs column shows the raw field, own pod included — allPods excludes it."""
    monkeypatch.setattr(ce, "pod_initiatives", lambda pods=None: {"pods": [MINE], "initiatives": [
        _init("OINT-1", [COREP, BOPS], linked=[])]})
    monkeypatch.setattr(ce, "current_user", lambda: {"accountId": "a", "name": "Me", "email": "e"})
    monkeypatch.setattr(ce, "_fetch_epics", lambda keys, months: {})
    r = ce.pod_dependencies(months=["2026-10"])
    row = r["rows"][0]
    assert MINE in row["podTags"], "podTags is the raw PODs field"
    assert MINE not in row["allPods"], "allPods is the counterparts only"
    assert sorted(row["podTags"]) == sorted([MINE, COREP, BOPS])


def test_page_and_route_exist():
    root = os.path.join(os.path.dirname(__file__), "..")
    page = os.path.join(root, "derived", "deps.html")
    if not os.path.exists(page):
        pytest.skip("derived/ is gitignored — no deps.html in this checkout")
    html = open(page, encoding="utf-8").read()
    assert "/api/dependencies" in html
    server = open(os.path.join(root, "derive", "sprint_server.py"), encoding="utf-8").read()
    assert '"/deps"' in server and "/deps.html" in server
    nav = open(os.path.join(root, "derive", "synapse_nav.py"), encoding="utf-8").read()
    assert '"/deps"' in nav, "the report needs a sidebar entry to be findable"


def test_planning_cycle_rides_along_on_every_row(stub):
    """The cycle filter on /deps is client-side, so each row must carry its own cycles."""
    stub([_init("OINT-1", [COREP, BOPS], linked=[], cycles=["Oct-26", "Nov-26"])])
    r = ce.pod_dependencies(months=["2026-10"])
    assert len(r["rows"]) == 2, "one row per dependent pod"
    for row in r["rows"]:
        assert row["cycles"] == ["Oct-26", "Nov-26"], "both rows carry the initiative's cycles"


def test_cycle_is_independent_of_whether_a_budget_exists(stub):
    """An un-budgeted initiative still belongs to its cycle — those are the ones needing work."""
    stub([_init("OINT-1", [COREP], cycles=["Oct-26"])])          # no epic, no budget
    r = ce.pod_dependencies(months=["2026-10"])
    row = r["rows"][0]
    assert row["cycles"] == ["Oct-26"] and row["sp"] == 0
    assert row["state"] == "Missing epic"


def test_rows_with_no_cycle_are_still_returned(stub):
    stub([_init("OINT-1", [COREP], linked=[])])
    r = ce.pod_dependencies(months=["2026-10"])
    assert r["rows"][0]["cycles"] == [], "absent cycle is an empty list, not missing"


def test_pod_project_prefix():
    assert ce._pod_project("BOPS - BRANCH-BANKING") == "BOPS"
    assert ce._pod_project("LEO -  Liabilities Exp & Orchestrator") == "LEO", "double space"
    assert ce._pod_project("COREP - Core Platform") == "COREP"
    assert ce._pod_project("") == ""

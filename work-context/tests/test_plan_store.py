"""The monthly planner's own state, stored where every browser can see the same one.

This exists because the stack rank lived only in localStorage: the desktop app and Chrome
were two different planners that happened to look alike, and there was no way to tell
which order was the real one. These tests pin the two rules that make the stored copy
trustworthy - it is never replaced by a blank one, and every save keeps what it replaced.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "derive"))
import plan_store as ps


@pytest.fixture
def store(tmp_path):
    return {"path": str(tmp_path / "plan.json"), "hist": str(tmp_path / "hist")}


def test_a_missing_store_reads_as_an_empty_plan(store):
    got = ps.read_plan(store["path"])
    assert got["order"] == [] and got["plan"] == {}


def test_a_plan_round_trips(store):
    r = ps.write_plan({"order": ["OINT-1", "OINT-2"], "plan": {"OINT-1": {"Oct": {"sp": 3}}},
                       "pods": ["TEAM - A"], "months": ["2026-10"]}, **store)
    assert r["ok"] and r["order"] == 2
    got = ps.read_plan(store["path"])
    assert got["order"] == ["OINT-1", "OINT-2"]
    assert got["plan"]["OINT-1"]["Oct"]["sp"] == 3
    assert got["pods"] == ["TEAM - A"]
    assert got["saved"]


def test_a_blank_browser_cannot_wipe_a_saved_plan(store):
    """The failure this file exists to prevent. A browser with cleared storage sends an
    empty order; accepting it would replace a quarter of planning with nothing."""
    ps.write_plan({"order": ["OINT-1", "OINT-2", "OINT-3"]}, **store)
    r = ps.write_plan({"order": []}, **store)
    assert "__error__" in r
    assert r["kept"] == 3
    assert ps.read_plan(store["path"])["order"] == ["OINT-1", "OINT-2", "OINT-3"]


def test_an_empty_plan_can_still_be_seeded(store):
    """Refusing a blank save must not stop the FIRST save from an empty store."""
    assert ps.write_plan({"order": []}, **store)["ok"]


def test_every_save_keeps_the_one_it_replaced(store):
    ps.write_plan({"order": ["a", "b"]}, **store)
    ps.write_plan({"order": ["b", "a"]}, **store)
    ps.write_plan({"order": ["b", "a", "c"]}, **store)
    hist = ps.history(hist=store["hist"])
    assert len(hist) == 2, "two saves replaced something, so two versions are kept"
    assert hist[0]["order"] == 2 and hist[1]["order"] == 2


def test_a_replaced_order_can_be_read_back(store):
    """The whole point: if an order changes and it should not have, the old one is still
    on disk rather than being a thing nobody can reconstruct."""
    ps.write_plan({"order": ["mine", "ranked", "carefully"]}, **store)
    ps.write_plan({"order": ["jira", "order"]}, **store)
    old = ps.history(hist=store["hist"])[0]
    with open(os.path.join(store["hist"], old["file"])) as fh:
        assert json.load(fh)["order"] == ["mine", "ranked", "carefully"]


def test_a_kept_version_can_be_made_current_again(store):
    ps.write_plan({"order": ["mine", "ranked", "carefully"]}, **store)
    ps.write_plan({"order": ["jira", "order"]}, **store)
    name = ps.history(hist=store["hist"])[0]["file"]
    assert ps.restore(name, **store)["ok"]
    assert ps.read_plan(store["path"])["order"] == ["mine", "ranked", "carefully"]


def test_a_record_says_which_fields_it_speaks_for(store):
    """Versions are too blunt. A browser running yesterday's page sends only the rank, and
    the record must not then assert "no columns" on its behalf."""
    ps.write_plan({"order": ["a"], "plan": {}, "pods": None, "months": ["2026-10"]}, **store)
    got = ps.read_plan(store["path"])
    assert "cols" not in got["fields"], "it never sent columns, so it cannot speak for them"
    assert "order" in got["fields"]

    ps.write_plan({"order": ["a"], "cols": []}, **store)
    assert "cols" in ps.read_plan(store["path"])["fields"], \
        "sending an empty list IS a statement about columns"


def test_a_record_written_before_any_of_this_speaks_only_for_the_rank(store):
    with open(store["path"], "w") as fh:
        json.dump({"v": 1, "order": ["a"], "plan": {}, "pods": None, "months": [],
                   "saved": "x", "savedBy": "y"}, fh)
    assert ps.read_plan(store["path"])["fields"] == ["months", "order", "plan", "pods"]


def test_a_window_that_is_behind_cannot_overwrite_a_newer_plan(store):
    """Two windows are both allowed to write. Without this the one that happened to click
    last would quietly erase the other - which is how the rank went missing in the first
    place, one layer up."""
    ps.write_plan({"order": ["a", "b", "c"]}, **store)
    rev = ps.read_plan(store["path"])["rev"]
    ps.write_plan({"order": ["a", "b", "c", "d"]}, base_rev=rev, **store)   # the other window

    stale = ps.write_plan({"order": ["a", "b", "c"]}, base_rev=rev, **store)
    assert stale.get("conflict") is True
    assert ps.read_plan(store["path"])["order"] == ["a", "b", "c", "d"], "it was not applied"
    assert stale["plan"]["order"] == ["a", "b", "c", "d"], "the refusal carries what is there"


def test_a_write_in_step_with_the_store_is_applied(store):
    ps.write_plan({"order": ["a"]}, **store)
    rev = ps.read_plan(store["path"])["rev"]
    assert ps.write_plan({"order": ["a", "b"]}, base_rev=rev, **store)["ok"]
    assert ps.read_plan(store["path"])["order"] == ["a", "b"]


def test_a_writer_that_does_not_claim_a_revision_is_still_allowed(store):
    """The seed from a browser that has never synced has no revision to offer."""
    ps.write_plan({"order": ["a"]}, **store)
    assert ps.write_plan({"order": ["a", "b"]}, **store)["ok"]


def test_the_revision_moves_on_every_save(store):
    assert ps.read_plan(store["path"])["rev"] == 0
    ps.write_plan({"order": ["a"]}, **store)
    assert ps.read_plan(store["path"])["rev"] == 1
    ps.write_plan({"order": ["b"]}, **store)
    assert ps.read_plan(store["path"])["rev"] == 2


def test_a_restored_version_comes_back_as_it_was(store):
    """Stamping a restored record with today's schema would claim it carries fields it
    never had, and a browser adopting it would read "no columns" out of a record written
    before columns existed - blanking a view that was fine."""
    with open(store["path"], "w") as fh:
        json.dump({"v": 1, "order": ["mine", "ranked"], "plan": {}, "pods": None,
                   "months": [], "saved": "x", "savedBy": "y"}, fh)
    ps.write_plan({"order": ["something", "else"], "cols": ["dri"]}, **store)
    name = [h for h in ps.history(hist=store["hist"]) if h["order"] == 2][-1]["file"]
    assert ps.restore(name, **store)["ok"]
    got = ps.read_plan(store["path"])
    assert got["order"] == ["mine", "ranked"]
    assert got["v"] == 1, "it is the record it was, not a new one wearing today's shape"


def test_restoring_keeps_what_it_replaced_too(store):
    """An undo that cannot itself be undone is just another way to lose the plan."""
    ps.write_plan({"order": ["one"]}, **store)
    ps.write_plan({"order": ["two"]}, **store)
    ps.restore(ps.history(hist=store["hist"])[0]["file"], **store)
    assert ps.read_plan(store["path"])["order"] == ["one"]
    assert any(h["order"] == 1 for h in ps.history(hist=store["hist"]))


def test_two_saves_in_the_same_millisecond_both_keep_their_version(store):
    """The kept version is the only copy of what was replaced. Letting a second save in
    the same millisecond land on the same filename would lose exactly that."""
    for i in range(5):
        ps.write_plan({"order": ["k%d" % i]}, **store)
    assert len(ps.history(hist=store["hist"])) == 4, "one version per save that replaced one"
    assert len({h["file"] for h in ps.history(hist=store["hist"])}) == 4


def test_history_is_trimmed_but_not_unbounded(store, monkeypatch):
    monkeypatch.setattr(ps, "KEEP", 3)
    for i in range(6):
        ps.write_plan({"order": ["k%d" % i]}, **store)
    assert len(ps.history(hist=store["hist"])) == 3


def test_junk_is_refused_rather_than_stored(store):
    assert "__error__" in ps.write_plan("nope", **store)
    assert "__error__" in ps.write_plan({"order": "nope"}, **store)
    assert "__error__" in ps.write_plan({"order": [], "plan": []}, **store)


def test_a_corrupt_file_does_not_stop_the_planner_opening(store):
    with open(store["path"], "w") as fh:
        fh.write("{ not json")
    assert ps.read_plan(store["path"])["order"] == []


def test_a_plan_from_an_unknown_shape_is_not_half_read(store):
    with open(store["path"], "w") as fh:
        json.dump({"v": 99, "order": ["stale"]}, fh)
    assert ps.read_plan(store["path"])["order"] == []


def test_the_first_stored_plan_is_not_thrown_away_by_the_upgrade(store):
    """A v1 record holds a real rank. Dropping it on a schema bump would lose exactly the
    plan this store was added to protect."""
    with open(store["path"], "w") as fh:
        json.dump({"v": 1, "order": ["OINT-1", "OINT-2"], "plan": {"OINT-1": {}},
                   "pods": None, "months": ["2026-10"], "saved": "x", "savedBy": "y"}, fh)
    got = ps.read_plan(store["path"])
    assert got["order"] == ["OINT-1", "OINT-2"]
    assert got["v"] == 1, "it says which shape it is, so a browser knows what it can speak for"
    assert got["cols"] == [] and got["est"] == {}


def test_columns_and_estimates_travel_with_the_plan(store):
    """Everything the planner typed or dragged belongs to the plan, not to one browser:
    columns, per-initiative estimates, who is on each one, and dragged start dates."""
    ps.write_plan({"order": ["OINT-1"], "cols": ["dri", "target"],
                   "est": {"OINT-1": 21}, "heads": {"OINT-1": ["dev-one"]},
                   "schStart": {"OINT-1": "2026-11-04"}}, **store)
    got = ps.read_plan(store["path"])
    assert got["v"] == 2
    assert got["cols"] == ["dri", "target"]
    assert got["est"] == {"OINT-1": 21}
    assert got["heads"] == {"OINT-1": ["dev-one"]}
    assert got["schStart"] == {"OINT-1": "2026-11-04"}


def test_clearing_every_column_is_a_real_choice(store):
    """Columns are not protected the way the order is: showing none is a view someone can
    legitimately want, whereas an empty rank is only ever a mistake."""
    ps.write_plan({"order": ["a"], "cols": ["dri"]}, **store)
    assert ps.write_plan({"order": ["a"], "cols": []}, **store)["ok"]
    assert ps.read_plan(store["path"])["cols"] == []

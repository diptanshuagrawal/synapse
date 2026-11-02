"""Marking leave the team never posted, on every capacity calendar.

The behaviour lives in a node harness (leave_editor_smoke.js), which loads
derived/leave-editor.js for real. This wrapper puts it in the pytest run so it cannot be
forgotten, and separately checks that every page drawing a capacity calendar actually
uses the shared module rather than growing a second one.
"""
import os
import subprocess

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
HARNESS = os.path.join(TESTS, "leave_editor_smoke.js")

# Every page that draws a per-person day grid. A new one belongs here the day it is
# written, or it quietly ships a calendar nobody can correct.
#
# derived/ is gitignored because those pages carry seeded real names and ticket keys;
# only a few are force-added. A page that is not in the checkout is SKIPPED rather than
# failed, because its absence is the repo working as intended, not a regression. The
# pages that ARE here still have to pass, and at least one must be, or this file would
# quietly assert nothing at all.
CALENDARS = ("monthly.html", "sprint-board.html",
             "sprint-planner.html", "sprint-planner-v2.html")


def _page(name):
    path = os.path.join(ROOT, "derived", name)
    if not os.path.exists(path):
        pytest.skip(f"{name} is not in this checkout (derived/ is gitignored)")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_at_least_one_calendar_is_present_to_check():
    """Otherwise every test below skips and the file reports green having read nothing."""
    here = [c for c in CALENDARS if os.path.exists(os.path.join(ROOT, "derived", c))]
    assert here, "no capacity calendar in this checkout, so nothing was actually checked"


def test_leave_editor_behaviour():
    r = subprocess.run(["node", HARNESS], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("page", CALENDARS)
def test_every_capacity_calendar_can_mark_leave(page):
    src = _page(page)
    assert '/leave-editor.js' in src, f"{page} draws a calendar but cannot mark leave"
    assert "LeaveEditor.wire(" in src, f"{page} loads the editor but never wires it"


@pytest.mark.parametrize("page", CALENDARS)
def test_no_page_grew_its_own_copy(page):
    """The cycle lived inline in monthly.html first. Two copies drift, and the one that
    drifts is the one nobody is looking at."""
    src = _page(page)
    assert "LEAVE_CYCLE" not in src, f"{page} has its own copy of the cycle"


@pytest.mark.parametrize("page", CALENDARS)
def test_a_day_cell_offers_itself_through_the_shared_helpers(page):
    """Hand-rolled data attributes would work until the attribute names change."""
    src = _page(page)
    assert "LeaveEditor.attrs(" in src, f"{page} builds its own cell attributes"

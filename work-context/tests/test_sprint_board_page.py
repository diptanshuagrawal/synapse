"""The sprint board as a user drives it: click a cell, type a name, press Enter.

This exists because the board shipped with that exact path dead - the editor opened, the
input cleared, and no task appeared - while every assertion we had still passed, because
they all stopped at layout(). This one loads the shipped HTML into a DOM and uses it.

Skips rather than fails when derived/ is absent (it is gitignored), or when node or jsdom
is missing.
"""
import os
import re
import shutil
import subprocess

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(TESTS, "sprint_board_page.js")
PAGE = os.path.join(TESTS, "..", "derived", "sprint-board.html")


@pytest.fixture(scope="module")
def page():
    if not os.path.exists(PAGE):
        pytest.skip("derived/ is gitignored - no sprint-board.html in this checkout")
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    p = subprocess.run(["node", HARNESS], capture_output=True, text=True, timeout=180)
    if "Cannot find module 'jsdom'" in (p.stderr or ""):
        pytest.skip("jsdom not installed in tests/")
    return p


def test_all_checks_pass(page):
    assert page.returncode == 0, page.stdout + page.stderr


def test_clicking_a_cell_creates_a_task(page):
    """The reported bug, as a test."""
    for line in ("clicking opens an editor in the cell", "Enter creates a task",
                 "the task carries the typed name"):
        assert f"ok  {line}" in page.stdout, page.stdout


def test_an_abandoned_editor_leaves_nothing_behind(page):
    for line in ("Escape closes the editor", "and creates nothing", "a blank name is ignored"):
        assert f"ok  {line}" in page.stdout, page.stdout


def test_a_date_window_can_be_planned_without_a_sprint(page):
    """Planning a cycle Jira has not created yet: the From/To window is what gets saved,
    and no sprint id rides along with it."""
    for line in ("there is a From date input", "there is a To date input",
                 "the window goes with it", "no sprint id is sent in date mode"):
        assert f"ok  {line}" in page.stdout, page.stdout


def test_the_push_snapshots_the_plan_it_is_actually_pushing():
    """Derived tiles carry keys of their own. A snapshot taken before they are added
    never looks those keys up, so the rows come back "not found in Jira" and are skipped
    on every publish. Asserted on source order, because the bug IS the order."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "derive", "sprint_server.py"), encoding="utf-8") as fh:
        src = fh.read()
    # Call sites only: the definition line matches the same text.
    uses = [m.end() for m in re.finditer(r"(?<!def )_with_derived\(plan", src)]
    assert len(uses) >= 2, "both the preview and the single-row refresh must use it"
    for at in uses:
        tail = src[at:at + 1500]
        snap, push = tail.find("_jira_snapshot(plan)"), tail.find("plan_push(plan")
        assert push >= 0, "the derived plan must reach plan_push"
        assert snap >= 0 and snap < push, \
            "the snapshot must be taken AFTER the derived tiles are added"

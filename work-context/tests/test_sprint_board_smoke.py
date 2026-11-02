"""Sprint-board layout, via the node harness that holds the assertions.

The page is a single self-contained HTML file with no build step, so layout() is extracted
from the shipped source and run directly (pure arithmetic over a statuses array — no DOM).
Skips rather than fails when derived/ is absent (it is gitignored) or node is missing.
"""
import os
import shutil
import subprocess

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(TESTS, "sprint_board_smoke.js")
PAGE = os.path.join(TESTS, "..", "derived", "sprint-board.html")


@pytest.fixture(scope="module")
def smoke():
    if not os.path.exists(PAGE):
        pytest.skip("derived/ is gitignored - no sprint-board.html in this checkout")
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    p = subprocess.run(["node", HARNESS], capture_output=True, text=True, timeout=120)
    if p.returncode == 1 and "could not extract" in (p.stderr or ""):
        pytest.fail("layout() could not be extracted from sprint-board.html - renamed?")
    return p


def test_all_checks_pass(smoke):
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr


def test_a_bar_steps_over_the_owners_non_working_days(smoke):
    """The whole reason this view exists: five days of work across two days of leave really
    is seven days on the calendar."""
    for line in ("the weekend is skipped, not consumed",
                 "leave days are stepped over", "on-call skipped", "holiday skipped"):
        assert f"ok  {line}" in smoke.stdout, smoke.stdout


def test_wfh_is_still_a_working_day(smoke):
    assert "ok  wfh counts as available" in smoke.stdout, smoke.stdout


def test_work_that_does_not_fit_is_reported(smoke):
    for line in ("the overflow is counted", "what fits is still drawn"):
        assert f"ok  {line}" in smoke.stdout, smoke.stdout

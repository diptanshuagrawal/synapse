"""Dependency-register copy text (derived/deps.html) — the chase list you send another EM.

The page is a single self-contained HTML file with no build step, so the assertions live
in a node harness (deps_copy_smoke.js) that extracts copyText() from the page and runs it
against fixture rows. copyText is pure string building, so no DOM is needed and node alone
is enough — unlike the monthly planner harness this does not require jsdom.

Skips rather than fails when derived/ is absent (it is gitignored) or node is missing.
"""
import os
import shutil
import subprocess

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(TESTS, "deps_copy_smoke.js")
PAGE = os.path.join(TESTS, "..", "derived", "deps.html")


@pytest.fixture(scope="module")
def smoke():
    if not os.path.exists(PAGE):
        pytest.skip("derived/ is gitignored — no deps.html in this checkout")
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    p = subprocess.run(["node", HARNESS], capture_output=True, text=True, timeout=120)
    if p.returncode == 1 and "could not extract" in (p.stderr or ""):
        pytest.fail("copyText() could not be extracted from deps.html — did it get renamed?")
    return p


def test_harness_present():
    assert os.path.exists(HARNESS), "the node harness holds the actual assertions"


def test_all_checks_pass(smoke):
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr


def test_scorer_matches_the_sop(smoke):
    """1 = no epic, 3 = epic but empty budget field, 10 = both.

    Source: engg-cycle-planner scripts/emplan/scorer.py. Hardcoded here rather than
    imported so this breaks if the planner drifts from the published score.
    """
    for line in ("no epic is 1", "epic but empty budget field is 3",
                 "epic and filled field is 10"):
        assert f"ok  {line}" in smoke.stdout, smoke.stdout


def test_gaps_are_grouped_by_what_is_missing(smoke):
    for line in ("groups it under no delivery epic", "grouped as no budget",
                 "says which cycles need it", "names the epic and the months"):
        assert f"ok  {line}" in smoke.stdout, smoke.stdout


def test_a_zero_budget_scores_ten_but_is_still_surfaced(smoke):
    """The SOP counts a budget field set to 0 as filled, so the number must agree with the
    published sheet while the text still says the pod has committed nothing."""
    for line in ("scores 10, matching the published sheet", "but is still called out",
                 "and says nothing is committed"):
        assert f"ok  {line}" in smoke.stdout, smoke.stdout


def test_an_initiative_is_never_both_ready_and_a_gap(smoke):
    for line in ("and NOT also as ready", "each initiative appears exactly once"):
        assert f"ok  {line}" in smoke.stdout, smoke.stdout


def test_out_of_cycle_rows_keep_their_own_cycle(smoke):
    for line in ("labelled with its own cycle", "not claimed for the viewed months"):
        assert f"ok  {line}" in smoke.stdout, smoke.stdout

"""Monthly Planner board (derived/monthly.html) — DOM behaviour.

The page is a single self-contained HTML file with no build step, so the assertions
live in a jsdom harness (monthly_planner_smoke.js) that stubs every /api/* call and
drives the real handlers. This wrapper runs it under pytest and surfaces its output.

jsdom is dev-only and not required for the rest of the suite:
    npm install --prefix tests
Without it (or without node) these tests skip rather than fail.
"""
import os
import shutil
import subprocess

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(TESTS, "monthly_planner_smoke.js")
PAGE = os.path.join(TESTS, "..", "derived", "monthly.html")


@pytest.fixture(scope="module")
def smoke():
    """Run the jsdom harness once; hand its output to every assertion below."""
    if not os.path.exists(PAGE):
        pytest.skip("derived/ is gitignored — no monthly.html in this checkout")
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    if not os.path.isdir(os.path.join(TESTS, "node_modules", "jsdom")):
        pytest.skip("jsdom not installed — run: npm install --prefix tests")
    p = subprocess.run(["node", HARNESS], capture_output=True, text=True, timeout=120)
    if p.returncode == 2:                     # harness could not start
        pytest.skip(p.stderr.strip())
    return p


def test_harness_present():
    assert os.path.exists(HARNESS), "the jsdom harness holds the actual assertions"


def test_no_failures(smoke):
    failed = [l for l in smoke.stdout.splitlines() if l.startswith("FAIL")]
    assert not failed and smoke.returncode == 0, \
        "\n".join(failed) or smoke.stdout + smoke.stderr


def test_every_section_ran(smoke):
    """Guard against a harness that exits early and still reports green."""
    for section in ("pending vs submitted state",
                    "one-click submit",
                    "Undo restores",
                    "bulk submit all",
                    "column picker: initiative fields",
                    "column picker: epic-level fields",
                    "epic due date is editable",
                    "months are plain numbers, no tick box",
                    "every Jira-backed column edits inline",
                    "linking an epic is one picker",
                    "unlinking an epic",
                    "PODs column in the planner",
                    "Tech DRI filter",
                    "reload from Jira confirms it happened",
                    'never stuck on "Loading',
                    "stack-rank survives a pod filter change"):
        assert section in smoke.stdout, f"section missing from harness output: {section}"


def test_no_blocking_dialogs():
    """Every write path reports through toasts. A confirm()/alert() chain is the exact
    regression this page was reworked to remove, so fail on one reappearing."""
    if not os.path.exists(PAGE):
        pytest.skip("derived/ is gitignored — no monthly.html in this checkout")
    offenders = []
    for n, line in enumerate(open(PAGE, encoding="utf-8"), 1):
        code = line.split("//", 1)[0]          # ignore prose in comments
        if "confirm(" in code or "alert(" in code:
            offenders.append(f"{n}: {line.strip()}")
    assert not offenders, "blocking dialog(s) reintroduced:\n" + "\n".join(offenders)


def test_assertions_actually_ran(smoke):
    oks = [l for l in smoke.stdout.splitlines() if l.strip().startswith("ok ")]
    assert len(oks) >= 150, f"expected the full battery, got {len(oks)} checks"

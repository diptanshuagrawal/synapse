"""Find on page, for every Synapse page at once.

The bar is injected by the shared nav, so this builds that nav exactly as the servers do
and drives it in a DOM. Skips rather than fails when node or jsdom is missing.
"""
import os
import shutil
import subprocess
import sys

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TESTS, "..", "derive"))
HARNESS = os.path.join(TESTS, "find_bar_smoke.js")


@pytest.fixture(scope="module")
def find(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    import synapse_nav
    nav = tmp_path_factory.mktemp("nav") / "nav.html"
    nav.write_text(synapse_nav.build_nav("home"))
    p = subprocess.run(["node", HARNESS, str(nav)], capture_output=True, text=True, timeout=180)
    if "Cannot find module 'jsdom'" in (p.stderr or ""):
        pytest.skip("jsdom not installed in tests/")
    return p


def test_all_checks_pass(find):
    assert find.returncode == 0, find.stdout + find.stderr


def test_a_phrase_spanning_elements_still_matches(find):
    """The difference between this and searching each text node on its own: a ticket key in
    a <b> followed by its title is one phrase to a reader."""
    for line in ("a phrase spanning two elements matches",
                 "the markup's own indentation does not break a match"):
        assert f"ok  {line}" in find.stdout, find.stdout


def test_it_does_not_search_itself_or_the_chrome(find):
    for line in ("script contents are not searched", "the nav itself is not searched",
                 "the find box itself is not searched"):
        assert f"ok  {line}" in find.stdout, find.stdout


def test_it_never_touches_the_page(find):
    """These pages carry drag handlers and repaint themselves; wrapping matches in markup
    would break both."""
    for line in ("no elements were injected into the content",
                 "a repaint is picked up, not left stale"):
        assert f"ok  {line}" in find.stdout, find.stdout


def test_the_bar_is_on_every_page():
    """One injection point, so adding it once puts it everywhere."""
    import synapse_nav
    for active in ("home", "monthly", "sprintboard", ""):
        html = synapse_nav.build_nav(active)
        assert 'id="sfind"' in html
        assert "synapseFind" in html

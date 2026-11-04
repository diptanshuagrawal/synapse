"""Scrolling the page while a native drag is in flight.

The behaviour lives in a node harness (drag_scroll_smoke.js), which loads
derived/drag-scroll.js for real. This wrapper puts it in the pytest run so it cannot be
forgotten, and separately checks that a page dragging with native HTML5 drag-and-drop
actually loads the shared module.

A native drag takes the wheel away: no wheel event reaches the page for the duration.
Any page whose drop target can be off-screen therefore needs this, and the one that does
not has a backlog item you can pick up and nowhere to put it.
"""
import os
import subprocess

import pytest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
HARNESS = os.path.join(TESTS, "drag_scroll_smoke.js")

# Pages that drag with native HTML5 drag-and-drop onto a target that can be off-screen.
#
# derived/ is gitignored because those pages carry seeded real names and ticket keys;
# only a few are force-added. A page that is not in the checkout is SKIPPED rather than
# failed, because its absence is the repo working as intended, not a regression.
DRAG_PAGES = ("sprint-board.html",)


def _page(name):
    path = os.path.join(ROOT, "derived", name)
    if not os.path.exists(path):
        pytest.skip(f"{name} is not in this checkout (derived/ is gitignored)")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_drag_scroll_behaviour():
    r = subprocess.run(["node", HARNESS], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("page", DRAG_PAGES)
def test_a_dragging_page_can_scroll_while_dragging(page):
    src = _page(page)
    assert 'draggable="true"' in src, f"{page} is listed here but drags nothing"
    assert "/drag-scroll.js" in src, (
        f"{page} drags natively, so the wheel is dead for the duration and a drop "
        f"target off-screen cannot be reached"
    )


@pytest.mark.parametrize("page", DRAG_PAGES)
def test_no_page_grew_its_own_copy(page):
    """Two copies drift, and the one that drifts is the one nobody is looking at."""
    src = _page(page)
    assert "function autoScroll" not in src, f"{page} has its own auto-scroll"


def test_the_harness_loads_every_shared_script_the_board_lists():
    """jsdom does not fetch <script src>. A page that gains one and the harness that does
    not gain a matching line test a window that could not have worked in a browser, which
    is how the leave editor's harnesses passed against pages that were broken."""
    board = os.path.join(ROOT, "derived", "sprint-board.html")
    if not os.path.exists(board):
        pytest.skip("sprint-board.html is not in this checkout (derived/ is gitignored)")
    with open(board, encoding="utf-8") as fh:
        src = fh.read()
    import re
    listed = set(re.findall(r'<script src="/([^"]+\.js)"', src))
    with open(os.path.join(TESTS, "sprint_board_page.js"), encoding="utf-8") as fh:
        harness = fh.read()
    missing = sorted(n for n in listed if n not in harness)
    assert not missing, f"sprint_board_page.js never loads: {', '.join(missing)}"

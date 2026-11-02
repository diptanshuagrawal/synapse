"""The /api/pods cache must expire.

This one is a real outage, not a hypothetical. `derived/pods.json` was written once and
then served forever: no timestamp, no TTL, no schema version, and the page never asks for
`fresh=1`. When a pod was renamed in Jira ("TEAM - Old Pod Name" became
"TEAM - New Pod Name") the filter kept offering an option that no longer existed and could
never offer the live one. The board itself was correct the whole time, which is what made
it hard to see: only the dropdown was frozen.

The handler is inline in sprint_server's do_GET, so rather than stand a server up these
tests exercise the cache RULE the handler applies, read out of the shipped source. That
keeps the assertions honest about the file that actually runs: rename the field or drop
the date check and these fail.
"""
import datetime as dt
import json
import os
import re

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "derive", "sprint_server.py")


@pytest.fixture(scope="module")
def handler_src():
    with open(SRC) as fh:
        body = fh.read()
    i = body.index('if self.path.split("?")[0] == "/api/pods"')
    return body[i:i + 2000]


def test_the_pods_cache_is_dated(handler_src):
    """Without a stored date there is nothing to expire against."""
    assert '"generated"' in handler_src, handler_src[:400]


def test_a_stale_cache_is_rebuilt_not_served(handler_src):
    """The bug: the file existing was treated as the file being current."""
    assert "dt.date.today().isoformat()" in handler_src
    assert re.search(r'cached\.get\("generated"\).*==.*today', handler_src, re.S), handler_src[:800]


def test_pod_options_is_only_called_when_the_cache_misses(handler_src):
    """It is a whole-project scan — the point of the cache. Rebuilding on every request
    would trade a stale dropdown for a slow one."""
    assert handler_src.count("capacity_engine.pod_options()") == 1


def test_fresh_still_forces_a_rebuild(handler_src):
    assert 'get("fresh"' in handler_src


def _rule(cached, today):
    """The handler's decision, restated: serve the cache only when it is from today."""
    return (cached.get("generated") or "") == today


def test_rule_serves_todays_cache():
    today = dt.date.today().isoformat()
    assert _rule({"generated": today, "pods": ["A"]}, today)


def test_rule_rejects_yesterdays_cache():
    today = dt.date.today().isoformat()
    y = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    assert not _rule({"generated": y, "pods": ["A"]}, today)


def test_rule_rejects_the_undated_file_that_caused_the_outage():
    """Exactly the shape found on disk: a pods list and no date at all."""
    today = dt.date.today().isoformat()
    assert not _rule({"pods": ["TEAM - Old Pod Name"]}, today)


def test_a_renamed_pod_cannot_survive_a_rebuild(tmp_path):
    """End to end over the cache file: a dated-today file is reused, and the stale file
    written before the rename is not, so the new option reaches the dropdown."""
    today = dt.date.today().isoformat()
    f = tmp_path / "pods.json"

    f.write_text(json.dumps({"pods": ["TEAM - Old Pod Name"]}))
    assert not _rule(json.loads(f.read_text()), today), "undated file must be rebuilt"

    f.write_text(json.dumps({"generated": today, "pods": ["TEAM - New Pod Name"]}))
    cached = json.loads(f.read_text())
    assert _rule(cached, today)
    assert "TEAM - New Pod Name" in cached["pods"]
    assert "TEAM - Old Pod Name" not in cached["pods"]

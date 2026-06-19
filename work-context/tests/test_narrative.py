"""derive/narrative.py — per-person signal builder + render (seed-driven).

narrative (the legacy per-person path) gathers a PersonSignals bundle from the
DB then renders a deterministic signals block + content hash for cache-keying.
build_signals is the bulk; driven on the seed for alice (who authored a PR, owns
a story, edited a page). _person_aliases / _render_signals_block / _content_hash
are pinned too.
"""

from __future__ import annotations

import pytest

from derive import narrative as nar

SINCE = "2026-05-01T00:00:00Z"
PEOPLE = {"alice-gh": {"github": "alice-gh", "email": "alice@example.com",
                       "jira_id": "acc-alice", "slack_id": "U0ALICE", "name": "Alice"}}


def test_person_aliases():
    out = nar._person_aliases(PEOPLE["alice-gh"])
    assert set(out) == {"alice-gh", "alice@example.com", "acc-alice", "U0ALICE"}


def test_build_signals_gathers_authored_work(seeded_db):
    sig = nar.build_signals(seeded_db, "alice-gh", SINCE, window_days=60,
                            verdicts={}, people=PEOPLE, alias_map={})
    assert sig.actor == "alice-gh" and sig.name == "Alice"
    # alice authored the merged PR org/repo#10.
    assert any(p.subject == "org/repo#10" for p in sig.authored_prs)
    # …and owns the story EX-2301.
    assert any(j.key == "EX-2301" for j in sig.jira_owned)


def test_render_signals_block(seeded_db):
    sig = nar.build_signals(seeded_db, "alice-gh", SINCE, window_days=60,
                            verdicts={}, people=PEOPLE, alias_map={})
    block = nar._render_signals_block(sig)
    # the block names the authored PR + owned ticket it was built from.
    assert "org/repo#10" in block and "EX-2301" in block


def test_content_hash_stable_and_sensitive(seeded_db):
    sig = nar.build_signals(seeded_db, "alice-gh", SINCE, window_days=60,
                            verdicts={}, people=PEOPLE, alias_map={})
    h1 = nar._content_hash(sig)
    assert h1 == nar._content_hash(sig)            # stable
    # mutate a hashed field → hash must move (sensitivity).
    sig.authored_prs[0].summary += " (edited)"
    assert nar._content_hash(sig) != h1


def test_build_signals_empty_for_unknown_actor(seeded_db):
    sig = nar.build_signals(seeded_db, "ghost-gh", SINCE, window_days=60,
                            verdicts={}, people={}, alias_map={})
    assert sig.authored_prs == [] and sig.jira_owned == []

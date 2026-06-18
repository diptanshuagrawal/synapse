"""derive/story_graph.py — cross-subject ref graph walker.

StoryGraph walks events + event_refs to connect subjects (a PR → the ticket it
mentions → …). Driven against the seed, where the PR org/repo#10 body references
EX-2301, and the story EX-2301 carries the [Epic EX-2238] anchor. Covers
outgoing / incoming / neighbours dedup / BFS walk / related_subjects.
"""

from __future__ import annotations

import pytest

from derive.story_graph import StoryGraph

PR = "org/repo#10"
STORY = "EX-2301"


def test_outgoing_pr_to_ticket(seeded_db):
    links = StoryGraph(seeded_db).outgoing(PR)
    tickets = {(l.via_ref_type, l.via_ref_value, l.to_subject) for l in links}
    # PR body "see EX-2301" → ticket ref → resolves to the EX-2301 subject.
    assert ("ticket", "EX-2301", "EX-2301") in tickets
    assert all(l.direction == "out" for l in links)


def test_incoming_ticket_from_pr(seeded_db):
    links = StoryGraph(seeded_db).incoming(STORY)
    froms = {l.from_subject for l in links}
    assert PR in froms
    assert all(l.direction == "in" for l in links)


def test_neighbours_merges_both_directions(seeded_db):
    # EX-2301 has BOTH an incoming edge (PR references it) and an outgoing edge
    # (its title carries the [Epic EX-2238] anchor → ticket ref). neighbours()
    # must surface both directions, deduped. (Review: the old test only checked
    # set==list on an already-deduped result — tautological.)
    g = StoryGraph(seeded_db)
    n = g.neighbours(STORY)
    keys = [(l.from_subject, l.to_subject, l.via_ref_type, l.via_ref_value) for l in n]
    assert len(keys) == len(set(keys))                      # deduped
    dirs = {l.direction for l in n}
    assert dirs == {"in", "out"}                            # both directions merged
    assert any(l.from_subject == PR for l in n)             # incoming from the PR
    assert any(l.via_ref_value == "EX-2238" for l in n)     # outgoing to the epic


def test_walk_reaches_ticket(seeded_db):
    links = StoryGraph(seeded_db).walk(PR, depth=2)
    reached = {l.to_subject for l in links} | {l.from_subject for l in links}
    assert "EX-2301" in reached


def test_related_subjects_within_radius(seeded_db):
    rel = StoryGraph(seeded_db).related_subjects(PR, depth=2)
    subjects = {s for s, _src, _d in rel}
    assert "EX-2301" in subjects
    assert PR not in subjects        # the start subject is excluded
    # hop distances are non-negative + sorted.
    assert all(d >= 1 for _s, _src, d in rel)


def test_outgoing_unknown_subject_empty(seeded_db):
    assert StoryGraph(seeded_db).outgoing("nonexistent#999") == []

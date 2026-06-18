"""derive/person_census.py — per-person coverage census.

The census is the V2 discovery layer person_v3 builds on. Pure detectors
(_source_of, _is_oncall_duty, _resolve_canonical) are pinned directly;
_person_role + build_person_census run against the seed (with get_aliases_for
stubbed to the seed roster, since the real one reads the live people.yaml).
"""

from __future__ import annotations

import pytest

from derive import person_census as pc

ALICE = ["alice-gh", "alice@example.com", "acc-alice", "U0ALICE"]
SINCE, UNTIL = "2026-05-01T00:00:00Z", "2026-06-30T00:00:00Z"


# ── _source_of (github_org-aware) ────────────────────────────────────────────

def test_source_of(monkeypatch):
    monkeypatch.setattr(pc, "github_org", lambda: "org")
    assert pc._source_of("slack:C:1") == "slack"
    assert pc._source_of("page:1") == "confluence"
    assert pc._source_of("org/repo#10") == "github"
    assert pc._source_of("EX-2629") == "jira"   # fallthrough


# ── _is_oncall_duty (pure) ───────────────────────────────────────────────────

def test_is_oncall_duty():
    assert pc._is_oncall_duty("[Epic EX-207] Oncall rota", "jira") is True
    assert pc._is_oncall_duty("Shadow Oncall", "confluence") is True
    # only jira/confluence count; a slack/github title never is duty.
    assert pc._is_oncall_duty("oncall", "slack") is False
    assert pc._is_oncall_duty("payout fix", "jira") is False


# ── _resolve_canonical (lookup-driven) ───────────────────────────────────────

def test_resolve_canonical(monkeypatch):
    monkeypatch.setattr(pc, "load_people_lookup", lambda: {"alice@example.com": "alice"})
    assert pc._resolve_canonical("alice@example.com") == "alice"
    assert pc._resolve_canonical("unknown@x.com") == "unknown@x.com"  # passthrough


# ── _person_role (seed) ──────────────────────────────────────────────────────

def test_person_role_author(seeded_db):
    # alice opened the PR / created the story.
    assert pc._person_role(seeded_db, "EX-2301", ALICE) == "author"
    assert pc._person_role(seeded_db, "org/repo#10", ALICE) == "author"


def test_person_role_participant(seeded_db):
    # bob only reviewed org/repo#10 — not author, not assignee.
    assert pc._person_role(seeded_db, "org/repo#10", ["bob-gh"]) == "participant"


# ── build_person_census (seed, stubbed aliases) ──────────────────────────────

def test_build_person_census_computed_values(seeded_db, monkeypatch):
    monkeypatch.setattr(pc, "get_aliases_for", lambda c: ALICE)
    census = pc.build_person_census(seeded_db, "alice", SINCE, UNTIL)
    # assert the COMPUTED census, not just shape (review finding): alice touches
    # all 5 seed subjects, fully classified.
    assert census["totals"]["subjects"] == 5
    assert census["totals"]["represented"] == 5
    assert census["coverage_ok"] is True
    # signal routing: epic + confluence page = 2 design; story Done = delivery;
    # PR = discussion (no domain-owned weight); slack incident thread = incident.
    assert census["own_by_signal"]["design"] == 2
    assert census["own_by_signal"]["delivery"] == 1
    assert census["own_by_signal"]["incident"] == 1

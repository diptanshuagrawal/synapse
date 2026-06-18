"""derive/feature_resolve.py — token→project resolution + artefact gathering.

feature_resolve maps a slug / epic key / name to a Project, then gathers the
feature's artefacts (epics, jira, github, confluence) from the DB. resolve_slug
is pure (pass a Project list); the artefact/epic-child probes run against the
seeded DB (which has epic EX-2238 → child story EX-2301).
"""

from __future__ import annotations

import pytest

from derive import feature_resolve as fr
from derive.feature_resolve import Project

PROJECTS = [
    Project(slug="payments", name="Payments", jira_epics=["EX-2238"]),
    Project(slug="ledger", name="Ledger"),
]


# ── resolve_slug (pure) ──────────────────────────────────────────────────────

def test_resolve_slug_exact():
    assert fr.resolve_slug("payments", PROJECTS).slug == "payments"


def test_resolve_slug_case_insensitive():
    assert fr.resolve_slug("PAYMENTS", PROJECTS).slug == "payments"


def test_resolve_slug_by_epic_key():
    assert fr.resolve_slug("EX-2238", PROJECTS).slug == "payments"


def test_resolve_slug_unknown_epic_is_none():
    assert fr.resolve_slug("EX-9999", PROJECTS) is None


def test_resolve_slug_by_name():
    assert fr.resolve_slug("Ledger", PROJECTS).slug == "ledger"


def test_resolve_slug_no_match():
    assert fr.resolve_slug("totally-unrelated", PROJECTS) is None


# ── FeatureArtefacts.counts ──────────────────────────────────────────────────

def test_feature_artefacts_counts():
    fa = fr.FeatureArtefacts(
        slug="payments", name="Payments", epics=["EX-2238"], jira=["EX-2301"],
        github=["org/repo#10"], confluence=["page:1"], slack=[],
        declared_confluence=[], release_cmrs=[])
    assert fa.counts() == {"epics": 1, "jira": 1, "github": 1, "confluence": 1, "slack": 0}


# ── DB probes on the seed ────────────────────────────────────────────────────

def test_epic_children_finds_prefixed_child(seeded_db):
    # EX-2301 title carries "[Epic EX-2238] …"
    assert fr.epic_children(seeded_db, "EX-2238") == ["EX-2301"]


def test_epic_children_issue_type_filter(seeded_db):
    assert fr.epic_children(seeded_db, "EX-2238", issue_type="Story") == ["EX-2301"]
    assert fr.epic_children(seeded_db, "EX-2238", issue_type="CMR") == []


def test_epic_created_ts(seeded_db):
    assert fr._epic_created_ts(seeded_db, "EX-2238") == "2026-06-01T09:00:00Z"


def test_subjects_by_project_ref_groups_by_source(seeded_db):
    # enrich_refs tagged the seed's payments subjects (keyword/epic). Assert the
    # specific expected subject, not just "something non-empty" (review finding).
    out = fr._subjects_by_project_ref(seeded_db, "payments")
    assert "EX-2301" in out.get("jira", [])
    assert "org/repo#10" in out.get("github", [])

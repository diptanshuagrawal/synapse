"""derive/link_clusters_to_projects.py — deterministic cluster→project linker.

Maps topic clusters to projects.yaml slugs via four rules (domain agreement,
confluence page, jira epic, keyword). All rules are pure functions over
members + the project index, and link_cluster collapses their hits to the
best-confidence entry per slug — pinned here so a re-link can't silently
mis-attribute clusters.
"""

from __future__ import annotations

import pytest

from derive import link_clusters_to_projects as lk

PROJECTS = [
    {"slug": "payments", "name": "Payments", "keywords": ["payout", "withholding"],
     "jira_epics": ["EX-2238"], "confluence_pages": ["123456789"]},
    {"slug": "ledger", "name": "Ledger", "keywords": ["ledger"], "jira_epics": []},
]


@pytest.fixture
def index():
    return lk._build_project_index(PROJECTS)


# ── _build_project_index ─────────────────────────────────────────────────────

def test_build_index_maps(index):
    assert index["epic_to_slug"]["EX-2238"] == "payments"
    assert index["page_to_slug"][123456789] == "payments"
    assert index["keyword_to_slug"]["payout"] == "payments"
    assert index["keyword_to_slug"]["ledger"] == "ledger"


# ── _extract_epic_key ────────────────────────────────────────────────────────

def test_extract_epic_key_from_title():
    assert lk._extract_epic_key("[Epic EX-2238] foo", "EX-2301") == "EX-2238"


def test_extract_epic_key_from_subject():
    # no prefix, but the subject itself is a jira key.
    assert lk._extract_epic_key("plain title", "EX-2238") == "EX-2238"


def test_extract_epic_key_none():
    assert lk._extract_epic_key("no prefix", "org/repo#1") is None


# ── _rule_jira_epic ──────────────────────────────────────────────────────────

def test_rule_jira_epic(index):
    members = [{"subject": "EX-2301", "source": "jira", "title": "[Epic EX-2238] x"}]
    hits = lk._rule_jira_epic(members, index["epic_to_slug"])
    assert len(hits) == 1 and hits[0]["slug"] == "payments" and hits[0]["source"] == "jira_epic"


def test_rule_jira_epic_skips_non_jira(index):
    members = [{"subject": "org/repo#1", "source": "github", "title": "[Epic EX-2238] x"}]
    assert lk._rule_jira_epic(members, index["epic_to_slug"]) == []


# ── _rule_confluence_page ────────────────────────────────────────────────────

def test_rule_confluence_page(index):
    members = [{"subject": "page:123456789", "source": "confluence", "title": ""}]
    hits = lk._rule_confluence_page(members, index["page_to_slug"])
    assert hits[0]["slug"] == "payments" and hits[0]["source"] == "confluence_page"


# ── _rule_keyword ────────────────────────────────────────────────────────────

def test_rule_keyword_matches_label_and_summary(index):
    hits = lk._rule_keyword("Payout outage", "withholding tax issue", index["keyword_to_slug"])
    slugs = {h["slug"] for h in hits}
    assert slugs == {"payments"}


def test_rule_keyword_empty_haystack(index):
    assert lk._rule_keyword(None, None, index["keyword_to_slug"]) == []


# ── link_cluster (collapse best-per-slug) ────────────────────────────────────

def test_link_cluster_collapses_to_best_per_slug(index):
    # jira-epic (high conf) + keyword (lower) both point at payments → one entry.
    members = [{"subject": "EX-2301", "source": "jira", "title": "[Epic EX-2238] payout"}]
    cluster = {"label": "payout work", "summary": ""}
    out = lk.link_cluster(cluster, members, {}, index)
    payments = [c for c in out if c["slug"] == "payments"]
    assert len(payments) == 1
    # the higher-confidence rule (jira_epic) wins the collapse.
    assert payments[0]["confidence"] == lk.CONF_JIRA_EPIC

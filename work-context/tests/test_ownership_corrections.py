"""derive/ownership_corrections.py — content-first ownership helpers.

The author/root probes and the identity tiebreaker (_identity_fallback) decide
who owns a subject when content/chat signals are absent. Driven on the seed;
_identity_fallback takes its identity sets as a param so it runs without the
live roster config. The full correct() pass (config-heavy) is left to integration.
"""

from __future__ import annotations

import pytest

from derive import ownership_corrections as oc


# ── _source_of ───────────────────────────────────────────────────────────────

def test_source_of(monkeypatch):
    monkeypatch.setattr(oc, "github_org", lambda: "org")
    assert oc._source_of("slack:C:1") == "slack"
    assert oc._source_of("page:1") == "confluence"
    assert oc._source_of("org/repo#10") == "github"
    assert oc._source_of("EX-2629") == "jira"


# ── author / root probes on the seed ─────────────────────────────────────────

def test_pr_author(seeded_db):
    assert oc._pr_author(seeded_db, "org/repo#10") == "alice-gh"
    assert oc._pr_author(seeded_db, "EX-2301") is None        # not a PR


def test_doc_author(seeded_db):
    assert oc._doc_author(seeded_db, "page:123456789") == "acc-alice"


def test_root_event(seeded_db):
    title, body, actor, etypes = oc._root_event(seeded_db, "slack:C0A:1700000000.000100")
    assert actor == "U0ALICE"                                  # thread_started author
    assert "thread_started" in etypes
    assert "payout" in (body or "").lower()


def test_root_event_unknown(seeded_db):
    assert oc._root_event(seeded_db, "nope") == ("", "", "", set())


# ── _identity_fallback (idn passed in) ───────────────────────────────────────

def test_identity_fallback_github_home_roster(seeded_db):
    idn = {"pots_github": set(), "team_github": {"alice-gh"}}
    primary, co, reason = oc._identity_fallback(seeded_db, "org/repo#10", "github", idn)
    assert primary == oc.HOME_TEAM and reason and "PR by alice-gh" in reason


def test_identity_fallback_github_coowner(seeded_db):
    idn = {"pots_github": {"alice-gh"}, "team_github": set()}
    primary, co, reason = oc._identity_fallback(seeded_db, "org/repo#10", "github", idn)
    assert primary == oc.POTS_TEAM and co == [oc.HOME_TEAM]


def test_identity_fallback_jira_author(seeded_db):
    idn = {"author_roster": {"alice@example.com"}}
    # EX-2301 issue_created actor is alice@example.com.
    primary, co, reason = oc._identity_fallback(seeded_db, "EX-2301", "jira", idn)
    assert primary == oc.HOME_TEAM


def test_identity_fallback_none_when_unmatched(seeded_db):
    idn = {"pots_github": set(), "team_github": set()}
    assert oc._identity_fallback(seeded_db, "org/repo#10", "github", idn) == (None, [], None)

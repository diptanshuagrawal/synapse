"""derive/ownership_resolve.py — content-first ownership resolution.

Ownership is resolved from a subject's classified domains (content), falling
back to the chat verdict, then to nothing. resolve() is pure (takes the
domain→team map as an arg), so the whole decision tree is pinned here.
"""

from __future__ import annotations

import pytest

from derive import ownership_resolve as orv

MAP = {
    "default_team": "home-team",
    "overrides": {
        "payments": {"primary": "payments-team", "co": ["risk-team"]},
        "fraud": {"primary": "risk-team", "co": []},
    },
    "review": ["ledger"],
}


# ── _team_for_domain ─────────────────────────────────────────────────────────

def test_team_for_domain_override():
    assert orv._team_for_domain("payments", MAP) == ("payments-team", ["risk-team"])


def test_team_for_domain_default():
    assert orv._team_for_domain("unmapped-slug", MAP) == ("home-team", [])


# ── resolve: content basis ───────────────────────────────────────────────────

def test_resolve_content_primary_and_co():
    primary, co, basis = orv.resolve(["payments"], None, None, MAP)
    assert primary == "payments-team" and co == ["risk-team"] and basis == "content"


def test_resolve_content_multi_domain_adds_co_owners():
    # first domain dominant; a second domain's team becomes a co-owner.
    primary, co, basis = orv.resolve(["payments", "fraud"], None, None, MAP)
    assert primary == "payments-team" and "risk-team" in co and basis == "content"


def test_resolve_content_ignores_primary_in_co():
    primary, co, _ = orv.resolve(["fraud", "fraud"], None, None, MAP)
    assert primary == "risk-team" and "risk-team" not in co


# ── resolve: chat fallback + none ────────────────────────────────────────────

def test_resolve_falls_back_to_chat_when_no_domains():
    primary, co, basis = orv.resolve([], "ml-team", ["data-team"], MAP)
    assert primary == "ml-team" and co == ["data-team"] and basis == "chat"


def test_resolve_none_when_nothing():
    assert orv.resolve([], None, None, MAP) == (None, [], "none")


def test_resolve_drops_empty_domains():
    # falsy domain entries are filtered; empty → chat/none path.
    assert orv.resolve([""], None, None, MAP)[2] == "none"


# ── review_slugs ─────────────────────────────────────────────────────────────

def test_review_slugs():
    assert orv.review_slugs(MAP) == ["ledger"]

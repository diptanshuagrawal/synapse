"""derive/build_trd_owners.py — TRD detection + ownership scoring (pure).

build_trd_owners materialises per-doc owner/contributor attribution. The scoring
core is pure: is_trd (declared page or TRD-shaped title), score_page (weighted
created/updated/comment per resolved actor), and derive_owner_and_contributors
(top scorer + ≥30% contributors). fetch_trd_events (config-driven) is left out.
"""

from __future__ import annotations

import pytest

from derive import build_trd_owners as bto


# ── is_trd ───────────────────────────────────────────────────────────────────

def test_is_trd_declared_page():
    assert bto.is_trd("anything", "123", {"123": "payments"}) == (True, "payments")


@pytest.mark.parametrize("title", ["Payments TRD", "Tech Spec: ledger", "Technical Design Doc"])
def test_is_trd_by_title(title):
    ok, slug = bto.is_trd(title, "999", {})
    assert ok is True and slug is None


def test_is_trd_negative():
    assert bto.is_trd("Sprint planning notes", "999", {}) == (False, None)


# ── score_page ───────────────────────────────────────────────────────────────

def test_score_page_weights():
    events = [
        {"actor": "alice@x.com", "event_type": "page_created", "ts": "2026-06-01T00:00:00Z"},
        {"actor": "alice@x.com", "event_type": "page_updated", "ts": "2026-06-02T00:00:00Z"},
        {"actor": "bob@x.com", "event_type": "comment", "ts": "2026-06-03T00:00:00Z"},
        {"actor": "ghost@x.com", "event_type": "comment", "ts": "2026-06-04T00:00:00Z"},  # unresolved
    ]
    lookup = {"alice@x.com": "alice", "bob@x.com": "bob"}
    out = bto.score_page(events, lookup)
    assert out["scores"]["alice"] == bto.W_CREATED + bto.W_UPDATED   # 10 + 3
    assert out["scores"]["bob"] == bto.W_COMMENT                      # 1
    assert "ghost@x.com" not in out["scores"]                        # unresolved skipped
    assert out["total_events"] == 4 and out["last_ts"] == "2026-06-04T00:00:00Z"


# ── derive_owner_and_contributors ────────────────────────────────────────────

def test_derive_owner_and_contributors():
    owner, score, contribs = bto.derive_owner_and_contributors(
        {"alice": 13.0, "bob": 5.0, "carol": 1.0})
    assert owner == "alice" and score == 13.0
    assert "bob" in contribs                 # 5 ≥ 30% of 13
    assert "carol" not in contribs           # 1 < 30% of 13


def test_derive_owner_empty():
    assert bto.derive_owner_and_contributors({}) == (None, 0.0, [])

"""derive/apply_verdicts._validate — chat-verdict gatekeeper.

_validate is the guard between the chat-LLM classification output and the
subject_summary table: it rejects stale/mismatched verdicts, drops invalid
slugs/risk-flags, clamps confidence, applies the epic anchor, and nulls
low-confidence or off-team ownership. Every rejection path is a data-integrity
guarantee, so each is pinned here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_DERIVE = Path(__file__).resolve().parent.parent / "derive"
if str(_DERIVE) not in sys.path:
    sys.path.insert(0, str(_DERIVE))

from derive import apply_verdicts as av  # noqa: E402

SLUGS = {"payments", "ledger"}
EPIC_TO_SLUG = {"EX-2238": "payments"}
TEAM = {"alice", "bob"}


def _pending(content_hash="h1", epic_key=""):
    return {"org/repo#1": {"content_hash": content_hash, "epic_key": epic_key}}


def _verdict(**kw):
    base = {"subject": "org/repo#1", "content_hash": "h1", "domains": ["payments"],
            "summary": "s", "confidence": 0.9}
    base.update(kw)
    return base


# ── rejection paths ──────────────────────────────────────────────────────────

def test_missing_subject_or_hash_rejected():
    cleaned, errs = av._validate({"subject": "x"}, _pending(), SLUGS, EPIC_TO_SLUG, TEAM)
    assert cleaned is None and errs


def test_subject_not_in_pending_rejected():
    v = _verdict(subject="ghost#9")
    cleaned, errs = av._validate(v, _pending(), SLUGS, EPIC_TO_SLUG, TEAM)
    assert cleaned is None and any("not in pending" in e for e in errs)


def test_content_hash_mismatch_rejected():
    cleaned, errs = av._validate(_verdict(content_hash="WRONG"), _pending("h1"),
                                 SLUGS, EPIC_TO_SLUG, TEAM)
    assert cleaned is None and any("content_hash" in e for e in errs)


# ── cleaning paths ───────────────────────────────────────────────────────────

def test_invalid_slugs_dropped():
    cleaned, errs = av._validate(_verdict(domains=["payments", "bogus"]),
                                 _pending(), SLUGS, EPIC_TO_SLUG, TEAM)
    assert cleaned["domains"] == ["payments"] and any("invalid slugs" in e for e in errs)


def test_invalid_risk_flags_dropped():
    cleaned, _ = av._validate(_verdict(risk_flags=["security", "nonsense"]),
                              _pending(), SLUGS, EPIC_TO_SLUG, TEAM)
    assert cleaned["risk_flags"] == ["security"]


def test_summary_truncated():
    cleaned, errs = av._validate(_verdict(summary="z" * 500),
                                 _pending(), SLUGS, EPIC_TO_SLUG, TEAM)
    assert len(cleaned["summary"]) <= av.SUMMARY_MAX


def test_confidence_clamped():
    cleaned, _ = av._validate(_verdict(confidence=5.0), _pending(), SLUGS, EPIC_TO_SLUG, TEAM)
    assert cleaned["confidence"] == 1.0


def test_epic_anchor_prepended():
    # pending has epic EX-2238 → payments must lead even though model said ledger.
    cleaned, _ = av._validate(_verdict(domains=["ledger"]),
                              _pending(epic_key="EX-2238"), SLUGS, EPIC_TO_SLUG, TEAM)
    assert cleaned["domains"][0] == "payments"


# ── ownership nulling ────────────────────────────────────────────────────────

def test_unknown_owner_nulled():
    v = _verdict(owned_by_primary="stranger", owned_by_confidence=0.9)
    cleaned, errs = av._validate(v, _pending(), SLUGS, EPIC_TO_SLUG, TEAM)
    assert cleaned["owned_by_primary"] is None


def test_low_ownership_confidence_nulled():
    v = _verdict(owned_by_primary="alice", owned_by_confidence=0.3)
    cleaned, errs = av._validate(v, _pending(), SLUGS, EPIC_TO_SLUG, TEAM)
    assert cleaned["owned_by_primary"] is None and cleaned["owned_by_confidence"] == 0.0


def test_valid_ownership_kept():
    v = _verdict(owned_by_primary="alice", owned_by_confidence=0.9, co_owners=["bob", "x"])
    cleaned, _ = av._validate(v, _pending(), SLUGS, EPIC_TO_SLUG, TEAM)
    assert cleaned["owned_by_primary"] == "alice"
    assert cleaned["co_owners"] == ["bob"]  # off-team 'x' dropped


def test_needs_diff_flag_passthrough():
    cleaned, _ = av._validate(_verdict(needs_diff=True), _pending(), SLUGS, EPIC_TO_SLUG, TEAM)
    assert cleaned["needs_diff"] is True

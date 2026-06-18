"""derive/apply_pr_classes._validate + _is_locked — PR-comment class gate.

_validate guards pr_comment_class writes: stale verdicts, off-taxonomy
categories, and low-confidence are rejected; source is carried from the dump
(never trusted from chat). _is_locked classifies the sqlite retry condition.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_DERIVE = Path(__file__).resolve().parent.parent / "derive"
if str(_DERIVE) not in sys.path:
    sys.path.insert(0, str(_DERIVE))

from derive import apply_pr_classes as ap  # noqa: E402

# A real category from the taxonomy (CATEGORY_WEIGHTS).
CAT = next(iter(ap.VALID_CATEGORIES))
PENDING = {"e1": {"subject": "org/repo#1", "source": "human"}}


def _v(**kw):
    base = {"event_id": "e1", "category": CAT, "confidence": 0.9}
    base.update(kw)
    return base


def test_missing_event_id():
    cleaned, err = ap._validate({"category": CAT, "confidence": 0.9}, PENDING)
    assert cleaned is None and err


def test_stale_verdict():
    cleaned, err = ap._validate(_v(event_id="ghost"), PENDING)
    assert cleaned is None and "not in pending" in err


def test_bad_category():
    cleaned, err = ap._validate(_v(category="not-a-category"), PENDING)
    assert cleaned is None and "bad category" in err


def test_low_confidence():
    cleaned, err = ap._validate(_v(confidence=0.2), PENDING)
    assert cleaned is None and "confidence" in err


def test_happy_path_carries_source_from_pending():
    cleaned, err = ap._validate(_v(), PENDING)
    assert err is None
    assert cleaned["subject"] == "org/repo#1"
    assert cleaned["source"] == "human"      # from dump, not chat
    assert cleaned["category"] == CAT


def test_category_case_insensitive():
    cleaned, err = ap._validate(_v(category=CAT.upper()), PENDING)
    assert cleaned is not None and cleaned["category"] == CAT


# ── _is_locked ───────────────────────────────────────────────────────────────

def test_is_locked():
    assert ap._is_locked(Exception("database is locked"))
    assert ap._is_locked(Exception("database is BUSY"))
    assert not ap._is_locked(Exception("syntax error"))

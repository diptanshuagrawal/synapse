"""derive/meetings/correct.py — post-ASR fuzzy vocabulary correction.

Pins the conservative correction rules: length + stop-word gating, the
exact-match guard (a correct token must never be 'corrected' to a near vocab
term — the 2026-07-18 bug), and the owner phrase map. Vocab is injected so these
never read the live roster/config.
"""

from __future__ import annotations

import re

import pytest

from derive.meetings import correct


@pytest.fixture(autouse=True)
def _fixed_vocab(monkeypatch):
    # Deterministic, fake vocabulary — never the real roster.
    vocab = (("Alexander", 0.72), ("ledger", 0.84))
    monkeypatch.setattr(correct, "_vocab", lambda: vocab)
    monkeypatch.setattr(correct, "_phrase_map", lambda: ())


def test_fixes_phonetically_misheard_name():
    assert correct.correct_text("saw Alexandr yesterday") == "saw Alexander yesterday"


def test_exact_known_term_is_never_corrected():
    # The 2026-07-18 bug: a correct token got 'corrected' to a similar vocab term
    # because the exact match was excluded from candidates. Guard must hold.
    assert correct.correct_text("check the ledger") == "check the ledger"


def test_common_words_and_short_tokens_untouched():
    assert correct.correct_text("about the account") == "about the account"
    assert correct.correct_text("Yash") == "Yash"  # len < 5 → never fuzzy-matched


def test_phrase_map_applies_first(monkeypatch):
    monkeypatch.setattr(
        correct, "_phrase_map",
        lambda: ((re.compile(re.escape("acme plus"), re.I), "AcmePlus"),),
    )
    assert correct.correct_text("we use Acme Plus daily") == "we use AcmePlus daily"

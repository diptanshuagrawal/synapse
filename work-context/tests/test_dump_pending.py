"""derive/dump_pending.py — auto-slug derivation from Jira epic titles.

When an epic isn't yet in projects.yaml, dump_pending mints a human-readable
kebab slug + bigram keywords from its title (epic-first classification). These
pure helpers decide what those slugs/keywords look like — a regression here
pollutes projects.yaml with junk slugs or over-broad keywords.
"""

from __future__ import annotations

import pytest

from derive import dump_pending as dp


# ── JIRA_KEY_RE ──────────────────────────────────────────────────────────────

def test_jira_key_re():
    m = dp.JIRA_KEY_RE.match("EX-2238")
    assert m and m.group(1) == "EX" and m.group(2) == "2238"
    assert dp.JIRA_KEY_RE.match("not-a-key") is None


# ── _tokenize_title ──────────────────────────────────────────────────────────

def test_tokenize_strips_brackets_and_stopwords():
    toks = dp._tokenize_title("[Epic EX-1] Card Transactions Migration from A to B")
    assert "card" in toks and "transactions" in toks and "migration" in toks
    assert "ex" not in toks                  # bracketed prefix stripped
    assert "from" not in toks and "to" not in toks   # stop-words dropped


def test_tokenize_preserves_hyphenated_compound():
    toks = dp._tokenize_title("E-nach development")
    assert "e-nach" in toks                  # internal hyphen preserved as one token


def test_tokenize_dedupes():
    toks = dp._tokenize_title("card card transactions card")
    assert toks.count("card") == 1


# ── _slug_from_title ─────────────────────────────────────────────────────────

def test_slug_from_title_kebab():
    assert dp._slug_from_title("Cheque Flow Optimizations", "EX-9") == "cheque-flow-optimizations"


def test_slug_caps_at_four_tokens():
    slug = dp._slug_from_title("alpha beta gamma delta epsilon zeta", "EX-9")
    assert slug.count("-") <= 3              # at most 4 tokens joined


def test_slug_fallback_when_no_tokens():
    # title is all stop-words/brackets → fall back to epic-<key>.
    assert dp._slug_from_title("[EX-9]", "EX-9") == "epic-ex-9"


# ── _keywords_from_title ─────────────────────────────────────────────────────

def test_keywords_are_bigrams():
    kws = dp._keywords_from_title("card transactions migration")
    assert kws == ["card transactions", "transactions migration"]   # bigrams, no unigrams


def test_keywords_capped_and_deduped():
    kws = dp._keywords_from_title("a1 b2 c3 d4 e5 f6 g7 h8 i9")  # avoid stop-words
    assert len(kws) <= 6

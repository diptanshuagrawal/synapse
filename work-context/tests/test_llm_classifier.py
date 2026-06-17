"""derive/llm_classifier.py — pure classification helpers (no live LLM).

The classifier's network call is mocked out of scope; what's pinned here is the
deterministic logic around it: content hashing (cache key), the keyword
fallback, epic→slug anchoring, and parsing/validating the tool-call verdict.
A regression in any of these silently mis-tags or mis-caches work.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# llm_classifier uses bare sibling imports (`import diff_fetcher`), so derive/
# must be on sys.path — same as its runtime (rollup runs with derive/ on path).
_DERIVE = Path(__file__).resolve().parent.parent / "derive"
if str(_DERIVE) not in sys.path:
    sys.path.insert(0, str(_DERIVE))

from derive import llm_classifier as lc
from derive.llm_classifier import SubjectInput, SubjectVerdict

PROJECTS = [
    {"slug": "payments", "keywords": ["payout", "withholding"], "jira_epics": ["EX-2238"]},
    {"slug": "ledger", "keywords": ["ledger"], "jira_epics": ["EX-9"]},
]
SLUGS = {"payments", "ledger"}


# ── _trunc ─────────────────────────────────────────────────────────────────

def test_trunc_under_cap_unchanged():
    assert lc._trunc("hello", 10) == "hello"


def test_trunc_over_cap_adds_ellipsis():
    out = lc._trunc("abcdefgh", 3)
    assert out == "abc…" and out != "abcdefgh"


def test_trunc_none_safe():
    assert lc._trunc(None, 5) == ""


# ── _content_hash ─────────────────────────────────────────────────────────

def test_content_hash_deterministic_and_32():
    s = SubjectInput(subject="X#1", source="github", title="t", body="b")
    h1 = lc._content_hash(s)
    h2 = lc._content_hash(s)
    assert h1 == h2 and len(h1) == 32


def test_content_hash_changes_with_diff_and_title():
    s = SubjectInput(subject="X#1", source="github", title="t", body="b")
    base = lc._content_hash(s)
    assert lc._content_hash(s, with_diff="some diff") != base
    s2 = SubjectInput(subject="X#1", source="github", title="DIFFERENT", body="b")
    assert lc._content_hash(s2) != base


# ── _fallback_classify ──────────────────────────────────────────────────────

def test_fallback_keyword_hit():
    s = SubjectInput(subject="X#1", source="github", title="fix payout bug")
    v = lc._fallback_classify(s, PROJECTS)
    assert v.domains == ["payments"] and v.source == "fallback"


def test_fallback_epic_anchor_hit():
    s = SubjectInput(subject="EX-100", source="jira", title="unrelated", epic_key="EX-2238")
    v = lc._fallback_classify(s, PROJECTS)
    assert "payments" in v.domains


def test_fallback_no_hit_empty_domains():
    s = SubjectInput(subject="X#1", source="github", title="lunch menu")
    assert lc._fallback_classify(s, PROJECTS).domains == []


def test_fallback_summary_truncated():
    s = SubjectInput(subject="X#1", source="github", title="z" * 300)
    assert len(lc._fallback_classify(s, PROJECTS).summary) <= 180


# ── _build_epic_to_slug ─────────────────────────────────────────────────────

def test_build_epic_to_slug_first_wins():
    projects = [
        {"slug": "a", "jira_epics": ["EX-1"]},
        {"slug": "b", "jira_epics": ["EX-1"]},  # later claim ignored
    ]
    assert lc._build_epic_to_slug(projects) == {"EX-1": "a"}


# ── _apply_epic_anchor ──────────────────────────────────────────────────────

def test_apply_epic_anchor_inserts_first():
    v = SubjectVerdict(domains=["ledger"], summary="")
    out = lc._apply_epic_anchor(v, "EX-2238", {"EX-2238": "payments"})
    assert out.domains[0] == "payments"


def test_apply_epic_anchor_moves_existing_to_front():
    v = SubjectVerdict(domains=["ledger", "payments"], summary="")
    out = lc._apply_epic_anchor(v, "EX-2238", {"EX-2238": "payments"})
    assert out.domains == ["payments", "ledger"]


def test_apply_epic_anchor_noop_without_epic():
    v = SubjectVerdict(domains=["ledger"], summary="")
    assert lc._apply_epic_anchor(v, "", {"EX-2238": "payments"}).domains == ["ledger"]


# ── _verdict_from_tool ──────────────────────────────────────────────────────

def test_verdict_from_tool_filters_unknown_domains():
    v = lc._verdict_from_tool(
        {"domains": ["payments", "bogus"], "summary": "s", "confidence": 0.9}, SLUGS)
    assert v.domains == ["payments"] and v.confidence == 0.9 and v.source == "claude"


def test_verdict_from_tool_truncates_summary_and_detail():
    v = lc._verdict_from_tool(
        {"domains": [], "summary": "s" * 300, "detail": "d" * 900}, SLUGS)
    assert len(v.summary) <= 200 and len(v.detail) <= 800


# ── _parse_tool_calls ───────────────────────────────────────────────────────

def test_parse_tool_calls_extracts_by_subject():
    resp = {"content": [
        SimpleNamespace(type="tool_use", name="classify", input={"subject": "X#1", "domains": ["payments"]}),
        SimpleNamespace(type="text", text="ignored"),
        SimpleNamespace(type="tool_use", name="classify", input={"domains": []}),  # no subject → skipped
    ]}
    out = lc._parse_tool_calls(resp)
    assert set(out) == {"X#1"} and out["X#1"]["input"]["domains"] == ["payments"]


# ── extract_epic_key ────────────────────────────────────────────────────────

def test_extract_epic_key():
    assert lc.extract_epic_key("[Epic EX-2238] do it") == "EX-2238"
    assert lc.extract_epic_key("no epic here") == ""
    assert lc.extract_epic_key("") == ""

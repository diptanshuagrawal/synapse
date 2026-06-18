"""derive/llm_classifier.classify_subjects — full branch tree (no network).

classify_subjects is the central tagging path: cache → no-creds fallback →
Claude pass-1 → diff-fetch pass-2. Every branch is exercised with the
fake_anthropic client, a temp db, and monkeypatched env/diff_fetcher — so a
regression in caching, fallback, escalation, or the abort-on-API-failure
contract fails here instead of silently mis-tagging months of work.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_DERIVE = Path(__file__).resolve().parent.parent / "derive"
if str(_DERIVE) not in sys.path:
    sys.path.insert(0, str(_DERIVE))

import anthropic  # noqa: E402
from derive import llm_classifier as lc  # noqa: E402
from derive.llm_classifier import SubjectInput  # noqa: E402

PROJECTS = [
    {"slug": "payments", "name": "Payments", "keywords": ["payout"], "jira_epics": ["EX-2238"]},
    {"slug": "ledger", "name": "Ledger", "keywords": ["ledger"], "jira_epics": []},
]


@pytest.fixture
def conn():
    import sqlite3
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


def _gh(subject="org/repo#1", title="payout fix", body=""):
    return SubjectInput(subject=subject, source="github", title=title, body=body)


def _no_creds(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def _with_creds_and_client(monkeypatch, client):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: client)


# ── no-credentials fallback ─────────────────────────────────────────────────

def test_no_creds_uses_keyword_fallback(conn, monkeypatch):
    _no_creds(monkeypatch)
    verdicts, stats = lc.classify_subjects(conn, [_gh(title="fix payout")], PROJECTS)
    v = verdicts["org/repo#1"]
    assert v.source == "fallback" and v.domains == ["payments"]
    assert stats.fallback == 1
    # persisted to subject_summary.
    assert conn.execute("SELECT COUNT(*) FROM subject_summary").fetchone()[0] == 1


def test_cache_hit_on_second_run(conn, monkeypatch):
    _no_creds(monkeypatch)
    subj = [_gh(title="fix payout")]
    lc.classify_subjects(conn, subj, PROJECTS)
    _, stats2 = lc.classify_subjects(conn, subj, PROJECTS)
    assert stats2.cache_hits == 1 and stats2.fallback == 0


# ── Claude pass-1 ────────────────────────────────────────────────────────────

def test_claude_pass1_verdict(conn, monkeypatch, fake_anthropic):
    client = fake_anthropic({"org/repo#1": {"domains": ["payments"], "summary": "s", "confidence": 0.9}})
    _with_creds_and_client(monkeypatch, client)
    verdicts, stats = lc.classify_subjects(conn, [_gh()], PROJECTS)
    v = verdicts["org/repo#1"]
    assert v.source == "claude" and v.domains == ["payments"] and v.confidence == 0.9
    assert stats.claude_pass1 == 1


def test_claude_filters_unknown_domains(conn, monkeypatch, fake_anthropic):
    client = fake_anthropic({"org/repo#1": {"domains": ["payments", "bogus"], "summary": "s", "confidence": 0.9}})
    _with_creds_and_client(monkeypatch, client)
    verdicts, _ = lc.classify_subjects(conn, [_gh()], PROJECTS)
    assert verdicts["org/repo#1"].domains == ["payments"]


def test_no_tool_call_falls_back(conn, monkeypatch, fake_anthropic):
    # Model emits nothing for the subject → fallback, not dropped.
    client = fake_anthropic({})
    _with_creds_and_client(monkeypatch, client)
    verdicts, stats = lc.classify_subjects(conn, [_gh(title="fix payout")], PROJECTS)
    assert verdicts["org/repo#1"].source == "fallback" and stats.fallback == 1


def test_epic_anchor_forces_slug_first(conn, monkeypatch, fake_anthropic):
    # Model returns only 'ledger'; the epic EX-2238 → payments must be prepended.
    client = fake_anthropic({"EX-2301": {"domains": ["ledger"], "summary": "s", "confidence": 0.9}})
    _with_creds_and_client(monkeypatch, client)
    s = SubjectInput(subject="EX-2301", source="jira", title="x", epic_key="EX-2238")
    verdicts, _ = lc.classify_subjects(conn, [s], PROJECTS)
    assert verdicts["EX-2301"].domains[0] == "payments"


# ── fallback upgrade when creds arrive ──────────────────────────────────────

def test_fallback_row_upgraded_when_creds_present(conn, monkeypatch, fake_anthropic):
    # 1st run: no creds → fallback row persisted.
    _no_creds(monkeypatch)
    lc.classify_subjects(conn, [_gh(title="fix payout")], PROJECTS)
    # 2nd run: creds present → fallback cache treated as miss, re-classified.
    client = fake_anthropic({"org/repo#1": {"domains": ["payments"], "summary": "s", "confidence": 0.95}})
    _with_creds_and_client(monkeypatch, client)
    verdicts, stats = lc.classify_subjects(conn, [_gh(title="fix payout")], PROJECTS)
    assert stats.claude_pass1 == 1 and verdicts["org/repo#1"].source == "claude"


# ── diff-fetch pass 2 ───────────────────────────────────────────────────────

def test_request_diff_escalates_to_pass2(conn, monkeypatch, fake_anthropic):
    # pass1 returns request_diff; pass2 returns a real verdict.
    client = fake_anthropic(
        {"org/repo#1": {"_tool": "request_diff"}},                                   # call 1 (pass1)
        {"org/repo#1": {"domains": ["payments"], "summary": "s", "confidence": 0.9}},  # call 2 (pass2)
    )
    _with_creds_and_client(monkeypatch, client)
    # stub the diff fetcher so pass2 has a diff to feed.
    monkeypatch.setattr(lc.diff_fetcher, "fetch_diff",
                        lambda subj: type("D", (), {"to_text": lambda self: "+ diff"})())
    verdicts, stats = lc.classify_subjects(conn, [_gh()], PROJECTS)
    assert stats.diff_fetched == 1 and stats.claude_pass2 == 1
    assert verdicts["org/repo#1"].domains == ["payments"]


def test_request_diff_on_non_github_falls_back(conn, monkeypatch, fake_anthropic):
    client = fake_anthropic({"EX-9": {"_tool": "request_diff"}})
    _with_creds_and_client(monkeypatch, client)
    s = SubjectInput(subject="EX-9", source="jira", title="ledger work")
    verdicts, stats = lc.classify_subjects(conn, [s], PROJECTS)
    assert verdicts["EX-9"].source == "fallback" and stats.fallback == 1


# ── abort on API failure ────────────────────────────────────────────────────

def test_api_failure_raises(conn, monkeypatch, fake_anthropic):
    _with_creds_and_client(monkeypatch, fake_anthropic({}))
    # _call_claude returning None == retries exhausted → must abort, not mis-tag.
    monkeypatch.setattr(lc, "_call_claude", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        lc.classify_subjects(conn, [_gh()], PROJECTS)

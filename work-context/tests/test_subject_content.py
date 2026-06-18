"""derive/subject_content.py — embeddable-content extraction per source.

subject_content turns a subject into the text that gets embedded + a stable
content hash for drift detection. detect_source / content_sha / _truncate are
pure; get_content + the per-source extractors run against the seeded DB (one
subject per source). A regression here silently changes what gets embedded.
"""

from __future__ import annotations

import pytest

from derive import subject_content as sc
from tests.conftest import SEED_SUBJECTS


# ── detect_source (pure) ─────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,expected", [
    ("service:example-svc#overview", "service"),   # checked before github (#+/)
    ("slack:C0A:1700000000.1", "slack"),
    ("page:123", "confluence"),
    ("org/repo#10", "github"),
    ("EX-2629", "jira"),
    ("garbage", "unknown"),
])
def test_detect_source(subject, expected):
    assert sc.detect_source(subject) == expected


# ── _truncate / content_sha (pure) ───────────────────────────────────────────

def test_truncate():
    assert sc._truncate("hello", 10) == "hello"           # under cap unchanged
    out = sc._truncate("x" * 100, 10)
    assert out.startswith("x" * 10) and "…" in out and len(out) < 100  # truncated


def test_truncate_none():
    assert sc._truncate(None, 5) == ""


def test_content_sha_stable_and_changes():
    a = sc.content_sha("same text")
    assert a == sc.content_sha("same text") and len(a) == 16
    assert a != sc.content_sha("different text")


# ── get_content on the seed (one subject per source) ─────────────────────────

def test_get_content_jira(seeded_db):
    source, content = sc.get_content(seeded_db, SEED_SUBJECTS["story"])
    # assert the actual extracted text, not just non-empty (review finding).
    assert source == "jira" and "payout" in content.lower()


def test_get_content_github(seeded_db):
    source, content = sc.get_content(seeded_db, SEED_SUBJECTS["pr"])
    assert source == "github" and "payout" in content.lower()


def test_get_content_confluence(seeded_db):
    source, content = sc.get_content(seeded_db, SEED_SUBJECTS["page"])
    assert source == "confluence" and "ledger" in content.lower()


def test_get_content_slack(seeded_db):
    source, content = sc.get_content(seeded_db, SEED_SUBJECTS["thread"])
    # slack content folds in the parent + replies.
    assert source == "slack" and "payout" in content.lower()


def test_get_content_unknown_subject(seeded_db):
    source, content = sc.get_content(seeded_db, "nonsense-subject-xyz")
    assert content == ""

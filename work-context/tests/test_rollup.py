"""derive/rollup.py — pure parsing/format helpers.

rollup is the main derivation driver; its big build_* functions are DB-driven
(exercised end-to-end), but the small pure helpers below feed every report and
are where a silent format/parse regression would corrupt output: PR-review
summary extraction, domain detection, subject-source classification, severity
counting, and the time/percentile math.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# rollup uses bare sibling imports (import llm_classifier / narrative /
# sources_config) — derive/ must be on sys.path, mirroring its runtime.
_DERIVE = Path(__file__).resolve().parent.parent / "derive"
if str(_DERIVE) not in sys.path:
    sys.path.insert(0, str(_DERIVE))

from derive import rollup  # noqa: E402

PROJECTS = [
    {"slug": "payments", "keywords": ["payout"], "jira_epics": ["EX-2238"]},
    {"slug": "ledger", "keywords": ["ledger"], "jira_epics": []},
]


# ── extract_claude_summary ──────────────────────────────────────────────────

def test_claude_summary_absent_marker_is_none():
    assert rollup.extract_claude_summary("plain body, no marker") is None
    assert rollup.extract_claude_summary(None) is None


def test_claude_summary_extracts_prose_before_findings():
    body = (
        f"{rollup.CLAUDE_REVIEW_MARKER}\n"
        "## Code Review — PR #859\n\n"
        "This PR adds retry logic to the slack walker.\n\n"
        "### Findings\n| sev | note |\n"
    )
    summary = rollup.extract_claude_summary(body)
    assert summary and "retry logic" in summary
    assert "Findings" not in summary  # stops at the ### section


# ── detect_domains ───────────────────────────────────────────────────────────

def test_detect_domains_epic_first():
    assert rollup.detect_domains("[Epic EX-2238] anything", PROJECTS) == ["payments"]


def test_detect_domains_keyword():
    assert rollup.detect_domains("fix the payout flow", PROJECTS) == ["payments"]


def test_detect_domains_none_and_empty():
    assert rollup.detect_domains("unrelated text", PROJECTS) == []
    assert rollup.detect_domains("", PROJECTS) == []


# ── extract_matterai_summary ────────────────────────────────────────────────

def test_matterai_summary():
    body = "🧪 PR Review is completed: adds idempotent upsert\nmore text"
    assert rollup.extract_matterai_summary(body) == "adds idempotent upsert"
    assert rollup.extract_matterai_summary("no marker") is None


# ── _subject_source ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,expected", [
    ("page:123456", "confluence"),
    ("slack:C0A:1700000000.000100", "slack"),
    ("org/repo#42", "github"),
    ("EX-2629", "jira"),
    ("weird-thing", "unknown"),
    ("", "unknown"),
])
def test_subject_source(subject, expected):
    assert rollup._subject_source(subject) == expected


# ── severity_count ───────────────────────────────────────────────────────────

def test_severity_count():
    assert rollup.severity_count("🔴🔴 🟠 🟡🟡🟡") == {"red": 2, "orange": 1, "yellow": 3}
    assert rollup.severity_count(None) == {"red": 0, "orange": 0, "yellow": 0}


# ── fmt_iso / hours_between / percentile ─────────────────────────────────────

def test_fmt_iso():
    assert rollup.fmt_iso("2026-06-10T12:34:56Z") == "2026-06-10 12:34"
    assert rollup.fmt_iso("") == ""


def test_hours_between():
    assert rollup.hours_between("2026-06-10T10:00:00Z", "2026-06-10T13:00:00Z") == 3.0
    assert rollup.hours_between("bad", "alsobad") == 0.0


def test_percentile():
    assert rollup.percentile([], 0.5) == 0.0
    assert rollup.percentile([1, 2, 3, 4], 0.5) == 3
    assert rollup.percentile([1, 2, 3, 4], 1.0) == 4  # clamps to last


# ── person_aliases ───────────────────────────────────────────────────────────

def test_person_aliases_collects_present_keys():
    p = {"github": "alice-gh", "email": "alice@x.com", "slack_id": "U_A"}
    out = rollup.person_aliases(p)
    assert set(out) == {"alice-gh", "alice@x.com", "U_A"}

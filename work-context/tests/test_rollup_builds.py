"""derive/rollup.py — the build_* report generators (DB-driven).

These are the heart of the derivation layer: they read the events.db and emit
the per-person / per-project / weekly / alerts markdown. Exercised here against
the seeded DB. Assertions are intentionally loose (return shape + a key signal
present) — the goal is to run the real query/format paths so a crash or empty
output regresses loudly, without pinning exact wording.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_DERIVE = Path(__file__).resolve().parent.parent / "derive"
if str(_DERIVE) not in sys.path:
    sys.path.insert(0, str(_DERIVE))

from derive import rollup  # noqa: E402
from derive.llm_classifier import SubjectVerdict  # noqa: E402
from tests.conftest import SEED_PEOPLE, SEED_PROJECTS  # noqa: E402

SINCE = "2026-05-01T00:00:00Z"


@pytest.fixture
def cfg():
    """people-by-handle + alias_map + verdicts matching the seed."""
    people = {p["github"]: p for p in SEED_PEOPLE}
    alias_map = {}
    for p in SEED_PEOPLE:
        for k in ("github", "email", "jira_id", "slack_id"):
            if p.get(k):
                alias_map[p[k]] = p["github"]
    verdicts = {
        "EX-2301": SubjectVerdict(domains=["payments"], summary="payout rounding"),
        "org/repo#10": SubjectVerdict(domains=["payments"], summary="payout fix"),
        "page:123456789": SubjectVerdict(domains=["ledger"], summary="ledger notes"),
    }
    return {"people": people, "alias_map": alias_map, "projects": SEED_PROJECTS,
            "verdicts": verdicts}


def test_build_person_profile_returns_markdown(seeded_db, cfg):
    md = rollup.build_person_profile(
        seeded_db, "alice-gh", SINCE, cfg["projects"], cfg["people"],
        cfg["alias_map"], cfg["verdicts"])
    assert isinstance(md, str) and md.strip()


def test_build_project_rollup_payments(seeded_db, cfg):
    payments = next(p for p in SEED_PROJECTS if p["slug"] == "payments")
    md = rollup.build_project_rollup(
        seeded_db, payments, SINCE, cfg["people"], cfg["alias_map"], cfg["verdicts"])
    assert isinstance(md, str) and md.strip()


def test_build_weekly_returns_name_and_body(seeded_db, cfg):
    fname, md = rollup.build_weekly(
        seeded_db, "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z", cfg["people"])
    assert fname and isinstance(md, str)


def test_build_alerts_returns_markdown(seeded_db, cfg):
    md = rollup.build_alerts(seeded_db, cfg["people"])
    assert isinstance(md, str)


def test_collect_subjects_picks_up_seed(seeded_db, cfg):
    subs = rollup.collect_subjects(seeded_db, SINCE, cfg["projects"])
    subjects = {s.subject for s in subs}
    # github PR + jira story + confluence page are all classifiable subjects.
    assert "org/repo#10" in subjects
    assert "EX-2301" in subjects

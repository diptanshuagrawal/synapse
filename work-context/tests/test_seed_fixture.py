"""Smoke test for the seeded_db fixture itself.

If the seed stops building (schema drift, bad insert), every DB-driven derive
test would fail confusingly — this isolates that failure to one obvious place.
"""

from __future__ import annotations


def test_seed_has_all_sources(seeded_db):
    srcs = {r[0] for r in seeded_db.execute("SELECT DISTINCT source FROM events")}
    assert srcs == {"jira", "github", "confluence", "slack"}


def test_seed_jira_lifecycle(seeded_db):
    # epic + story + assignment + 2 status changes.
    n = seeded_db.execute(
        "SELECT COUNT(*) FROM events WHERE source='jira'").fetchone()[0]
    assert n == 5
    done = seeded_db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='status_change' "
        "AND to_status='Done'").fetchone()[0]
    assert done == 1


def test_seed_pr_and_review(seeded_db):
    types = {r[0] for r in seeded_db.execute(
        "SELECT event_type FROM events WHERE source='github'")}
    assert types == {"pr_opened", "review", "pr_merged"}


def test_seed_subject_summary_domains(seeded_db):
    row = seeded_db.execute(
        "SELECT domains FROM subject_summary WHERE subject='org/repo#10'").fetchone()
    assert row and "payments" in row[0]


def test_seed_pr_meta_and_topic_brief(seeded_db):
    assert seeded_db.execute("SELECT COUNT(*) FROM pr_meta").fetchone()[0] == 1
    assert seeded_db.execute(
        "SELECT root_cause FROM topic_brief WHERE cluster_id=1").fetchone()[0] == "job crash"


def test_seed_refs_populated(seeded_db):
    # enrich_refs ran on insert → the PR body 'see EX-2301' yields a ticket ref.
    refs = {r[0] for r in seeded_db.execute(
        "SELECT ref_value FROM event_refs WHERE ref_type='ticket'")}
    assert "EX-2301" in refs

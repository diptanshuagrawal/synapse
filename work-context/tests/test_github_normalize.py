"""ingest/github.py — GitHub REST JSON → Event mapping (no network).

normalize_pr collapses three lifecycle states (opened/closed/merged) into one
event with the right type + timestamp; the commit normalizers carry a 3-step
actor-resolution chain (GH login → people.yaml email lookup → raw git name)
that PR-author ownership attribution depends on. Both are pinned here.
"""

from __future__ import annotations

import pytest

from ingest import github


# ── helpers ─────────────────────────────────────────────────────────────────

def test_ts_z_normalization():
    assert github._ts("2026-06-10T12:00:00+00:00") == "2026-06-10T12:00:00Z"
    assert github._ts("2026-06-10T12:00:00Z") == "2026-06-10T12:00:00Z"
    assert github._ts("").endswith("Z")


def test_actor_login():
    assert github._actor({"login": "alice-gh"}) == "alice-gh"
    assert github._actor(None) is None


def test_email_to_github_lookup(patch_config):
    assert github._email_to_github("alice@example.com") == "alice-gh"
    assert github._email_to_github("ALICE@EXAMPLE.COM") == "alice-gh"  # case-insensitive
    assert github._email_to_github("nobody@x.com") is None
    assert github._email_to_github(None) is None


# ── normalize_pr: opened / closed / merged ──────────────────────────────────

def _pr(**kw):
    base = dict(number=7, state="open", title="add feature",
                body="closes EX-1", user={"login": "alice-gh"},
                html_url="https://github.com/org/repo/pull/7",
                created_at="2026-06-01T00:00:00Z", closed_at=None, merged_at=None)
    base.update(kw)
    return base


def test_pr_opened(patch_config):
    ev = github.normalize_pr("org/repo", _pr())
    assert ev.event_type == "pr_opened"
    assert ev.id == "github:org/repo:pr:7:pr_opened"
    assert ev.subject == "org/repo#7"
    assert ev.ts == "2026-06-01T00:00:00Z"
    assert "alice" in ev.refs.people
    assert "EX-1" in ev.refs.tickets


def test_pr_closed_unmerged(patch_config):
    ev = github.normalize_pr("org/repo", _pr(state="closed",
                             closed_at="2026-06-02T00:00:00Z"))
    assert ev.event_type == "pr_closed"
    assert ev.ts == "2026-06-02T00:00:00Z"


def test_pr_merged_takes_priority(patch_config):
    # merged_at set → pr_merged even if state=closed.
    ev = github.normalize_pr("org/repo", _pr(state="closed",
                             closed_at="2026-06-02T00:00:00Z",
                             merged_at="2026-06-03T00:00:00Z"))
    assert ev.event_type == "pr_merged"
    assert ev.ts == "2026-06-03T00:00:00Z"


def test_pr_merged_by_credited_as_extra_handle(patch_config):
    ev = github.normalize_pr("org/repo", _pr(merged_at="2026-06-03T00:00:00Z",
                             merged_by={"login": "bob-gh"}))
    # both author (alice) and merger (bob) resolved.
    assert {"alice", "bob"} <= set(ev.refs.people)


# ── review / comment ─────────────────────────────────────────────────────────

def test_normalize_review(patch_config):
    review = {"id": 55, "state": "APPROVED", "user": {"login": "bob-gh"},
              "submitted_at": "2026-06-04T00:00:00Z", "body": "lgtm",
              "html_url": "https://github.com/org/repo/pull/7#r55"}
    ev = github.normalize_review("org/repo", 7, review)
    assert ev.event_type == "review"
    assert ev.id == "github:org/repo:pr:7:review:55"
    assert "APPROVED" in ev.title
    assert "bob" in ev.refs.people


def test_normalize_pr_comment(patch_config):
    comment = {"id": 88, "user": {"login": "alice-gh"},
               "created_at": "2026-06-05T00:00:00Z", "body": "nit",
               "html_url": "https://github.com/org/repo/pull/7#c88"}
    ev = github.normalize_pr_comment("org/repo", 7, comment, kind="review_comment")
    assert ev.event_type == "comment"
    assert ev.id == "github:org/repo:pr:7:review_comment:88"


# ── commit actor-resolution chain ───────────────────────────────────────────

def _commit(login=None, email=None, name="Git Author", sha="abc123def4567"):
    return {
        "sha": sha,
        "author": {"login": login} if login else None,
        "commit": {"message": "fix thing\n\nbody line",
                   "author": {"email": email, "name": name,
                              "date": "2026-06-06T00:00:00Z"}},
        "html_url": f"https://github.com/org/repo/commit/{sha}",
    }


def test_commit_in_pr_prefers_gh_login(patch_config):
    ev = github.normalize_pr_commit("org/repo", 7, _commit(login="alice-gh"))
    assert ev.event_type == "commit_in_pr"
    assert ev.actor == "alice-gh"
    assert ev.subject == "org/repo#7"
    assert ev.title == "fix thing"  # first line only


def test_commit_falls_back_to_email_lookup(patch_config):
    ev = github.normalize_commit("org/repo", _commit(login=None,
                                 email="alice@example.com"))
    assert ev.actor == "alice-gh"  # resolved via people.yaml email→github


def test_commit_falls_back_to_raw_git_name(patch_config):
    ev = github.normalize_commit("org/repo", _commit(login=None,
                                 email="stranger@nowhere.com", name="Stranger"))
    assert ev.actor == "Stranger"  # neither login nor known email → raw name


def test_commit_pushed_subject_uses_short_sha(patch_config):
    ev = github.normalize_commit("org/repo", _commit(login="alice-gh",
                                 sha="abcdef1234567890"))
    assert ev.event_type == "commit_pushed"
    assert ev.subject == "org/repo@abcdef123456"  # first 12 chars

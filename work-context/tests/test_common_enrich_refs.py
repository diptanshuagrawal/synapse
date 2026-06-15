"""enrich_refs — ref extraction + people/project resolution.

This is the highest-leverage function in the pipeline: every event's
person/project/ticket/page/PR/slack-thread refs come from here, and a silent
regex regression poisons every downstream analytic. Each ref kind has its own
extractor with sharp edge cases (the 16-digit Slack ts packing, the PR-owner
prefix filter, project keyword-vs-epic-vs-page precedence) — all exercised here.
"""

from __future__ import annotations

import pytest

from ingest import common


def _enrich(make_event, **kw):
    """Build an event from kwargs, run enrich_refs, return its refs."""
    actor_field = kw.pop("actor_field", "github")
    ev = make_event(**kw)
    common.enrich_refs(ev, actor_field=actor_field)
    return ev.refs


# ── Jira ticket extraction ─────────────────────────────────────────────────

def test_ticket_basic(patch_config, make_event):
    refs = _enrich(make_event, title="fix EX-2629 today", body="")
    assert refs.tickets == ["EX-2629"]


def test_ticket_dedup_and_sorted(patch_config, make_event):
    refs = _enrich(make_event, title="EX-2 and ABC-10", body="EX-2 again")
    assert refs.tickets == ["ABC-10", "EX-2"]


def test_ticket_requires_word_boundary(patch_config, make_event):
    # Lowercase / glued tokens must NOT match the KEY-N shape.
    refs = _enrich(make_event, title="notaticketEX-1x ex-9", body="")
    assert refs.tickets == []


# ── Confluence page id extraction ──────────────────────────────────────────

def test_confluence_page_id(patch_config, make_event):
    refs = _enrich(make_event,
                   title="see /pages/123456789 for details", body="")
    assert refs.pages == ["123456789"]


def test_confluence_page_id_too_short_ignored(patch_config, make_event):
    # Regex requires 8-12 digits; 7 digits must not match.
    refs = _enrich(make_event, title="/pages/1234567", body="")
    assert refs.pages == []


# ── GitHub PR / issue extraction ───────────────────────────────────────────

def test_pr_url_extraction(patch_config, make_event):
    refs = _enrich(make_event,
                   title="", body="https://github.com/org/repo/pull/42 merged")
    assert refs.pull_requests == ["org/repo#42"]


def test_pr_issues_url_extraction(patch_config, make_event):
    refs = _enrich(make_event,
                   title="https://github.com/org/repo/issues/7", body="")
    assert refs.pull_requests == ["org/repo#7"]


def test_pr_shorthand_only_for_known_owner_prefix(patch_config, make_event):
    # patch_config sets handle prefixes to ('org/',). 'org/repo#5' passes;
    # 'stranger/repo#9' is filtered out as noise.
    refs = _enrich(make_event,
                   title="org/repo#5 vs stranger/repo#9", body="")
    assert refs.pull_requests == ["org/repo#5"]


# ── Slack thread permalink → canonical subject ─────────────────────────────

def test_slack_thread_permalink_ts_packing(patch_config, make_event):
    # 16-digit packed ts → 10-sec '.' 6-microsec.
    url = "https://acme.slack.com/archives/C0ABC123/p1700000000123456"
    refs = _enrich(make_event, title="", body=url)
    assert refs.slack_threads == ["slack:C0ABC123:1700000000.123456"]


def test_slack_thread_requires_exactly_16_digits(patch_config, make_event):
    # 15 digits must NOT match (guards against truncated text false-positives).
    url = "https://acme.slack.com/archives/C0ABC123/p170000000012345"
    refs = _enrich(make_event, title="", body=url)
    assert refs.slack_threads == []


# ── Slack <@U…> mention resolution (needs users cache) ─────────────────────

def test_slack_mention_resolved_via_cache(patch_config, make_event):
    ev = make_event(title="ping <@U0CAROL> please", body="", actor=None)
    common.enrich_refs(ev, slack_users_cache={"U0CAROL": "carol"})
    assert "carol" in ev.refs.people


def test_slack_mention_unknown_uid_skipped(patch_config, make_event):
    ev = make_event(title="ping <@U0NOBODY>", body="", actor=None)
    common.enrich_refs(ev, slack_users_cache={"U0CAROL": "carol"})
    assert ev.refs.people == []


# ── Actor / extra-handle resolution ────────────────────────────────────────

def test_actor_resolved_by_github_field(patch_config, make_event):
    refs = _enrich(make_event, actor="alice-gh", actor_field="github",
                   title="", body="")
    assert refs.people == ["alice"]


def test_actor_unknown_handle_not_added(patch_config, make_event):
    refs = _enrich(make_event, actor="ghost-gh", actor_field="github",
                   title="", body="")
    assert refs.people == []


def test_extra_handles_resolved(patch_config, make_event):
    ev = make_event(actor="alice-gh", title="", body="")
    common.enrich_refs(ev, actor_field="github",
                       extra_handles=[("bob@example.com", "email")])
    assert ev.refs.people == ["alice", "bob"]


def test_actor_resolution_case_insensitive(patch_config, make_event):
    refs = _enrich(make_event, actor="ALICE-GH", actor_field="github",
                   title="", body="")
    assert refs.people == ["alice"]


# ── Project matching: keyword / epic-prefix / page precedence ──────────────

def test_project_keyword_match(patch_config, make_event):
    refs = _enrich(make_event, title="payout reconciliation", body="")
    assert "payments" in refs.projects


def test_project_keyword_case_insensitive(patch_config, make_event):
    refs = _enrich(make_event, title="WITHHOLDING tax", body="")
    assert "payments" in refs.projects


def test_project_via_epic_prefix(patch_config, make_event):
    # The [Epic EX-2238] title prefix anchors to payments via jira_epics.
    refs = _enrich(make_event,
                   title="[Epic EX-2238] some unrelated words", body="")
    assert "payments" in refs.projects


def test_project_via_confluence_page(patch_config, make_event):
    refs = _enrich(make_event, title="notes /pages/123456789", body="")
    assert "payments" in refs.projects


def test_unrelated_text_matches_no_project(patch_config, make_event):
    refs = _enrich(make_event, title="lunch menu", body="nothing here")
    assert refs.projects == []


# ── None-safety + idempotence ──────────────────────────────────────────────

def test_none_title_and_body_safe(patch_config, make_event):
    ev = make_event(title=None, body=None, actor=None)
    common.enrich_refs(ev)  # must not raise on None text
    assert ev.refs.tickets == [] and ev.refs.projects == []


def test_enrich_is_idempotent(patch_config, make_event):
    ev = make_event(actor="alice-gh", title="EX-1 payout", body="")
    common.enrich_refs(ev, actor_field="github")
    first = (ev.refs.people, ev.refs.tickets, ev.refs.projects)
    common.enrich_refs(ev, actor_field="github")
    second = (ev.refs.people, ev.refs.tickets, ev.refs.projects)
    assert first == second

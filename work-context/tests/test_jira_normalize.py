"""ingest/jira.py — Jira REST JSON → Event mapping (no network).

The normalize_* functions are the contract boundary between Jira's API shape
and our Event schema; every standup/retro/velocity number traces back to these
mappings. They're pure (dict in, Event out), so we exercise the field plumbing
and the fiddly bits — ADF flattening, sprint selection, the +0530 timezone
fixup, epic-prefix anchoring, and per-changelog-item fan-out — directly.
"""

from __future__ import annotations

import pytest

from ingest import jira


# ── helpers: _ts / _user / _flatten_adf ────────────────────────────────────

def test_ts_converts_offset_to_utc():
    # 11:23:45.123 +0530 → 05:53:45 UTC.
    assert jira._ts("2026-05-08T11:23:45.123+0530").startswith("2026-05-08T05:53:45")


def test_ts_keeps_z():
    assert jira._ts("2026-05-08T11:23:45.000Z") == "2026-05-08T11:23:45Z"


def test_ts_empty_is_now_z():
    out = jira._ts("")
    assert out.endswith("Z") and "T" in out


def test_ts_unparseable_passthrough():
    assert jira._ts("not-a-date") == "not-a-date"


def test_user_prefers_email_then_name_then_account():
    assert jira._user({"emailAddress": "a@x.com", "displayName": "A"}) == "a@x.com"
    assert jira._user({"displayName": "A", "accountId": "acc1"}) == "A"
    assert jira._user({"accountId": "acc1"}) == "acc1"
    assert jira._user(None) is None


def test_flatten_adf_nested():
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"}]}]}
    assert jira._flatten_adf(adf) == "hello world"


# ── sprint selection ────────────────────────────────────────────────────────

def test_sprint_prefers_active():
    f = {"customfield_10010": [
        {"id": 1, "name": "S1", "state": "closed"},
        {"id": 2, "name": "S2", "state": "active"},
        {"id": 3, "name": "S3", "state": "future"}]}
    assert jira._extract_sprint(f) == (2, "S2", "active")


def test_sprint_closed_picks_highest_id():
    f = {"customfield_10010": [
        {"id": 5, "name": "old", "state": "closed"},
        {"id": 9, "name": "recent", "state": "closed"}]}
    assert jira._extract_sprint(f) == (9, "recent", "closed")


def test_sprint_future_picks_lowest_id():
    f = {"customfield_10010": [
        {"id": 9, "name": "later", "state": "future"},
        {"id": 7, "name": "soon", "state": "future"}]}
    assert jira._extract_sprint(f) == (7, "soon", "future")


def test_sprint_absent_is_none_triple():
    assert jira._extract_sprint({}) == (None, None, None)
    assert jira._extract_sprint({"customfield_10010": []}) == (None, None, None)


# ── story points ─────────────────────────────────────────────────────────────

def test_story_points_float():
    assert jira._extract_story_points({"customfield_10051": "5"}) == 5.0
    assert jira._extract_story_points({"customfield_10051": 3}) == 3.0


def test_story_points_missing_or_bad():
    assert jira._extract_story_points({}) is None
    assert jira._extract_story_points({"customfield_10051": "abc"}) is None


# ── epic key + prefix ────────────────────────────────────────────────────────

def test_epic_key_from_parent_epic():
    issue = {"fields": {"parent": {"key": "EX-100",
             "fields": {"issuetype": {"name": "Epic"}}}}}
    assert jira.get_epic_key(issue) == "EX-100"


def test_epic_key_parent_not_epic_ignored():
    issue = {"fields": {"parent": {"key": "EX-1",
             "fields": {"issuetype": {"name": "Story"}}}}}
    assert jira.get_epic_key(issue) == ""


def test_epic_key_classic_link_string():
    assert jira.get_epic_key({"fields": {"customfield_10014": "EX-9"}}) == "EX-9"


def test_prefix_epic_idempotent():
    assert jira._prefix_epic("title", "EX-1") == "[Epic EX-1] title"
    assert jira._prefix_epic("[Epic EX-1] title", "EX-1") == "[Epic EX-1] title"
    assert jira._prefix_epic("title", "") == "title"


# ── normalize_issue_created ──────────────────────────────────────────────────

def test_normalize_issue_created_full(patch_config):
    issue = {
        "key": "EX-42",
        "fields": {
            "creator": {"emailAddress": "alice@example.com"},
            "assignee": {"emailAddress": "bob@example.com"},
            "summary": "withholding fix",
            "description": {"type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "body text"}]}]},
            "issuetype": {"name": "Task"},
            "status": {"name": "To Do"},
            "created": "2026-05-08T11:23:45.000+0530",
            "customfield_10051": "8",
            "customfield_10010": [{"id": 1, "name": "Sprint 1", "state": "active"}],
            "parent": {"key": "EX-2238", "fields": {"issuetype": {"name": "Epic"}}},
        },
    }
    ev = jira.normalize_issue_created("example.atlassian.net", issue)
    assert ev.id == "jira:EX-42:created"
    assert ev.event_type == "issue_created"
    assert ev.subject == "EX-42"
    assert ev.title == "[Epic EX-2238] withholding fix"
    assert ev.body == "body text"
    assert ev.actor == "alice@example.com"
    assert ev.assignee == "bob@example.com"
    assert ev.issue_type == "Task"
    assert ev.to_status == "To Do"
    assert ev.story_points == 8.0
    assert ev.sprint_state == "active"
    assert ev.url.endswith("/browse/EX-42")
    # enrich_refs resolved actor (email) + project (keyword + epic) + epic ticket.
    assert "alice" in ev.refs.people
    assert "payments" in ev.refs.projects


def test_normalize_issue_created_unassigned(patch_config):
    issue = {"key": "EX-1", "fields": {
        "creator": {"emailAddress": "alice@example.com"},
        "summary": "x", "created": "2026-05-08T00:00:00.000Z"}}
    ev = jira.normalize_issue_created("d", issue)
    assert ev.assignee is None and ev.issue_type is None


# ── normalize_changelog_entry: one event per item ───────────────────────────

def test_changelog_status_and_assignee_fan_out(patch_config):
    history = {
        "id": "h1", "created": "2026-05-08T00:00:00.000Z",
        "author": {"emailAddress": "alice@example.com"},
        "items": [
            {"field": "status", "fromString": "To Do", "toString": "In Progress"},
            {"field": "assignee", "fromString": "∅", "toString": "Bob Example"},
        ],
    }
    evs = jira.normalize_changelog_entry("d", "EX-5", history, epic_key="EX-2238")
    assert [e.event_type for e in evs] == ["status_change", "assignment"]
    sc = evs[0]
    assert sc.to_status == "In Progress"
    assert sc.title == "[Epic EX-2238] status: To Do → In Progress"
    assert sc.id == "jira:EX-5:status:h1:0"
    assert evs[1].id == "jira:EX-5:assignee:h1:1"


def test_changelog_sprint_delta_only_on_change(patch_config):
    history = {"id": "h2", "created": "2026-05-08T00:00:00.000Z",
               "author": {"emailAddress": "alice@example.com"},
               "items": [{"field": "Sprint", "fromString": "S1", "toString": "S1, S2"}]}
    evs = jira.normalize_changelog_entry("d", "EX-6", history)
    assert len(evs) == 1 and evs[0].event_type == "sprint_change"
    assert "+S2" in evs[0].title


def test_changelog_sprint_no_op_skipped(patch_config):
    history = {"id": "h3", "created": "2026-05-08T00:00:00.000Z",
               "author": {"emailAddress": "alice@example.com"},
               "items": [{"field": "Sprint", "fromString": "S1", "toString": "S1"}]}
    assert jira.normalize_changelog_entry("d", "EX-7", history) == []


# ── normalize_comment ────────────────────────────────────────────────────────

def test_normalize_comment(patch_config):
    comment = {"id": "c1", "created": "2026-05-08T00:00:00.000Z",
               "author": {"emailAddress": "alice@example.com"},
               "body": "see EX-99"}
    ev = jira.normalize_comment("example.atlassian.net", "EX-8", comment)
    assert ev.event_type == "comment"
    assert ev.id == "jira:EX-8:comment:c1"
    assert "EX-99" in ev.refs.tickets
    assert "focusedCommentId=c1" in ev.url

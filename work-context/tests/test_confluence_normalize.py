"""ingest/confluence.py — Confluence v2 JSON → Event mapping (no network).

Confluence attributes edits to the *version* author (who made this revision),
not the page's original creator — getting that precedence wrong miscredits
every doc edit. The actor is an Atlassian accountId resolved via the jira_id
people.yaml field. Body is capped to avoid massive blobs. All pinned here.
"""

from __future__ import annotations

import pytest

from ingest import confluence


def test_ts_offset_to_utc():
    assert confluence._ts("2026-05-08T11:23:45.000+00:00") == "2026-05-08T11:23:45Z"


def test_load_team_account_ids(patch_config):
    ids = confluence.load_team_account_ids()
    assert ids == {"acc-alice", "acc-bob"}


def _page(**kw):
    base = dict(id=987654321,
                title="Design doc",
                version={"number": 3, "authorId": "acc-bob",
                         "createdAt": "2026-05-08T00:00:00.000Z"},
                authorId="acc-alice",
                spaceId="SPACE1",
                body={"storage": {"value": "page body /pages/987654321"}})
    base.update(kw)
    return base


def test_page_created_vs_updated(patch_config):
    created = confluence.normalize_page("ex.atlassian.net", _page(), is_first_version=True)
    updated = confluence.normalize_page("ex.atlassian.net", _page(), is_first_version=False)
    assert created.event_type == "page_created"
    assert updated.event_type == "page_updated"


def test_page_actor_is_version_author(patch_config):
    # version.authorId (acc-bob) wins over page.authorId (acc-alice).
    ev = confluence.normalize_page("ex.atlassian.net", _page(), is_first_version=False)
    assert ev.actor == "acc-bob"
    assert "bob" in ev.refs.people  # resolved via jira_id field


def test_page_falls_back_to_page_author(patch_config):
    p = _page(version={"number": 1, "createdAt": "2026-05-08T00:00:00.000Z"})
    ev = confluence.normalize_page("ex.atlassian.net", p, is_first_version=True)
    assert ev.actor == "acc-alice"


def test_page_id_subject_and_url(patch_config):
    ev = confluence.normalize_page("ex.atlassian.net", _page(), is_first_version=False)
    assert ev.subject == "page:987654321"
    assert ev.id == "confluence:page:987654321:v3"
    assert "/pages/987654321" in ev.url


def test_page_body_capped(patch_config):
    big = "x" * 9000
    ev = confluence.normalize_page("ex.atlassian.net",
                                   _page(body={"storage": {"value": big}}),
                                   is_first_version=True)
    assert len(ev.body) == 5000


def test_normalize_comment(patch_config):
    comment = {"id": 222, "pageId": 987654321,
               "version": {"authorId": "acc-alice",
                           "createdAt": "2026-05-08T00:00:00.000Z"},
               "body": {"storage": {"value": "a comment"}}}
    ev = confluence.normalize_comment("ex.atlassian.net", comment, kind="footer",
                                      page_title="Design doc")
    assert ev.event_type == "comment"
    assert ev.subject == "page:987654321"
    assert ev.id == "confluence:comment:footer:222"
    assert "Design doc" in ev.title

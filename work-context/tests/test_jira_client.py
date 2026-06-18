"""ingest/jira.py — JiraClient HTTP layer (no network).

Normalizers live in test_jira_normalize.py; this pins the fetch layer: get/post
429-retry + raise, whoami (the silent-empty-200 guard), search_issues
token-pagination + legacy /search fallback on 404, and issue_comments paging.
A path-dispatching fake session is injected.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from ingest import jira as jira_mod


class _FakeResp:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def _client(get_fn=None, post_fn=None):
    c = jira_mod.JiraClient(domain="x.atlassian.net", email="e", token="t")
    c.session = SimpleNamespace(
        get=get_fn or (lambda *a, **k: _FakeResp(200, {})),
        post=post_fn or (lambda *a, **k: _FakeResp(200, {})),
    )
    return c


# ── get / post retry ─────────────────────────────────────────────────────────

def test_get_returns_json():
    assert _client(get_fn=lambda u, params=None, timeout=None: _FakeResp(200, {"ok": 1})).get("/p") == {"ok": 1}


def test_get_retries_on_429(monkeypatch):
    monkeypatch.setattr(jira_mod.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_get(u, params=None, timeout=None):
        calls["n"] += 1
        return _FakeResp(429, headers={"Retry-After": "0"}) if calls["n"] == 1 else _FakeResp(200, {"ok": 1})

    assert _client(get_fn=fake_get).get("/p") == {"ok": 1}
    assert calls["n"] == 2


def test_get_raises():
    with pytest.raises(requests.HTTPError):
        _client(get_fn=lambda u, params=None, timeout=None: _FakeResp(500)).get("/p")


# ── whoami ────────────────────────────────────────────────────────────────────

def test_whoami_returns_email():
    c = _client(get_fn=lambda u, params=None, timeout=None: _FakeResp(200, {"emailAddress": "me@x.com"}))
    assert c.whoami() == "me@x.com"


def test_whoami_none_on_error():
    c = _client(get_fn=lambda u, params=None, timeout=None: _FakeResp(401))
    assert c.whoami() is None


# ── search_issues: token pagination ──────────────────────────────────────────

def test_search_issues_token_paginated():
    pages = {
        None:  {"issues": [{"key": "EX-1"}], "nextPageToken": "t2"},
        "t2":  {"issues": [{"key": "EX-2"}]},   # no token → stop
    }

    def fake_post(path, json=None, timeout=None):
        return _FakeResp(200, pages[json.get("nextPageToken")])

    keys = [i["key"] for i in _client(post_fn=fake_post).search_issues("jql", [], [])]
    assert keys == ["EX-1", "EX-2"]


# ── search_issues: legacy fallback on 404 ────────────────────────────────────

def test_search_issues_legacy_fallback():
    def fake_post(path, json=None, timeout=None):
        if path.endswith("/search/jql"):
            return _FakeResp(404, text="not found")        # new endpoint absent
        # legacy /search: one page, total=1.
        return _FakeResp(200, {"issues": [{"key": "EX-9"}], "total": 1})

    keys = [i["key"] for i in _client(post_fn=fake_post).search_issues("jql", [], [])]
    assert keys == ["EX-9"]


# ── issue_comments paging ────────────────────────────────────────────────────

def test_issue_comments_paginates():
    def fake_get(path, params=None, timeout=None):
        start = params["startAt"]
        if start == 0:
            return _FakeResp(200, {"comments": [{"id": "1"}], "total": 2})
        return _FakeResp(200, {"comments": [{"id": "2"}], "total": 2})

    ids = [c["id"] for c in _client(get_fn=fake_get).issue_comments("EX-1")]
    assert ids == ["1", "2"]

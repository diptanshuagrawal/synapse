"""ingest/confluence.py — ConfluenceClient HTTP layer (no network).

Normalizers live in test_confluence_normalize.py; this pins the fetch layer:
get 429-retry + raise, whoami (silent-empty guard), get_page_title (cache +
error→""), and paginate's cursor walk (_links.next, /wiki prefix strip, stop).
Fake session injected.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from ingest import confluence as cf


class _FakeResp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _client(get_fn):
    c = cf.ConfluenceClient(domain="x.atlassian.net", email="e", token="t")
    c.session = SimpleNamespace(get=get_fn)
    return c


# ── get ──────────────────────────────────────────────────────────────────────

def test_get_returns_json():
    assert _client(lambda u, params=None, timeout=None: _FakeResp(200, {"ok": 1})).get("/p") == {"ok": 1}


def test_get_retries_on_429(monkeypatch):
    monkeypatch.setattr(cf.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_get(u, params=None, timeout=None):
        calls["n"] += 1
        return _FakeResp(429, headers={"Retry-After": "0"}) if calls["n"] == 1 else _FakeResp(200, {"ok": 1})

    assert _client(fake_get).get("/p") == {"ok": 1}
    assert calls["n"] == 2


# ── whoami ────────────────────────────────────────────────────────────────────

def test_whoami_email_or_account():
    assert _client(lambda u, params=None, timeout=None: _FakeResp(200, {"email": "me@x"})).whoami() == "me@x"
    assert _client(lambda u, params=None, timeout=None: _FakeResp(200, {"accountId": "acc1"})).whoami() == "acc1"


def test_whoami_none_on_error():
    assert _client(lambda u, params=None, timeout=None: _FakeResp(500)).whoami() is None


# ── get_page_title (caches) ──────────────────────────────────────────────────

def test_get_page_title_caches():
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return _FakeResp(200, {"title": "Design Doc"})

    c = _client(fake_get)
    assert c.get_page_title("123") == "Design Doc"
    assert c.get_page_title("123") == "Design Doc"   # second call served from cache
    assert calls["n"] == 1


def test_get_page_title_error_is_empty():
    assert _client(lambda url, params=None, timeout=None: _FakeResp(404)).get_page_title("9") == ""
    assert _client(lambda url, params=None, timeout=None: _FakeResp(200, {})).get_page_title("") == ""


# ── paginate (cursor _links.next) ────────────────────────────────────────────

def test_paginate_follows_cursor():
    def fake_get(url, params=None, timeout=None):
        if "cursor" not in url:
            return _FakeResp(200, {"results": [{"id": "1"}],
                                   "_links": {"next": "/wiki/api/v2/pages?cursor=abc"}})
        return _FakeResp(200, {"results": [{"id": "2"}], "_links": {}})   # no next → stop

    ids = [r["id"] for r in _client(fake_get).paginate("/api/v2/pages")]
    assert ids == ["1", "2"]

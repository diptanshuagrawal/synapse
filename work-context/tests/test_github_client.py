"""ingest/github.py — GitHubClient HTTP layer (no network).

The normalizers are covered in test_github_normalize.py; this pins the fetch
layer: get() JSON return + rate-limit retry + raise-for-status, and paginate()'s
page loop (stops on a short or empty page, ignores non-list). A fake session is
injected so nothing hits api.github.com.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from ingest import github as gh


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
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _client(get_fn):
    c = gh.GitHubClient(token="t")
    c.session = SimpleNamespace(get=get_fn)
    return c


# ── get ──────────────────────────────────────────────────────────────────────

def test_get_returns_json():
    c = _client(lambda url, params=None, timeout=None: _FakeResp(200, {"ok": 1}))
    assert c.get("/x") == {"ok": 1}


def test_get_retries_on_rate_limit(monkeypatch):
    monkeypatch.setattr(gh.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp(403, None, headers={"X-RateLimit-Reset": "0"},
                             text="API rate limit exceeded")
        return _FakeResp(200, {"ok": 1})

    assert _client(fake_get).get("/x") == {"ok": 1}
    assert calls["n"] == 2   # retried once after the rate-limit sleep


def test_get_raises_on_error():
    c = _client(lambda url, params=None, timeout=None: _FakeResp(500, None, text="boom"))
    with pytest.raises(requests.HTTPError):
        c.get("/x")


# ── paginate ──────────────────────────────────────────────────────────────────

def test_paginate_walks_until_short_page():
    page1 = [{"i": i} for i in range(100)]   # full page → fetch next
    page2 = [{"i": 100}, {"i": 101}]          # short page → stop

    def fake_get(url, params=None, timeout=None):
        return _FakeResp(200, page1 if params["page"] == 1 else page2)

    out = _client(fake_get).paginate("/items")
    assert len(out) == 102 and out[0]["i"] == 0 and out[-1]["i"] == 101


def test_paginate_empty_first_page():
    c = _client(lambda url, params=None, timeout=None: _FakeResp(200, []))
    assert c.paginate("/items") == []


def test_paginate_non_list_response():
    c = _client(lambda url, params=None, timeout=None: _FakeResp(200, {"not": "a list"}))
    assert c.paginate("/items") == []

"""ingest/slack_api_client.py — SlackClient HTTP/retry layer (no network).

The parser (api_message_to_parsed) is covered in test_slack_parse.py. This pins
the transport: RateLimit budgeting, _call's ok/ratelimited/429/transient retry
tree, the error-payload raise, and iter_history cursor pagination. urlopen +
sleep are stubbed — nothing leaves the process.
"""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from ingest import slack_api_client as sac


def _client(monkeypatch):
    return sac.SlackClient(token="xoxp-test")


class _FakeHTTPResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── RateLimit ────────────────────────────────────────────────────────────────

def test_ratelimit_sleeps_at_budget(monkeypatch):
    slept = {"n": 0}
    monkeypatch.setattr(sac.time, "sleep", lambda *_: slept.__setitem__("n", slept["n"] + 1))
    # freeze monotonic so the window never rolls over.
    monkeypatch.setattr(sac.time, "monotonic", lambda: 1000.0)
    rl = sac.RateLimit()
    for _ in range(rl.max_per_min):
        rl.acquire()
    rl.acquire()                 # one past budget → must sleep + reset
    assert slept["n"] >= 1 and rl.calls_in_window == 1


# ── _call retry tree ─────────────────────────────────────────────────────────

def test_call_ok(monkeypatch):
    monkeypatch.setattr(sac, "urlopen", lambda req, timeout=None: _FakeHTTPResp({"ok": True, "x": 1}))
    assert _client(monkeypatch)._call("auth.test", {})["x"] == 1


def test_call_ratelimited_payload_retries(monkeypatch):
    monkeypatch.setattr(sac.time, "sleep", lambda *_: None)
    seq = [{"ok": False, "error": "ratelimited", "retry_after": 0}, {"ok": True, "v": 9}]
    state = {"i": 0}

    def fake_urlopen(req, timeout=None):
        p = seq[state["i"]]; state["i"] += 1
        return _FakeHTTPResp(p)

    monkeypatch.setattr(sac, "urlopen", fake_urlopen)
    assert _client(monkeypatch)._call("conversations.history", {})["v"] == 9


def test_call_other_error_raises(monkeypatch):
    monkeypatch.setattr(sac, "urlopen", lambda req, timeout=None: _FakeHTTPResp({"ok": False, "error": "channel_not_found"}))
    with pytest.raises(RuntimeError):
        _client(monkeypatch)._call("conversations.info", {"channel": "C0X"})


def test_call_http_429_retries(monkeypatch):
    monkeypatch.setattr(sac.time, "sleep", lambda *_: None)
    state = {"i": 0}

    def fake_urlopen(req, timeout=None):
        if state["i"] == 0:
            state["i"] += 1
            raise HTTPError("u", 429, "rate", {"Retry-After": "0"}, None)
        return _FakeHTTPResp({"ok": True, "done": 1})

    monkeypatch.setattr(sac, "urlopen", fake_urlopen)
    assert _client(monkeypatch)._call("auth.test", {})["done"] == 1


def test_call_transient_retries(monkeypatch):
    monkeypatch.setattr(sac.time, "sleep", lambda *_: None)
    state = {"i": 0}

    def fake_urlopen(req, timeout=None):
        if state["i"] == 0:
            state["i"] += 1
            raise socket.timeout("mid-stream drop")
        return _FakeHTTPResp({"ok": True, "ok2": 1})

    monkeypatch.setattr(sac, "urlopen", fake_urlopen)
    assert _client(monkeypatch)._call("conversations.history", {})["ok2"] == 1


# ── iter_history cursor pagination ───────────────────────────────────────────

def test_iter_history_paginates(monkeypatch):
    c = _client(monkeypatch)
    pages = [
        {"messages": [{"ts": "1"}], "response_metadata": {"next_cursor": "c2"}},
        {"messages": [{"ts": "2"}], "response_metadata": {"next_cursor": ""}},
    ]
    state = {"i": 0}
    monkeypatch.setattr(c, "_call", lambda method, params: pages[state.__setitem__("i", state["i"] + 1) or state["i"] - 1])
    out = [m["ts"] for m in c.iter_history("C0X")]
    assert out == ["1", "2"]

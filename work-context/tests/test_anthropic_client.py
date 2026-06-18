"""derive/anthropic_client.py — single-turn completion wrapper.

Covers the key-file gate and complete_json's behaviour: returns the first
content block's text, threads the optional system prompt, retries on transient
APIStatusError (429/5xx) and re-raises non-anthropic errors fast. SDK + sleep
are stubbed — no network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from derive import anthropic_client as ac


# ── key gate ─────────────────────────────────────────────────────────────────

def test_key_present_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "_KEY_PATH", tmp_path / "nokey")
    assert ac.key_present() is False


def test_key_present_true(tmp_path, monkeypatch):
    p = tmp_path / "k"; p.write_text("sk-ant\n")
    monkeypatch.setattr(ac, "_KEY_PATH", p)
    assert ac.key_present() is True


def test_load_key_raises_when_empty(tmp_path, monkeypatch):
    p = tmp_path / "k"; p.write_text("  ")
    monkeypatch.setattr(ac, "_KEY_PATH", p)
    with pytest.raises(ValueError):
        ac._load_key()


# ── complete_json ────────────────────────────────────────────────────────────

def _client(record=None, raise_seq=None):
    """Fake client; create() returns a {} body, optionally raising a sequence first."""
    state = {"i": 0}

    def create(**kwargs):
        if record is not None:
            record.append(kwargs)
        if raise_seq and state["i"] < len(raise_seq):
            exc = raise_seq[state["i"]]
            state["i"] += 1
            raise exc
        return SimpleNamespace(content=[SimpleNamespace(text='{"ok": true}')])

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_complete_json_returns_text(monkeypatch):
    monkeypatch.setattr(ac, "get_client", lambda: _client())
    assert ac.complete_json("hi") == '{"ok": true}'


def test_complete_json_threads_system(monkeypatch):
    rec: list[dict] = []
    monkeypatch.setattr(ac, "get_client", lambda: _client(record=rec))
    ac.complete_json("hi", system="be terse")
    assert rec[0].get("system") == "be terse"


def test_complete_json_omits_system_when_none(monkeypatch):
    rec: list[dict] = []
    monkeypatch.setattr(ac, "get_client", lambda: _client(record=rec))
    ac.complete_json("hi")
    assert "system" not in rec[0]


def test_complete_json_retries_transient(monkeypatch):
    import anthropic
    import httpx
    req = httpx.Request("POST", "https://api.anthropic.com")
    err = anthropic.APIStatusError(
        "overloaded", response=httpx.Response(529, request=req), body=None)
    monkeypatch.setattr(ac, "time", SimpleNamespace(sleep=lambda *_: None))
    monkeypatch.setattr(ac, "get_client", lambda: _client(raise_seq=[err]))
    # one transient failure, then success.
    assert ac.complete_json("hi") == '{"ok": true}'


def test_complete_json_non_anthropic_error_propagates(monkeypatch):
    def boom():
        c = _client()
        c.messages.create = lambda **k: (_ for _ in ()).throw(ValueError("bad"))
        return c
    monkeypatch.setattr(ac, "get_client", boom)
    with pytest.raises(ValueError):
        ac.complete_json("hi")

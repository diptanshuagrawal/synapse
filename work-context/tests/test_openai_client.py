"""derive/openai_client.py — embedding client wrapper.

The only OpenAI surface in the codebase (embeddings only, per policy). Covers
the key-file gate, and embed()'s real logic: batching by batch_size,
empty-string padding (OpenAI rejects ""), input-order preservation, and the
transient-error retry/backoff. The SDK + sleep are stubbed — no network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from derive import openai_client as oc


# ── key_present / _load_key ──────────────────────────────────────────────────

def test_key_present_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "_KEY_PATH", tmp_path / "nokey")
    assert oc.key_present() is False


def test_key_present_empty(tmp_path, monkeypatch):
    p = tmp_path / "key"; p.write_text("   ")
    monkeypatch.setattr(oc, "_KEY_PATH", p)
    assert oc.key_present() is False


def test_key_present_true(tmp_path, monkeypatch):
    p = tmp_path / "key"; p.write_text("sk-abc\n")
    monkeypatch.setattr(oc, "_KEY_PATH", p)
    assert oc.key_present() is True


def test_load_key_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "_KEY_PATH", tmp_path / "nope")
    with pytest.raises(FileNotFoundError):
        oc._load_key()


def test_load_key_empty_raises(tmp_path, monkeypatch):
    p = tmp_path / "key"; p.write_text("\n")
    monkeypatch.setattr(oc, "_KEY_PATH", p)
    with pytest.raises(ValueError):
        oc._load_key()


def test_load_key_strips(tmp_path, monkeypatch):
    p = tmp_path / "key"; p.write_text("  sk-xyz \n")
    monkeypatch.setattr(oc, "_KEY_PATH", p)
    assert oc._load_key() == "sk-xyz"


# ── embed: a recording fake SDK ──────────────────────────────────────────────

class _FakeEmbeddings:
    def __init__(self, calls, fail_first=0):
        self.calls = calls
        self.fail_first = fail_first
        self.attempts = 0

    def create(self, model, input):
        self.attempts += 1
        if self.attempts <= self.fail_first:
            raise RuntimeError("rate limit exceeded")
        self.calls.append(list(input))
        return SimpleNamespace(data=[SimpleNamespace(embedding=[float(len(t))]) for t in input])


def _fake_client(calls, fail_first=0):
    return SimpleNamespace(embeddings=_FakeEmbeddings(calls, fail_first))


def test_embed_empty_returns_empty():
    assert oc.embed([]) == []


def test_embed_batches_by_size(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(oc, "get_client", lambda: _fake_client(calls))
    texts = [f"t{i}" for i in range(250)]
    out = oc.embed(texts, batch_size=100)
    assert len(out) == 250                       # one vector per input
    assert [len(c) for c in calls] == [100, 100, 50]   # batched 100/100/50


def test_embed_pads_empty_strings(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(oc, "get_client", lambda: _fake_client(calls))
    oc.embed(["", "  "], batch_size=10)
    # both empty inputs were replaced with a single space before the API call.
    assert calls[0] == [" ", " "]


def test_embed_retries_transient(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(oc, "time", SimpleNamespace(sleep=lambda *_: None))  # no real backoff
    monkeypatch.setattr(oc, "get_client", lambda: _fake_client(calls, fail_first=1))
    out = oc.embed(["a"], max_retries=3)
    assert len(out) == 1                         # succeeded on the retry


def test_embed_reraises_non_transient(monkeypatch):
    class _Boom:
        def create(self, model, input):
            raise ValueError("invalid request — bad model")
    monkeypatch.setattr(oc, "get_client", lambda: SimpleNamespace(embeddings=_Boom()))
    with pytest.raises(ValueError):
        oc.embed(["a"])

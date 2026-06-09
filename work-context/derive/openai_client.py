"""
openai_client.py — thin wrapper for the OpenAI embeddings + chat endpoints.

Reads the API key from `~/.secrets/openai_api_key` (same pattern as
`~/.secrets/github_pat`, `~/.secrets/anthropic_api_key`). Plain file, key
content only — no JSON wrap, no env-var indirection.

Why a wrapper:
  - Keep the `from openai import OpenAI` import gated behind one module so
    consumers don't fail-import when the package isn't installed yet.
  - Add retry-with-backoff on 429/500/timeout — the SDK retries network
    errors but not rate-limit errors by default.
  - Surface a single `key_present()` check so callers degrade gracefully
    to a local backend when the secret is missing.

Public API
----------
    key_present()                       → bool
    get_client()                        → openai.OpenAI  (raises if no key)
    embed(texts, model='text-embedding-3-small') → list[list[float]]
    chat(messages, model='claude-...?')  → not implemented here; we use
                                            Anthropic SDK for chat. This
                                            module is embedding-only.

Cost reference (Mar 2026 pricing):
    text-embedding-3-small  $0.020/1M tok  1536-dim
    text-embedding-3-large  $0.130/1M tok  3072-dim
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterable, Optional

_KEY_PATH = Path.home() / ".secrets" / "openai_api_key"


def key_present() -> bool:
    """True iff a non-empty key file exists. Cheap; no API call."""
    if not _KEY_PATH.exists():
        return False
    return len(_KEY_PATH.read_text().strip()) > 0


def _load_key() -> str:
    if not _KEY_PATH.exists():
        raise FileNotFoundError(
            f"OpenAI key not found at {_KEY_PATH}. "
            f"Create the file with the key as plain text (no quotes, no JSON)."
        )
    key = _KEY_PATH.read_text().strip()
    if not key:
        raise ValueError(f"OpenAI key file {_KEY_PATH} is empty.")
    return key


def get_client():
    """Lazy import + construct OpenAI client. Raises if `openai` package
    isn't installed or key is missing — callers should check key_present()
    first and fall back to a local backend when False."""
    try:
        from openai import OpenAI
    except ImportError as e:
        raise ImportError(
            "openai package not installed. Run: "
            "$HOME/work-context/.venv/bin/pip install openai"
        ) from e
    return OpenAI(api_key=_load_key())


def embed(
    texts: list[str],
    model: str = "text-embedding-3-small",
    batch_size: int = 100,
    max_retries: int = 3,
) -> list[list[float]]:
    """Embed a batch of texts. Returns one vector per input, in input order.

    Batches calls (OpenAI accepts up to 2048 inputs per request but ~100 is
    a safer default — keeps per-request payload small and surfaces partial
    failures sooner). Retries on transient errors with exponential backoff.

    Empty strings are replaced with a single space (OpenAI rejects empty
    inputs); callers should pre-filter if that matters semantically.
    """
    if not texts:
        return []
    client = get_client()
    # OpenAI rejects empty strings; pad with a space so output indices align.
    safe = [t if (t and t.strip()) else " " for t in texts]
    out: list[list[float]] = []
    for i in range(0, len(safe), batch_size):
        chunk = safe[i : i + batch_size]
        delay = 1.0
        for attempt in range(max_retries):
            try:
                resp = client.embeddings.create(model=model, input=chunk)
                out.extend(d.embedding for d in resp.data)
                break
            except Exception as e:
                last = attempt == max_retries - 1
                if last:
                    raise
                # Common transient: rate_limit_exceeded, timeout, 5xx.
                msg = str(e).lower()
                if "rate" in msg or "timeout" in msg or "5" in msg[:3]:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
    return out


__all__ = ["key_present", "get_client", "embed"]

"""
anthropic_client.py — thin wrapper around the Anthropic SDK.

Keeps key loading + retry policy in one place so callers don't duplicate
boilerplate. Mirrors `derive/openai_client.py` for parity.

Key source: ~/.secrets/anthropic_api_key (one line, no quotes).
"""

from __future__ import annotations

import os
import time
from pathlib import Path


_KEY_PATH = Path.home() / ".secrets" / "anthropic_api_key"


def key_present() -> bool:
    return _KEY_PATH.exists() and _KEY_PATH.read_text().strip() != ""


def _load_key() -> str:
    if not _KEY_PATH.exists():
        raise FileNotFoundError(f"missing {_KEY_PATH} — add Anthropic key first")
    key = _KEY_PATH.read_text().strip()
    if not key:
        raise ValueError(f"{_KEY_PATH} is empty")
    return key


def get_client():
    import anthropic
    return anthropic.Anthropic(api_key=_load_key())


def complete_json(
    prompt: str,
    *,
    model: str = "claude-haiku-4-5",
    max_tokens: int = 600,
    max_retries: int = 3,
    system: str | None = None,
) -> str:
    """Single-turn completion. Returns the text body of the first content
    block. Caller parses JSON.

    Retries with exponential backoff on transient errors (overloaded /
    rate-limit). Fails fast on 4xx model/permission errors.
    """
    import anthropic
    client = get_client()
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            kwargs = dict(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            if system:
                kwargs["system"] = system
            resp = client.messages.create(**kwargs)
            return resp.content[0].text
        except anthropic.APIStatusError as e:
            if e.status_code in (429, 500, 502, 503, 504, 529):
                last_exc = e
                time.sleep(2 ** attempt)
                continue
            raise
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            last_exc = e
            time.sleep(2 ** attempt)
    raise last_exc or RuntimeError("all retries exhausted")


__all__ = ["key_present", "get_client", "complete_json"]

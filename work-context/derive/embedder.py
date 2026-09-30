"""
embedder.py — provider-agnostic embedding facade.

Single swap point for the whole warehouse. `embed_subjects`, `refresh_embeddings`,
`ask_engine`, and `embedding_query` import `embed()` / `available()` / `DEFAULT_MODEL`
from here instead of touching a provider SDK directly. Change the backend once,
here, and every consumer follows.

Backend selection (env `EMBED_BACKEND`, default "bge"):
    bge     → local BAAI/bge-m3 via sentence-transformers. No API, no key.
              1024-dim dense vectors, cosine. Handles English + Hinglish
              (romanized Hindi) in one model, which is why it replaced
              OpenAI text-embedding-3-* after the key expired.
    openai  → derive.openai_client (text-embedding-3-*). Needs the key file
              at ~/.secrets/openai_api_key. Kept as a fallback only.

bge-m3 uses NO instruction prefix — queries and passages are encoded the same
way (unlike bge-*-v1.5 / e5, which need a "Represent this sentence…" prefix).
So the same embed() serves both the subject corpus and on-the-fly query strings.

Public API mirrors the old openai_client surface so each callsite is a 1-line swap:
    backend()                         → str    ("bge" | "openai")
    available()                       → bool   (backend usable right now?)
    embed(texts, model=DEFAULT_MODEL) → list[list[float]]  (one vec per input, in order)
    DEFAULT_MODEL                     → str    (tag written to embedding.model)

Note: switching backend changes DEFAULT_MODEL, which is the `model` tag stored in
the embedding table. The content_sha + model drift check therefore treats every
subject as stale on first run after a swap → a full re-embed. That is intended.
Vector dim also changes (1536 → 1024); do the re-embed as one complete sweep
before running any query, or embedding_query._load_all will try to stack
mixed-dim rows and fail.
"""

from __future__ import annotations

import os

# The bge-m3 weights are pinned + fully cached under ~/.cache/huggingface. The
# corporate proxy 403s HuggingFace's Xet CDN, so any hub round-trip (even an
# etag HEAD on a cached model) risks hanging cron. Force offline: load from the
# local cache only. To re-download / upgrade the model, unset these first.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from derive import openai_client  # fallback backend; import is cheap (SDK is lazy)

_BACKEND = os.environ.get("EMBED_BACKEND", "bge").lower()
_BGE_MODEL_ID = "BAAI/bge-m3"

DEFAULT_MODEL = "text-embedding-3-small" if _BACKEND == "openai" else "bge-m3"

# Lazily-constructed sentence-transformers model (heavy: downloads ~2 GB on
# first use, then cached under ~/.cache/huggingface). Module-level singleton so
# a single process embeds thousands of subjects without reloading weights.
_MODEL = None


def backend() -> str:
    return _BACKEND


def _get_model():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer

        device = "cpu"
        try:
            import torch

            if torch.backends.mps.is_available():   # Apple Silicon
                device = "mps"
            elif torch.cuda.is_available():
                device = "cuda"
        except Exception:
            pass
        _MODEL = SentenceTransformer(_BGE_MODEL_ID, device=device)
        # Cap the context window. bge-m3 defaults to 8192 tokens; attention is
        # O(seq^2), so a few very long subjects (big threads/pages) blow up
        # per-batch memory and OOM the GPU. Subject *topic* is captured in the
        # first ~1024 tokens — plenty for clustering/neighbours — and this
        # bounds memory ~64x vs the 8192 default.
        _MODEL.max_seq_length = 1024
    return _MODEL


def available() -> bool:
    """True iff the selected backend can run right now. Cheap — no model load,
    no network. Callers gate on this and degrade gracefully when False."""
    if _BACKEND == "openai":
        return openai_client.key_present()
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _bge_embed(texts: list[str], batch_size: int = 16) -> list[list[float]]:
    model = _get_model()
    embs = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,   # unit vectors → cosine == dot downstream
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    out = [row.tolist() for row in embs]
    # Release the MPS/CUDA caching allocator between calls. Without this, a
    # full backfill (tens of thousands of subjects in one process) accumulates
    # freed-but-cached device memory and eventually OOMs on the last chunks.
    _empty_device_cache()
    return out


def _empty_device_cache() -> None:
    try:
        import torch

        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        elif torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def embed(texts: list[str], model: str = DEFAULT_MODEL) -> list[list[float]]:
    """Embed a batch; one vector per input, in input order. `model` is accepted
    for signature parity — the openai backend forwards it; the bge backend is
    pinned to bge-m3 and ignores it (the tag it stores is DEFAULT_MODEL)."""
    if not texts:
        return []
    if _BACKEND == "openai":
        return openai_client.embed(texts, model=model)
    # bge rejects nothing, but pad empties so output indices align with input.
    safe = [t if (t and t.strip()) else " " for t in texts]
    return _bge_embed(safe)


__all__ = ["backend", "available", "embed", "DEFAULT_MODEL"]

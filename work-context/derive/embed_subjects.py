"""
embed_subjects.py — embed a set of subjects and persist to the embedding table.

End-to-end:
  1. Resolve list of subjects (CLI args, file, or piped stdin)
  2. For each: fetch embeddable content via subject_content.get_content
  3. Hash content; skip if already embedded with same hash + same model
  4. Batch-embed via OpenAI (or local fallback when key missing)
  5. INSERT OR REPLACE into embedding table

Idempotent: re-running on the same subject set with same model is a no-op
when content hasn't changed.

CLI
---
    # Embed from a subject list (one per line, or piped stdin):
    .venv/bin/python derive/embed_subjects.py --subjects-file /tmp/subjects.txt
    .venv/bin/python derive/sample_subjects.py --target-size 50 | \\
        .venv/bin/python derive/embed_subjects.py --subjects-stdin

    # Embed everything embeddable (production):
    .venv/bin/python derive/embed_subjects.py --all

    # Dry-run: count what WOULD be embedded, no API calls:
    .venv/bin/python derive/embed_subjects.py --all --dry-run

Output JSON to stdout:
    {"requested": N, "skipped_unchanged": N, "skipped_no_content": N,
     "embedded": N, "errors": [...], "model": "...", "dim": N,
     "elapsed_sec": N}
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db  # noqa: E402
from derive.subject_content import get_content, content_sha  # noqa: E402
from derive import openai_client  # noqa: E402

DEFAULT_MODEL = "text-embedding-3-small"


def _pack_vector(v: list[float]) -> bytes:
    """float32 packed; ~6 KB per 1536-dim vector. Smaller than JSON, fast cosine in NumPy."""
    return struct.pack(f"<{len(v)}f", *v)


def _unpack_vector(b: bytes) -> list[float]:
    n = len(b) // 4
    return list(struct.unpack(f"<{n}f", b))


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_subjects(args: argparse.Namespace) -> list[str]:
    """Resolve subject list from --subjects, --subjects-file, --subjects-stdin, or --all."""
    if args.all:
        # Lazy import — avoids loading sampler unless needed.
        from derive.sample_subjects import sample
        conn = get_db()
        return sample(conn, target_size=None)
    out: list[str] = []
    if args.subjects:
        out.extend(args.subjects)
    if args.subjects_file:
        out.extend(
            line.strip()
            for line in Path(args.subjects_file).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        )
    if args.subjects_stdin:
        out.extend(line.strip() for line in sys.stdin if line.strip())
    return out


def _existing_embeddings(conn, subjects: list[str], model: str) -> dict[str, str]:
    """Return {subject: content_sha} for subjects already embedded with this model."""
    if not subjects:
        return {}
    ph = ",".join("?" * len(subjects))
    rows = conn.execute(
        f"SELECT subject, content_sha FROM embedding WHERE model = ? AND subject IN ({ph})",
        (model, *subjects),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def embed_subjects(
    subjects: list[str],
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
    force_reembed: bool = False,
) -> dict:
    """Main entry. Returns a stats dict suitable for JSON-stdout."""
    started = time.time()
    conn = get_db()

    # Phase 1 — resolve content + skip unchanged.
    existing = _existing_embeddings(conn, subjects, model)
    to_embed: list[tuple[str, str, str, str]] = []  # (subject, source, content, sha)
    skipped_unchanged = 0
    skipped_no_content = 0

    for subj in subjects:
        source, content = get_content(conn, subj)
        if not content:
            skipped_no_content += 1
            continue
        sha = content_sha(content)
        if not force_reembed and existing.get(subj) == sha:
            skipped_unchanged += 1
            continue
        to_embed.append((subj, source, content, sha))

    stats = {
        "requested": len(subjects),
        "skipped_unchanged": skipped_unchanged,
        "skipped_no_content": skipped_no_content,
        "to_embed": len(to_embed),
        "model": model,
        "dim": None,
        "embedded": 0,
        "errors": [],
        "elapsed_sec": 0.0,
    }

    if dry_run or not to_embed:
        stats["elapsed_sec"] = round(time.time() - started, 2)
        return stats

    # Phase 2 — provider routing.
    if not openai_client.key_present():
        stats["errors"].append(
            "No OpenAI key at ~/.secrets/openai_api_key. Local fallback not yet "
            "implemented in this scaffold — drop the key file and re-run."
        )
        stats["elapsed_sec"] = round(time.time() - started, 2)
        return stats

    # Phase 3 — batch embed.
    texts = [c for _, _, c, _ in to_embed]
    try:
        vectors = openai_client.embed(texts, model=model)
    except Exception as e:
        stats["errors"].append(f"embed_failed: {type(e).__name__}: {e}")
        stats["elapsed_sec"] = round(time.time() - started, 2)
        return stats

    if len(vectors) != len(to_embed):
        stats["errors"].append(
            f"embedding_count_mismatch: got {len(vectors)} for {len(to_embed)} inputs"
        )
        stats["elapsed_sec"] = round(time.time() - started, 2)
        return stats

    dim = len(vectors[0]) if vectors else 0
    stats["dim"] = dim
    now = _now_iso()

    # Phase 4 — UPSERT.
    with conn:
        for (subj, source, _content, sha), vec in zip(to_embed, vectors):
            conn.execute(
                """INSERT INTO embedding (subject, source, vector, model, dim, content_sha, computed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(subject) DO UPDATE SET
                       source=excluded.source,
                       vector=excluded.vector,
                       model=excluded.model,
                       dim=excluded.dim,
                       content_sha=excluded.content_sha,
                       computed_at=excluded.computed_at""",
                (subj, source, _pack_vector(vec), model, dim, sha, now),
            )
            stats["embedded"] += 1

    stats["elapsed_sec"] = round(time.time() - started, 2)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--subjects", nargs="*", help="Subjects on the command line")
    src.add_argument("--subjects-file", help="Path to file with one subject per line")
    src.add_argument("--subjects-stdin", action="store_true", help="Read subjects from stdin")
    src.add_argument("--all", action="store_true", help="Embed all embeddable subjects in events.db")

    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Embedding model (default: {DEFAULT_MODEL})")
    ap.add_argument("--dry-run", action="store_true", help="No API calls; report what would happen")
    ap.add_argument("--force", action="store_true", help="Re-embed even if content_sha unchanged")
    args = ap.parse_args()

    subjects = _load_subjects(args)
    stats = embed_subjects(
        subjects,
        model=args.model,
        dry_run=args.dry_run,
        force_reembed=args.force,
    )
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()

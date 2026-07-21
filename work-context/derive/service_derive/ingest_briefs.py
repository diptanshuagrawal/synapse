#!/usr/bin/env python3
"""Ingest service-brief markdown into events.db as `source='service'` rows.

This makes DB rows the *canonical* output of a service brief: the `.md` is the
human-readable artifact, the rows are what the embedding + `/ask` pipeline
consumes. Each brief is chunked by section so retrieval hits the right slice:

  - Responsibility           -> service:<svc>#responsibility
  - API endpoints (per H3)   -> service:<svc>#endpoints/<GrpcServiceOrController>
  - Data model               -> service:<svc>#data-model
  - Kafka                    -> service:<svc>#kafka

Downstream / Provenance sections are skipped (placeholder / meta).

Rows are idempotent: existing `service:<svc>#...` rows are deleted then
re-inserted, so removed sections don't linger.

No LLM. No network. Embedding happens later via /refresh-embeddings; clustering
excludes source='service' (see cluster_subjects.py).

Usage:
    python derive/service_derive/ingest_briefs.py                 # all derived/services/*.md
    python derive/service_derive/ingest_briefs.py --svc service-a
    python derive/service_derive/ingest_briefs.py --md path/to/x.md --svc x
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICES_DIR = REPO_ROOT / "derived" / "services"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Unattended fires resolve bare `python3` to the yaml-less system interpreter;
# ingest.common needs yaml, so re-exec under the repo venv before importing it.
try:
    import yaml  # noqa: F401
except ModuleNotFoundError:
    _venv_py = REPO_ROOT / ".venv" / "bin" / "python3"
    if _venv_py.exists() and Path(sys.executable).resolve() != _venv_py.resolve():
        os.execv(str(_venv_py), [str(_venv_py), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise

from ingest.common import delete_events  # noqa: E402  shared deleter (events+refs+fts)

# H2 section title (lowercased, prefix match) -> slug. Others are skipped.
_SECTION_SLUGS = {
    "responsibility": "responsibility",
    "api endpoints": "endpoints",   # split further by H3
    "data model": "data-model",
    "kafka": "kafka",
    "glossary": "glossary",
}
_SPLIT_BY_H3 = {"endpoints"}  # sections chunked per H3 subsection


def _fmt_fields(flds: list[dict], cap: int = 15) -> str:
    out = []
    for f in flds[:cap]:
        t = ("repeated " if f.get("repeated") else "") + f.get("type", "")
        rule = f" [{f['rule']}]" if f.get("rule") else ""
        out.append(f"{f['name']}:{t}{rule}")
    extra = "" if len(flds) <= cap else f", +{len(flds) - cap} more"
    return ", ".join(out) + extra


def endpoint_appendix(skel: dict) -> dict[str, str]:
    """Build {endpoints/<service-slug>: deterministic fields+validation text}
    from the skeleton, to enrich the embedded endpoint chunks (P1+P2)."""
    from collections import defaultdict
    by_svc: dict[str, list] = defaultdict(list)
    for e in skel.get("endpoints", []):
        by_svc[e["service"]].append(e)
    out: dict[str, str] = {}
    for svc_name, eps in by_svc.items():
        lines = ["Request/response fields & validation:"]
        for e in eps:
            req = _fmt_fields(e.get("request_fields", []))
            resp = _fmt_fields(e.get("response_fields", []))
            seg = f"- {e['rpc']}: req({e['request']})"
            if req:
                seg += f" {{{req}}}"
            if resp:
                seg += f"; resp {{{resp}}}"
            lines.append(seg)
        out[f"endpoints/{_slugify(svc_name)}"] = "\n".join(lines)
    return out


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def _section_slug(h2_title: str) -> str | None:
    low = h2_title.lower()
    for prefix, slug in _SECTION_SLUGS.items():
        if low.startswith(prefix):
            return slug
    return None


def chunk_brief(
    svc: str, md_text: str, appendix: dict[str, str] | None = None
) -> list[tuple[str, str, str]]:
    """Return [(slug, title, body), ...]. Pure — no DB. Body carries the
    service name + heading so the chunk is self-describing for retrieval.
    `appendix` maps slug -> extra deterministic text appended to that chunk."""
    appendix = appendix or {}
    lines = md_text.splitlines()
    chunks: list[tuple[str, str, str]] = []

    cur_slug: str | None = None       # active embeddable H2 slug, or None
    split_h3 = False
    sub_title: str | None = None      # active H3 title when splitting
    buf: list[str] = []
    heading: str = ""

    def flush():
        nonlocal buf
        if cur_slug and buf and any(l.strip() for l in buf):
            slug = cur_slug if not (split_h3 and sub_title) else f"{cur_slug}/{_slugify(sub_title)}"
            title = f"{svc} — {heading}" + (f": {sub_title}" if (split_h3 and sub_title) else "")
            body = f"# {title}\n\n" + "\n".join(buf).strip()
            if slug in appendix:
                body += "\n\n" + appendix[slug]
            chunks.append((slug, title, body))
        buf = []

    for line in lines:
        if line.startswith("## "):           # H2 boundary
            flush()
            h2 = line[3:].strip()
            cur_slug = _section_slug(h2)
            split_h3 = cur_slug in _SPLIT_BY_H3
            sub_title = None
            heading = h2
            continue
        if line.startswith("### ") and cur_slug and split_h3:  # H3 boundary inside split section
            flush()
            sub_title = line[4:].strip()
            continue
        if cur_slug:
            buf.append(line)
    flush()
    return chunks


def _today_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def ingest_md(conn: sqlite3.Connection, svc: str, md_path: Path, ts: str) -> int:
    # Pull deterministic endpoint fields+validation from the skeleton (if present)
    # to enrich the embedded endpoint chunks (P1+P2).
    appendix: dict[str, str] = {}
    skel_path = md_path.with_name(f"{svc}.skeleton.json")
    if skel_path.exists():
        import json as _json
        appendix = endpoint_appendix(_json.loads(skel_path.read_text()))
    chunks = chunk_brief(svc, md_path.read_text(errors="replace"), appendix)
    raw_path = str(md_path)
    # idempotent: clear this service's prior chunks first. Route through the
    # shared deleter so any event_refs / events_fts attached to a brief chunk
    # are cascaded too (service rows carry none today, but a bare DELETE FROM
    # events is the exact pattern that leaked orphan refs elsewhere).
    prior_ids = [r[0] for r in conn.execute(
        "SELECT id FROM events WHERE source='service' AND subject LIKE ?",
        (f"service:{svc}#%",),
    ).fetchall()]
    delete_events(conn, prior_ids, commit=False)
    for slug, title, body in chunks:
        subject = f"service:{svc}#{slug}"
        conn.execute(
            """INSERT OR REPLACE INTO events
               (id, source, event_type, ts, subject, title, body, raw_path)
               VALUES (?, 'service', 'service_brief', ?, ?, ?, ?, ?)""",
            (subject, ts, subject, title, body, raw_path),
        )
    conn.commit()
    return len(chunks)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="Explicit DB path; default = canonical get_db().")
    ap.add_argument("--md", default=None, help="Single brief file (requires --svc).")
    ap.add_argument("--svc", default=None, help="Service alias (with --md, or to limit to one).")
    args = ap.parse_args()

    if args.db:
        db = Path(args.db)
        if not db.exists() or db.stat().st_size == 0:
            print(f"error: events.db missing/empty at {db}.", file=sys.stderr)
            return 2
        conn = sqlite3.connect(db)
    else:
        from ingest.common import get_db  # canonical resolver (index/events.db)
        conn = get_db()
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events'").fetchone():
        print("error: events table missing. Run ingest first.", file=sys.stderr)
        return 2

    if args.md:
        if not args.svc:
            print("error: --md requires --svc", file=sys.stderr)
            return 2
        targets = [(args.svc, Path(args.md))]
    else:
        mds = sorted(SERVICES_DIR.glob("*.md"))
        if args.svc:
            mds = [p for p in mds if p.stem == args.svc]
        targets = [(p.stem, p) for p in mds]

    if not targets:
        print("no brief .md files found.", file=sys.stderr)
        return 1

    ts = _today_iso()
    total = 0
    for svc, p in targets:
        n = ingest_md(conn, svc, p, ts)
        total += n
        print(f"  {svc:18} {n:3} chunks  ({p.name})")
    print(f"\ningested {total} chunks from {len(targets)} brief(s) as source='service'.")
    print("Next: /refresh-embeddings to embed them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

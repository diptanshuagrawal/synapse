#!/usr/bin/env python3
"""extract_trd_artifacts.py — deterministic extractor of the concrete, buildable
artifacts from a code-grounded TRD markdown.

The `/pr-from-trd` skill consumes this so it implements EXACTLY what the TRD
specifies — DDL verbatim, endpoint contracts verbatim, open questions carried
into the PR body — instead of re-deriving (and possibly drifting from) the doc.

Pulls:
  - ddl_blocks    : every ```sql fenced block (CREATE/ALTER ...), verbatim.
  - mermaid_blocks: every ```mermaid block (for PR-body embedding).
  - endpoints     : `<METHOD> <path>` + adjacent backtick rpc name from §8.6.
  - open_questions: the numbered "Open questions" list (→ PR checklist).
  - prd_link / trd title / mis-link banner presence.

Zero LLM, zero network. Pure text parse so the PR skill can't invent schema.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

FENCE = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)
EP = re.compile(r"\b(POST|GET|PUT|PATCH|DELETE)\s+(/[^\s`]+)`?(?:\s+`([A-Za-z0-9_.]+)`)?")
PRD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def extract(md: str) -> dict:
    ddl, mermaid = [], []
    for lang, body in FENCE.findall(md):
        lang = (lang or "").lower()
        if lang == "sql":
            ddl.append(body.strip())
        elif lang == "mermaid":
            mermaid.append(body.strip())

    # Endpoints: scan prose lines (skip fenced code to avoid diagram noise).
    no_code = FENCE.sub("", md)
    seen, endpoints = set(), []
    for m in EP.finditer(no_code):
        method, path, rpc = m.group(1), m.group(2).rstrip("`.,)"), m.group(3)
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        endpoints.append({"method": method, "path": path, "rpc": rpc})

    # Open questions: numbered list under an "Open questions" heading.
    open_qs = []
    mq = re.search(r"open questions.*?$", md, re.IGNORECASE | re.MULTILINE)
    if mq:
        tail = md[mq.end():]
        for line in tail.splitlines():
            lm = re.match(r"\s*\d+\.\s+(.*)", line)
            if lm:
                open_qs.append(lm.group(1).strip())
            elif open_qs and not line.strip():
                continue
            elif open_qs and not re.match(r"\s*\d+\.", line) and line.strip().startswith("#"):
                break

    title_m = re.search(r"^#\s+(.*)", md, re.MULTILINE)
    prd = None
    for label, url in PRD_LINK.findall(md):
        if "wiki" in url or "confluence" in url.lower() or "prd" in label.lower():
            prd = {"label": label, "url": url}
            break

    return {
        "title": title_m.group(1).strip() if title_m else None,
        "prd_link": prd,
        "has_mislink_banner": "⚠️" in md and "automatic" in md.lower(),
        "ddl_blocks": ddl,
        "mermaid_blocks": mermaid,
        "endpoints": endpoints,
        "open_questions": open_qs,
        "todo_markers": re.findall(r"\(unknown\)|\(assign\)|\(confirm\)|\(to confirm[^)]*\)", md),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trd", required=True, help="Path to the TRD markdown")
    ap.add_argument("--out", help="default: <trd>.artifacts.json")
    args = ap.parse_args()

    trd_path = Path(args.trd)
    if not trd_path.exists():
        print(f"ERROR: TRD not found: {trd_path}", file=sys.stderr)
        return 2

    art = extract(trd_path.read_text())
    out_path = Path(args.out or str(trd_path).replace(".md", "") + ".artifacts.json")
    out_path.write_text(json.dumps(art, indent=2))

    print(f"trd            : {art['title']}")
    print(f"mislink banner : {art['has_mislink_banner']}")
    print(f"ddl blocks     : {len(art['ddl_blocks'])}")
    print(f"mermaid blocks : {len(art['mermaid_blocks'])}")
    print(f"endpoints      : {len(art['endpoints'])}  {[e['method']+' '+e['path'] for e in art['endpoints']]}")
    print(f"open questions : {len(art['open_questions'])}")
    print(f"todo markers   : {len(art['todo_markers'])} (unknown/assign/confirm in TRD)")
    print(f"written        : {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

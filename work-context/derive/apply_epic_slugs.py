#!/usr/bin/env python3
"""Apply LLM-proposed epic-slug verdicts to projects.yaml.

Reads `state/verdicts.epic_slugs.json` (list of verdicts in the schema
documented in `pending_slug_creation.json.rules.md`) and:

  1. For each verdict with `merge_into`: appends `epic_key` to the existing
     slug's `jira_epics` list.
  2. For each verdict without `merge_into`: appends a new project entry.
  3. Invalidates `subject_summary` cache rows for child subjects of each
     affected epic (so they re-anchor on the next rollup).
  4. Archives the verdicts file with a timestamp suffix and removes the
     pending_slug_creation.json + rules.md.

Run via `manual-rollup.sh apply-slugs`.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
PROJECTS_YAML = ROOT / "config/projects.yaml"
DB_PATH = ROOT / "index/events.db"
STATE_DIR = ROOT / "state"
DEFAULT_VERDICTS = STATE_DIR / "verdicts.epic_slugs.json"
PENDING_PATH = STATE_DIR / "pending_slug_creation.json"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("apply_epic_slugs")


# Same epic-prefix regex used elsewhere (kept local to avoid llm_classifier import side-effects)
EPIC_PREFIX_RE = re.compile(r"\[Epic ([A-Z]+-\d+)\]")


def _load_projects() -> tuple[str, list[dict]]:
    """Return (header_block, projects_list). Header preserves YAML comments."""
    raw = PROJECTS_YAML.read_text()
    header_lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("#") or line.strip() == "":
            header_lines.append(line)
            continue
        break
    header = "\n".join(header_lines).rstrip() + "\n\n" if header_lines else ""
    data = yaml.safe_load(raw) or {}
    return header, data.get("projects", []) or []


def _save_projects(header: str, projects: list[dict]) -> None:
    body = yaml.safe_dump(
        {"projects": projects},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=4096,
    )
    PROJECTS_YAML.write_text(header + body)


def _invalidate_cache_for_epic(conn: sqlite3.Connection, epic_key: str) -> int:
    """Delete subject_summary rows for the epic itself and its children.

    Children are identified by `[Epic <key>]` prefix in their stored event title.
    """
    pat = f"%[Epic {epic_key}]%"
    cur = conn.execute(
        "SELECT DISTINCT subject FROM events "
        "WHERE subject = ? OR title LIKE ?",
        (epic_key, pat),
    )
    subjects = [r[0] for r in cur.fetchall() if r[0]]
    if not subjects:
        return 0
    ph = ",".join("?" * len(subjects))
    conn.execute(f"DELETE FROM subject_summary WHERE subject IN ({ph})", tuple(subjects))
    return len(subjects)


def apply(verdicts_path: Path) -> None:
    if not verdicts_path.exists():
        log.error("missing %s — emit verdicts first", verdicts_path)
        sys.exit(1)
    verdicts = json.loads(verdicts_path.read_text())
    if not isinstance(verdicts, list):
        log.error("verdicts must be a JSON array")
        sys.exit(1)

    header, projects = _load_projects()
    by_slug = {p["slug"]: p for p in projects}

    conn = sqlite3.connect(DB_PATH)

    added = 0
    merged = 0
    invalidated = 0
    for v in verdicts:
        ek = v.get("epic_key")
        if not ek:
            log.warning("skipping verdict with no epic_key: %r", v)
            continue
        merge_into = v.get("merge_into")
        if merge_into:
            target = by_slug.get(merge_into)
            if not target:
                log.error("merge_into target %r not found for epic %s — skipping",
                          merge_into, ek)
                continue
            keys = list(dict.fromkeys((target.get("jira_epics") or []) + [ek]))
            target["jira_epics"] = keys
            merged += 1
            log.info("merge: %s → %s", ek, merge_into)
        else:
            slug = v.get("slug")
            if not slug:
                log.error("verdict for %s missing both slug and merge_into", ek)
                continue
            if slug in by_slug:
                log.error("slug %r already exists — use merge_into for %s", slug, ek)
                continue
            entry = {
                "slug": slug,
                "name": v.get("name") or slug,
                "keywords": v.get("keywords") or [],
                "jira_epics": [ek],
                "confluence_pages": [],
            }
            projects.append(entry)
            by_slug[slug] = entry
            added += 1
            log.info("new: %s ← %s (%s)", slug, ek, entry["name"])
        invalidated += _invalidate_cache_for_epic(conn, ek)

    conn.commit()
    conn.close()

    _save_projects(header, projects)

    # Archive verdicts + clear pending state.
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    archive = STATE_DIR / f"verdicts.epic_slugs.{stamp}.json"
    verdicts_path.rename(archive)
    rules = PENDING_PATH.with_suffix(PENDING_PATH.suffix + ".rules.md")
    for p in (PENDING_PATH, rules):
        if p.exists():
            p.unlink()

    log.info("apply_epic_slugs: +%d new · %d merged · %d cache rows invalidated · archived → %s",
             added, merged, invalidated, archive.name)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(DEFAULT_VERDICTS),
                    help="Path to verdicts.epic_slugs.json")
    args = ap.parse_args()
    apply(Path(args.inp))


if __name__ == "__main__":
    main()

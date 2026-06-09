#!/usr/bin/env python3
"""feature_resolve.py — resolve "a feature" to its artefact set.

Shared helper for the feature-narrative + Feature Score work
(PRD: prd/feature-narrative-scorer.md). Consumed by feature_stages.py,
feature_score.py, and the /feature skill.

A feature is identified by a projects.yaml slug. Input can be:
  * a slug                 ("instant-pay-atm")    → used directly
  * a Jira Epic key        ("EX-2233")            → mapped via jira_epics
  * a feature name / topic  ("Instant-Pay ATM Charges") → matched against name

Resolution returns a FeatureArtefacts bundle: the epics, and every subject
(jira tickets, PRs, confluence pages, slack threads) the classification
pipeline attributed to the slug via event_refs.ref_type='project', plus the
confluence pages declared in projects.yaml. Pure SQL + yaml, no LLM/network.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import get_db  # noqa: E402

PROJECTS_YAML = _REPO_ROOT / "config" / "projects.yaml"
_EPIC_KEY_RE = re.compile(r"^[A-Z]+-\d+$")


@dataclass
class Project:
    slug: str
    name: str
    keywords: list[str] = field(default_factory=list)
    jira_epics: list[str] = field(default_factory=list)
    confluence_pages: list[str] = field(default_factory=list)


@dataclass
class FeatureArtefacts:
    slug: str
    name: str
    epics: list[str]
    jira: list[str]
    github: list[str]       # owner/repo#N
    confluence: list[str]   # page:ID (broad, keyword-attributed)
    slack: list[str]
    declared_confluence: list[str]   # page:ID explicitly listed in projects.yaml
    release_cmrs: list[str]          # CMR subjects that define this feature's releases
    mode: str = "slug"      # slug (domain rollup) | epic (bounded journey)
    epic: str | None = None         # the anchor epic key in epic mode
    anchor_ts: str | None = None    # epic created ts — stages are bounded to >= this
    release_scope: str = "slug"     # how release_cmrs was derived: slug | epic_children

    def counts(self) -> dict[str, int]:
        return {
            "epics": len(self.epics),
            "jira": len(self.jira),
            "github": len(self.github),
            "confluence": len(self.confluence),
            "slack": len(self.slack),
        }


def load_projects() -> list[Project]:
    data = yaml.safe_load(PROJECTS_YAML.read_text())
    raw = data.get("projects", data) if isinstance(data, dict) else data
    out: list[Project] = []
    for p in raw or []:
        if not p.get("slug"):
            continue
        out.append(
            Project(
                slug=p["slug"],
                name=p.get("name") or p["slug"],
                keywords=[str(k) for k in (p.get("keywords") or [])],
                jira_epics=[str(e) for e in (p.get("jira_epics") or [])],
                confluence_pages=[str(c) for c in (p.get("confluence_pages") or [])],
            )
        )
    return out


def resolve_slug(token: str, projects: list[Project] | None = None) -> Project | None:
    """Map a slug / epic key / name to its Project. Returns None if no match."""
    projects = projects or load_projects()
    t = token.strip()
    by_slug = {p.slug: p for p in projects}
    if t in by_slug:
        return by_slug[t]
    tl = t.lower()
    # exact slug case-insensitive
    for p in projects:
        if p.slug.lower() == tl:
            return p
    # epic key → owning project
    if _EPIC_KEY_RE.match(t):
        for p in projects:
            if t in p.jira_epics:
                return p
        return None  # an epic key we don't recognize — caller may run /slug-epics
    # name match (substring, both directions)
    for p in projects:
        if p.name.lower() == tl:
            return p
    for p in projects:
        if tl in p.name.lower() or p.name.lower() in tl:
            return p
    return None


def _subjects_by_project_ref(conn, slug: str) -> dict[str, list[str]]:
    """All subjects the classifier attributed to this slug, grouped by source."""
    rows = conn.execute(
        "SELECT DISTINCT e.source, e.subject FROM event_refs er "
        "JOIN events e ON e.id = er.event_id "
        "WHERE er.ref_type='project' AND er.ref_value=? AND e.subject IS NOT NULL",
        (slug,),
    ).fetchall()
    out: dict[str, list[str]] = {}
    for source, subject in rows:
        out.setdefault(source, []).append(subject)
    return out


def _epic_created_ts(conn, epic_key: str) -> str | None:
    row = conn.execute(
        "SELECT MIN(ts) FROM events WHERE subject=? AND event_type='issue_created'",
        (epic_key,),
    ).fetchone()
    return row[0] if row else None


def epic_children(conn, epic_key: str, issue_type: str | None = None) -> list[str]:
    """Subjects whose ingested title carries the [Epic <key>] prefix. This is the
    parent-epic link the Jira ingest already embeds — recovered here for free,
    no re-fetch. Optionally filter to one issue_type (e.g. 'CMR')."""
    sql = "SELECT DISTINCT subject FROM events WHERE title LIKE ? AND subject IS NOT NULL"
    params: list = [f"[Epic {epic_key}]%"]
    if issue_type:
        sql += " AND issue_type=?"
        params.append(issue_type)
    return [r[0] for r in conn.execute(sql, params).fetchall()]


def _slug_release_cmrs(conn, slug: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT cmr_subject FROM feature_release WHERE slug=? AND is_feature_release=1",
        (slug,),
    ).fetchall()
    return [r[0] for r in rows]


def _epic_release_cmrs(conn, epic_key: str) -> list[str]:
    """CMRs childed to the epic (title prefix) that have a feature_release row."""
    kids = epic_children(conn, epic_key, issue_type="CMR")
    if not kids:
        return []
    ph = ",".join("?" for _ in kids)
    rows = conn.execute(
        f"SELECT DISTINCT cmr_subject FROM feature_release "
        f"WHERE is_feature_release=1 AND cmr_subject IN ({ph})",
        kids,
    ).fetchall()
    return [r[0] for r in rows]


def resolve_artefacts(conn, project: Project, epic: str | None = None) -> FeatureArtefacts:
    """Build the artefact bundle. If `epic` is set, run in epic-anchored mode:
    the slug supplies the artefact pool, but stages get bounded to the epic's
    creation ts (set on the bundle as anchor_ts)."""
    by_src = _subjects_by_project_ref(conn, project.slug)
    declared = [f"page:{pid}" for pid in project.confluence_pages]
    confluence = sorted(set(by_src.get("confluence", [])) | set(declared))
    mode = "epic" if epic else "slug"
    anchor_ts = _epic_created_ts(conn, epic) if epic else None
    epics = [epic] if epic else sorted(set(project.jira_epics))

    # jira scope: epic mode = the epic's own children (precise); slug = whole pool
    if epic:
        jira = sorted(set(epic_children(conn, epic)) | {epic})
    else:
        jira = sorted(set(by_src.get("jira", [])) | set(project.jira_epics))

    # release driver: epic mode prefers the epic's own CMRs, falls back to the
    # slug's feature CMRs (which stage computation then bounds by anchor_ts)
    if epic:
        epic_cmrs = _epic_release_cmrs(conn, epic)
        if epic_cmrs:
            release_cmrs, release_scope = epic_cmrs, "epic_children"
        else:
            release_cmrs, release_scope = _slug_release_cmrs(conn, project.slug), "slug"
    else:
        release_cmrs, release_scope = _slug_release_cmrs(conn, project.slug), "slug"

    return FeatureArtefacts(
        slug=project.slug,
        name=project.name,
        epics=epics,
        jira=jira,
        github=sorted(set(by_src.get("github", []))),
        confluence=confluence,
        slack=sorted(set(by_src.get("slack", []))),
        declared_confluence=sorted(set(declared)),
        release_cmrs=release_cmrs,
        mode=mode,
        epic=epic,
        anchor_ts=anchor_ts,
        release_scope=release_scope,
    )


def resolve(conn, token: str) -> FeatureArtefacts | None:
    project = resolve_slug(token)
    if project is None:
        return None
    # epic key that belongs to this project → epic-anchored mode
    epic = token.strip() if (_EPIC_KEY_RE.match(token.strip()) and token.strip() in project.jira_epics) else None
    return resolve_artefacts(conn, project, epic=epic)


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve a feature to its artefact set.")
    ap.add_argument("token", help="slug, Jira epic key, or feature name")
    ap.add_argument("--json", action="store_true", help="emit full artefact lists as JSON")
    args = ap.parse_args()

    conn = get_db()
    fa = resolve(conn, args.token)
    if fa is None:
        print(f"no feature matched {args.token!r} (try a slug from config/projects.yaml or /slug-epics)")
        return 1
    if args.json:
        print(json.dumps(fa.__dict__, indent=2))
    else:
        tag = f"epic:{fa.epic} (bounded @ {fa.anchor_ts[:10]})" if fa.mode == "epic" and fa.anchor_ts else "domain rollup"
        print(f"{fa.slug}  ({fa.name})  [{tag}]")
        print(f"  epics:      {', '.join(fa.epics) or '—'}")
        for k, v in fa.counts().items():
            print(f"  {k:<11} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

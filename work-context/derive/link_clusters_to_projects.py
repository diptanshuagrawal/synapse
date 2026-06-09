"""
link_clusters_to_projects.py — populate `cluster_project_map`.

For each non-RECURRING cluster in `topic_brief`, decide which `projects.yaml`
slug(s) it belongs to. Sources of signal (highest confidence first):

  1. subject_summary.domains — per-member LLM-classified slug list.
     If ≥50% of mappable members agree on a slug → conf 0.95.
  2. confluence_page direct match — cluster has member `page:NNNNNNN` AND
     NNNNNNN ∈ projects.yaml::confluence_pages[slug] → conf 0.90.
  3. jira_epic match — cluster has jira member whose epic_key
     (from `[Epic EX-XXXX]` title prefix or subject itself) ∈
     projects.yaml::jira_epics[slug] → conf 0.85.
  4. keyword fallback — cluster.label OR cluster.summary substring-hits
     a keyword from projects.yaml::keywords[slug] → conf 0.60.

One cluster may link to multiple slugs (e.g. instant-pay rollout cluster links to
both `instant-pay-on-service-a` and `service-c-accounting-revamp`). Deduped by (cluster_id,
project_slug); highest-confidence source wins on conflict.

Skipped:
  - status=RECURRING clusters (channel-joins, alerts, daily reports).
  - clusters with member_count < 3 (too thin to attribute).

CLI
---
    .venv/bin/python derive/link_clusters_to_projects.py status
        Print summary: total clusters, linked, unmapped. No writes.

    .venv/bin/python derive/link_clusters_to_projects.py plan
        Emit JSON plan to stdout. No writes.

    .venv/bin/python derive/link_clusters_to_projects.py apply
        Persist to cluster_project_map table (UPSERT by PK).

    .venv/bin/python derive/link_clusters_to_projects.py unmapped
        Print clusters that no rule matched — surface gaps in projects.yaml.

Hard constraints
----------------
- No LLM calls. Deterministic over projects.yaml + topic_brief + subject_summary.
- `--apply` only mutates cluster_project_map; never touches topic_brief.
- Idempotent — re-running over same DB state produces same map.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db  # noqa: E402


PROJECTS_YAML = _PKG_ROOT / "config" / "projects.yaml"
MIGRATION_SQL = _PKG_ROOT / "derive" / "migrations" / "008_cluster_project_map.sql"

# tunables
MIN_CLUSTER_MEMBERS = 3
DOMAIN_AGREEMENT_MIN_RATIO = 0.50
CONF_DOMAIN_AGREEMENT = 0.95
CONF_CONFLUENCE_PAGE = 0.90
CONF_JIRA_EPIC = 0.85
CONF_KEYWORD = 0.60
KEYWORD_MIN_LEN = 6  # avoid single-token keyword noise like "withholding" or "service-c"

EPIC_PREFIX_RE = re.compile(r"\[Epic ([A-Z]+-\d+)\]")
JIRA_KEY_RE = re.compile(r"^([A-Z]+-\d+)$")
PAGE_SUBJECT_RE = re.compile(r"^page:(\d+)$")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── projects.yaml indexing ──────────────────────────────────────────────────


def _build_project_index(projects: list[dict]) -> dict:
    """Return {epic_key→slug, page_id→slug, keyword_lower→slug, slugs→{name, keywords}}."""
    epic_to_slug: dict[str, str] = {}
    page_to_slug: dict[int, str] = {}
    keyword_to_slug: dict[str, str] = {}
    slug_meta: dict[str, dict] = {}
    for p in projects:
        slug = p["slug"]
        slug_meta[slug] = {"name": p.get("name", slug), "keywords": p.get("keywords", [])}
        for epic in p.get("jira_epics") or []:
            epic_to_slug[epic] = slug
        for page in p.get("confluence_pages") or []:
            try:
                page_to_slug[int(page)] = slug
            except (TypeError, ValueError):
                continue
        for kw in p.get("keywords") or []:
            kw_lower = kw.lower().strip()
            if len(kw_lower) < KEYWORD_MIN_LEN:
                continue
            # if multiple slugs claim the same keyword, the *first* slug wins
            # (yaml order = priority); not perfect but deterministic.
            keyword_to_slug.setdefault(kw_lower, slug)
    return {
        "epic_to_slug": epic_to_slug,
        "page_to_slug": page_to_slug,
        "keyword_to_slug": keyword_to_slug,
        "slug_meta": slug_meta,
    }


# ── helpers ─────────────────────────────────────────────────────────────────


def _extract_epic_key(title: str | None, subject: str) -> str | None:
    """Pull epic key from title prefix `[Epic EX-NNNN]`; fallback to subject if it
    looks like a jira key."""
    if title:
        m = EPIC_PREFIX_RE.search(title)
        if m:
            return m.group(1)
    if JIRA_KEY_RE.match(subject or ""):
        # Subject itself may be an epic (a jira ticket whose issue_type == Epic).
        return subject
    return None


def _fetch_cluster_members(conn: sqlite3.Connection, cluster_id: int) -> list[dict]:
    """Return [{subject, source, title}, ...] for the cluster."""
    rows = conn.execute(
        """
        SELECT tbm.subject, tbm.source, e.title
          FROM topic_brief_member tbm
          LEFT JOIN events e ON e.subject = tbm.subject
         WHERE tbm.cluster_id = ?
         GROUP BY tbm.subject  -- one row per subject (events may have many rows)
        """,
        (cluster_id,),
    ).fetchall()
    out = []
    for subj, src, title in rows:
        out.append({"subject": subj, "source": src, "title": title or ""})
    return out


def _fetch_subject_domains(conn: sqlite3.Connection, subjects: list[str]) -> dict[str, list[str]]:
    """{subject → [slug, ...]} from subject_summary cache. Missing rows omitted."""
    if not subjects:
        return {}
    placeholders = ",".join("?" * len(subjects))
    rows = conn.execute(
        f"SELECT subject, domains FROM subject_summary WHERE subject IN ({placeholders})",
        subjects,
    ).fetchall()
    out: dict[str, list[str]] = {}
    for subj, domains_json in rows:
        if not domains_json:
            continue
        try:
            d = json.loads(domains_json)
        except json.JSONDecodeError:
            continue
        if isinstance(d, list) and d:
            out[subj] = [str(s) for s in d if isinstance(s, str)]
    return out


# ── link rules ──────────────────────────────────────────────────────────────


def _rule_domain_agreement(members: list[dict], domains_map: dict[str, list[str]]) -> list[dict]:
    """Rule 1 — domain agreement across subject_summary."""
    counts: Counter = Counter()
    mappable = 0
    for m in members:
        slugs = domains_map.get(m["subject"])
        if not slugs:
            continue
        mappable += 1
        for s in slugs:
            counts[s] += 1
    if mappable < MIN_CLUSTER_MEMBERS:
        return []
    out = []
    for slug, n in counts.items():
        ratio = n / mappable
        if ratio >= DOMAIN_AGREEMENT_MIN_RATIO:
            out.append({
                "slug": slug,
                "confidence": min(CONF_DOMAIN_AGREEMENT, 0.70 + 0.25 * ratio),
                "source": "subject_summary_domains",
                "evidence": {"matched_members": n, "mappable_members": mappable, "ratio": round(ratio, 3)},
            })
    return out


def _rule_confluence_page(members: list[dict], page_to_slug: dict[int, str]) -> list[dict]:
    """Rule 2 — direct confluence_pages mapping."""
    hits: dict[str, list[int]] = defaultdict(list)
    for m in members:
        match = PAGE_SUBJECT_RE.match(m["subject"])
        if not match:
            continue
        page_id = int(match.group(1))
        slug = page_to_slug.get(page_id)
        if slug:
            hits[slug].append(page_id)
    return [
        {
            "slug": slug,
            "confidence": CONF_CONFLUENCE_PAGE,
            "source": "confluence_page",
            "evidence": {"matched_pages": pages},
        }
        for slug, pages in hits.items()
    ]


def _rule_jira_epic(members: list[dict], epic_to_slug: dict[str, str]) -> list[dict]:
    """Rule 3 — jira_epic mapping via title prefix or subject."""
    hits: dict[str, set[str]] = defaultdict(set)
    for m in members:
        if m["source"] != "jira":
            continue
        epic = _extract_epic_key(m.get("title"), m["subject"])
        if not epic:
            continue
        slug = epic_to_slug.get(epic)
        if slug:
            hits[slug].add(epic)
    return [
        {
            "slug": slug,
            "confidence": CONF_JIRA_EPIC,
            "source": "jira_epic",
            "evidence": {"matched_epics": sorted(epics)},
        }
        for slug, epics in hits.items()
    ]


def _rule_keyword(label: str | None, summary: str | None, keyword_to_slug: dict[str, str]) -> list[dict]:
    """Rule 4 — keyword substring against cluster label + summary."""
    haystack = " ".join([label or "", summary or ""]).lower()
    if not haystack.strip():
        return []
    hits: dict[str, list[str]] = defaultdict(list)
    for kw, slug in keyword_to_slug.items():
        if kw in haystack:
            hits[slug].append(kw)
    return [
        {
            "slug": slug,
            "confidence": CONF_KEYWORD,
            "source": "keyword",
            "evidence": {"matched_keywords": kws},
        }
        for slug, kws in hits.items()
    ]


# ── link computation ────────────────────────────────────────────────────────


def link_cluster(cluster_row: dict, members: list[dict], domains_map: dict[str, list[str]], index: dict) -> list[dict]:
    """Aggregate all rule hits; collapse to one entry per slug (highest conf wins)."""
    candidates: list[dict] = []
    candidates += _rule_domain_agreement(members, domains_map)
    candidates += _rule_confluence_page(members, index["page_to_slug"])
    candidates += _rule_jira_epic(members, index["epic_to_slug"])
    candidates += _rule_keyword(cluster_row.get("label"), cluster_row.get("summary"), index["keyword_to_slug"])

    # collapse to best-per-slug
    best: dict[str, dict] = {}
    for c in candidates:
        slug = c["slug"]
        cur = best.get(slug)
        if cur is None or c["confidence"] > cur["confidence"]:
            best[slug] = c
    return list(best.values())


def compute_plan(conn: sqlite3.Connection, index: dict) -> dict:
    """Run linker over all non-RECURRING clusters with >= MIN_CLUSTER_MEMBERS members."""
    rows = conn.execute(
        """
        SELECT cluster_id, label, summary, status, member_count
          FROM topic_brief
         WHERE status != 'RECURRING'
           AND member_count >= ?
        """,
        (MIN_CLUSTER_MEMBERS,),
    ).fetchall()

    plan: list[dict] = []
    unmapped: list[int] = []
    for cid, label, summary, status, mc in rows:
        members = _fetch_cluster_members(conn, cid)
        domains_map = _fetch_subject_domains(conn, [m["subject"] for m in members])
        links = link_cluster(
            {"cluster_id": cid, "label": label, "summary": summary, "status": status},
            members,
            domains_map,
            index,
        )
        if not links:
            unmapped.append(cid)
            continue
        for lk in links:
            plan.append({
                "cluster_id": cid,
                "project_slug": lk["slug"],
                "confidence": round(lk["confidence"], 3),
                "source": lk["source"],
                "evidence": lk["evidence"],
            })

    # source-breakdown stats
    src_counts: Counter = Counter(p["source"] for p in plan)
    slugs_distinct = len({p["project_slug"] for p in plan})
    clusters_linked = len({p["cluster_id"] for p in plan})

    return {
        "computed_at": _now_iso(),
        "clusters_eligible": len(rows),
        "clusters_linked": clusters_linked,
        "clusters_unmapped": len(unmapped),
        "unmapped_ids": unmapped,
        "links_total": len(plan),
        "slugs_used": slugs_distinct,
        "source_breakdown": dict(src_counts),
        "links": plan,
    }


# ── persistence ─────────────────────────────────────────────────────────────


def ensure_schema(conn: sqlite3.Connection) -> None:
    sql = MIGRATION_SQL.read_text()
    conn.executescript(sql)
    conn.commit()


def apply_plan(conn: sqlite3.Connection, plan: dict) -> dict:
    """UPSERT every link in plan; delete stale links for clusters present in plan."""
    ensure_schema(conn)
    ts = _now_iso()
    # Delete existing rows for clusters covered by this run (idempotent refresh).
    touched_clusters = {p["cluster_id"] for p in plan["links"]}
    if touched_clusters:
        placeholders = ",".join("?" * len(touched_clusters))
        conn.execute(
            f"DELETE FROM cluster_project_map WHERE cluster_id IN ({placeholders})",
            tuple(touched_clusters),
        )
    inserted = 0
    for lk in plan["links"]:
        conn.execute(
            """INSERT OR REPLACE INTO cluster_project_map
                  (cluster_id, project_slug, confidence, source, evidence_json, computed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                lk["cluster_id"],
                lk["project_slug"],
                lk["confidence"],
                lk["source"],
                json.dumps(lk["evidence"]),
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    return {"applied_at": ts, "rows_inserted": inserted, "clusters_touched": len(touched_clusters)}


# ── commands ────────────────────────────────────────────────────────────────


def _load_index() -> dict:
    raw = yaml.safe_load(PROJECTS_YAML.read_text())
    return _build_project_index(raw.get("projects", []))


def cmd_status(_args):
    conn = get_db()
    index = _load_index()
    plan = compute_plan(conn, index)
    out = {k: v for k, v in plan.items() if k not in ("links", "unmapped_ids")}
    out["unmapped_head"] = plan["unmapped_ids"][:15]
    print(json.dumps(out, indent=2))


def cmd_plan(_args):
    conn = get_db()
    index = _load_index()
    plan = compute_plan(conn, index)
    print(json.dumps(plan, indent=2))


def cmd_apply(_args):
    conn = get_db()
    index = _load_index()
    plan = compute_plan(conn, index)
    result = apply_plan(conn, plan)
    summary = {
        "clusters_eligible": plan["clusters_eligible"],
        "clusters_linked": plan["clusters_linked"],
        "clusters_unmapped": plan["clusters_unmapped"],
        "links_total": plan["links_total"],
        "source_breakdown": plan["source_breakdown"],
        "apply": result,
    }
    print(json.dumps(summary, indent=2))
    if plan["clusters_unmapped"] > 0:
        print(
            f"\nℹ {plan['clusters_unmapped']} clusters unmapped — run `unmapped` to surface gaps.",
            file=sys.stderr,
        )


def cmd_unmapped(_args):
    conn = get_db()
    index = _load_index()
    plan = compute_plan(conn, index)
    if not plan["unmapped_ids"]:
        print("✓ all eligible clusters mapped.")
        return
    # Pull label + member_count + first_ts for each unmapped cluster for owner triage.
    placeholders = ",".join("?" * len(plan["unmapped_ids"]))
    rows = conn.execute(
        f"""SELECT cluster_id, label, status, member_count, last_activity_ts
              FROM topic_brief
             WHERE cluster_id IN ({placeholders})
             ORDER BY member_count DESC""",
        tuple(plan["unmapped_ids"]),
    ).fetchall()
    print(f"{len(rows)} unmapped clusters (no rule matched):\n")
    for cid, label, status, mc, last in rows:
        print(f"  cluster_id={cid:>4} mem={mc:>3} status={status:<8} last={(last or 'n/a')[:10]} | {(label or '(no label)')[:80]}")


# ── main ────────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn, helptext in [
        ("status", cmd_status, "Summary counts only. No writes."),
        ("plan", cmd_plan, "Emit full JSON plan to stdout. No writes."),
        ("apply", cmd_apply, "Persist plan to cluster_project_map. Idempotent."),
        ("unmapped", cmd_unmapped, "List clusters that no rule matched — surface projects.yaml gaps."),
    ]:
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(fn=fn)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

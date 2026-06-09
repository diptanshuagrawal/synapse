#!/usr/bin/env python3
"""cmr_releases.py — parse CMR tickets into the feature_release table.

Phase 1 of the feature-narrative + Feature Score work
(PRD: prd/feature-narrative-scorer.md).

A CMR (issue_type='CMR') is the org's release record. Folks run a release
workflow in the release-service-c / example-releases Slack channels, create a CMR, and
take owner approval on it. This script turns each CMR into structured rows:

  * Body parse  — "Service: … PR: <github url> Impacted Areas: … Owner of
                  release: …" (a ~28% structured subset; the rest are DB-ops /
                  balance / config CMRs that carry no PR link).
  * Lifecycle   — status_change transitions give approval_requested_at,
                  approved_at, released_at, and a terminal `outcome`
                  (released | emergency | rolled_back | cancelled | pending).
  * Approver    — the human who commented "Approved" (the Change-Approved
                  transition itself is fired by the Jira automation bot).
  * Attribution — slug(s) come from the project refs the classification
                  pipeline already attached (event_refs.ref_type='project');
                  fallback is an Impacted-Areas keyword match against
                  projects.yaml. A release touching N features writes N rows.

is_feature_release = 0 marks ops CMRs (no PR link, no project ref, no keyword
match) so feature scoring can exclude them.

Deterministic — no LLM, no network. Re-runnable (UPSERT). Run after ingest.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import get_db  # noqa: E402

PROJECTS_YAML = _REPO_ROOT / "config" / "projects.yaml"

# ── body field extraction ───────────────────────────────────────────────────
# The template runs fields together on one line ("…service-c TransactionsPR: https…"),
# so each field is captured up to the next known label (or end of body).
_FIELD_LABELS = ["Service:", "PR:", "Impacted Areas:", "Owner of release:"]
_NEXT_LABEL = "|".join(re.escape(lbl) for lbl in _FIELD_LABELS)


def _field(body: str, label: str) -> str | None:
    """Extract one templated field, stopping at the next label or end."""
    m = re.search(
        re.escape(label) + r"\s*(.*?)\s*(?=" + _NEXT_LABEL + r"|----|\bBeta testing\b|\bChanges merged\b|$)",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    val = m.group(1).strip()
    return val or None


_PR_URL_RE = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.IGNORECASE)


def _pr_subjects(body: str) -> list[str]:
    """All github PR links in the body, normalized to owner/repo#N (deduped)."""
    out: list[str] = []
    for owner, repo, num in _PR_URL_RE.findall(body):
        subj = f"{owner}/{repo}#{num}"
        if subj not in out:
            out.append(subj)
    return out


# ── status lifecycle ─────────────────────────────────────────────────────────
_APPROVAL_REQ = {"approval requested", "request approval", "awaiting approvals"}


def _outcome(statuses: list[str]) -> str:
    """Terminal outcome from the set of to_status values seen (priority order)."""
    low = [s.lower() for s in statuses]
    if any("rolled back" in s for s in low):
        return "rolled_back"
    if any("emergency" in s for s in low):
        return "emergency"
    if any("released" in s for s in low):
        return "released"
    if any("cancelled" in s or "canceled" in s for s in low):
        return "cancelled"
    return "pending"


def _approved_by(comments: list[tuple[str, str, str]], approved_at: str | None) -> str | None:
    """Human approver: author of the last short 'Approved' comment at/before the
    Change-Approved transition. comments = [(ts, actor, body), …] sorted by ts."""
    candidate = None
    for ts, actor, body in comments:
        b = (body or "").strip().lower()
        if not b.startswith("approved") or len(b) > 40:
            continue
        if approved_at and ts > approved_at:
            break
        candidate = actor
    return candidate


# ── projects.yaml keyword fallback ───────────────────────────────────────────
def _load_project_keywords() -> list[tuple[str, list[str]]]:
    data = yaml.safe_load(PROJECTS_YAML.read_text())
    projects = data.get("projects", data) if isinstance(data, dict) else data
    out: list[tuple[str, list[str]]] = []
    for p in projects or []:
        slug = p.get("slug")
        if not slug:
            continue
        kws = [str(k).lower() for k in (p.get("keywords") or []) if k]
        out.append((slug, kws))
    return out


def _slugs_from_impacted(impacted: str | None, kw_index: list[tuple[str, list[str]]]) -> list[str]:
    if not impacted or impacted.strip().lower() in ("none", "na", "n/a", ""):
        return []
    hay = impacted.lower()
    return [slug for slug, kws in kw_index if any(kw in hay for kw in kws)]


# ── core ──────────────────────────────────────────────────────────────────────
def parse_cmrs(conn, limit: int | None = None) -> list[dict]:
    cur = conn.cursor()
    subj_rows = cur.execute(
        "SELECT DISTINCT subject FROM events WHERE issue_type='CMR' AND subject IS NOT NULL ORDER BY subject"
    ).fetchall()
    subjects = [r[0] for r in subj_rows]
    if limit:
        subjects = subjects[:limit]

    kw_index = _load_project_keywords()
    records: list[dict] = []

    for cmr in subjects:
        events = cur.execute(
            "SELECT event_type, ts, actor, to_status, body, title, url "
            "FROM events WHERE subject=? ORDER BY ts",
            (cmr,),
        ).fetchall()
        if not events:
            continue

        created_at = title = url = body = None
        statuses: list[tuple[str, str]] = []   # (ts, to_status)
        comments: list[tuple[str, str, str]] = []  # (ts, actor, body)

        for etype, ts, actor, to_status, ebody, etitle, eurl in events:
            if etype == "issue_created":
                created_at = ts
                body = ebody or ""
                title = etitle
                url = eurl
            elif etype == "status_change" and to_status:
                statuses.append((ts, to_status))
            elif etype == "comment":
                comments.append((ts, actor, ebody or ""))
        body = body or ""

        # lifecycle timestamps
        approval_requested_at = next(
            (ts for ts, st in statuses if st.strip().lower() in _APPROVAL_REQ), None
        )
        approved_at = next((ts for ts, st in statuses if st.strip().lower() == "change approved"), None)
        released_at = next((ts for ts, st in statuses if "released" in st.lower()), None)
        outcome = _outcome([st for _, st in statuses])
        approved_by = _approved_by(comments, approved_at)

        # body fields
        service = _field(body, "Service:")
        impacted = _field(body, "Impacted Areas:")
        owner = _field(body, "Owner of release:")
        pr_subjects = _pr_subjects(body)

        # slug attribution
        proj_refs = [
            r[0]
            for r in cur.execute(
                "SELECT DISTINCT er.ref_value FROM event_refs er "
                "JOIN events e ON e.id = er.event_id "
                "WHERE e.subject=? AND er.ref_type='project'",
                (cmr,),
            ).fetchall()
        ]
        if proj_refs:
            slugs, linked_via = proj_refs, "project_ref"
        else:
            kw_slugs = _slugs_from_impacted(impacted, kw_index)
            slugs, linked_via = (kw_slugs, "impacted_areas") if kw_slugs else ([""], "none")

        is_feature_release = 1 if (pr_subjects or proj_refs or linked_via == "impacted_areas") else 0

        for slug in slugs:
            records.append(
                {
                    "cmr_subject": cmr,
                    "slug": slug,
                    "linked_via": linked_via if slug else "none",
                    "service": service,
                    "impacted_areas": impacted,
                    "pr_urls_json": json.dumps(pr_subjects),
                    "release_owner": owner,
                    "created_at": created_at,
                    "approval_requested_at": approval_requested_at,
                    "approved_at": approved_at,
                    "approved_by": approved_by,
                    "released_at": released_at,
                    "outcome": outcome,
                    "is_feature_release": is_feature_release,
                    "title": title,
                    "url": url,
                }
            )
    return records


_COLS = [
    "cmr_subject", "slug", "linked_via", "service", "impacted_areas", "pr_urls_json",
    "release_owner", "created_at", "approval_requested_at", "approved_at", "approved_by",
    "released_at", "outcome", "is_feature_release", "title", "url", "computed_at",
]


def write_releases(conn, records: list[dict], rebuild: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    if rebuild:
        cur.execute("DELETE FROM feature_release")
    placeholders = ",".join("?" for _ in _COLS)
    updates = ",".join(f"{c}=excluded.{c}" for c in _COLS if c not in ("cmr_subject", "slug"))
    sql = (
        f"INSERT INTO feature_release ({','.join(_COLS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(cmr_subject, slug) DO UPDATE SET {updates}"
    )
    for r in records:
        r["computed_at"] = now
        cur.execute(sql, [r[c] for c in _COLS])
    conn.commit()


def print_stats(conn) -> None:
    cur = conn.cursor()
    total_cmrs = cur.execute("SELECT COUNT(DISTINCT cmr_subject) FROM feature_release").fetchone()[0]
    rows = cur.execute("SELECT COUNT(*) FROM feature_release").fetchone()[0]
    feat = cur.execute(
        "SELECT COUNT(DISTINCT cmr_subject) FROM feature_release WHERE is_feature_release=1"
    ).fetchone()[0]
    attributed = cur.execute(
        "SELECT COUNT(DISTINCT cmr_subject) FROM feature_release WHERE slug != ''"
    ).fetchone()[0]
    print(f"CMRs parsed:          {total_cmrs}")
    print(f"feature_release rows: {rows}")
    print(f"feature releases:     {feat}  (is_feature_release=1)")
    print(f"slug-attributed CMRs: {attributed}")
    print("\noutcome breakdown:")
    for outcome, n in cur.execute(
        "SELECT outcome, COUNT(DISTINCT cmr_subject) FROM feature_release GROUP BY outcome ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {outcome:<12} {n}")
    print("\nlinked_via breakdown:")
    for lv, n in cur.execute(
        "SELECT linked_via, COUNT(*) FROM feature_release GROUP BY linked_via ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {lv:<16} {n}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse CMR tickets into feature_release.")
    ap.add_argument("--limit", type=int, default=None, help="only parse the first N CMRs (debug)")
    ap.add_argument("--rebuild", action="store_true", help="wipe feature_release before writing")
    ap.add_argument("--stats", action="store_true", help="print summary after writing")
    ap.add_argument("--dry-run", action="store_true", help="parse + report counts, do not write")
    args = ap.parse_args()

    conn = get_db()
    records = parse_cmrs(conn, limit=args.limit)
    print(f"parsed {len(records)} feature_release rows from CMRs")
    if args.dry_run:
        return 0
    write_releases(conn, records, rebuild=args.rebuild)
    if args.stats:
        print_stats(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

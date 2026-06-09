#!/usr/bin/env python3
"""feature_stages.py — compute a feature's lifecycle stage timeline.

Phase 1 of the feature-narrative + Feature Score work
(PRD: prd/feature-narrative-scorer.md). Reads the artefact set from
feature_resolve and the release records from feature_release, then derives the
first-entered timestamp for each of the four stages:

  planning  — earliest issue_created among the feature's Jira epics
              (fallback: earliest issue_created among its Jira tickets)
  trd       — earliest Confluence page_created / page_updated for the feature
  code_dev  — earliest GitHub event for the feature's PRs (pr_opened is sparse,
              so any github event type counts as "dev started")
  rollout   — earliest CMR `Released` ts (from feature_release, feature rows)

A stage with no signal is simply not written (the narrative renders
"not detected"). Deterministic — no LLM, no network. Re-runnable (UPSERT).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import get_db  # noqa: E402
from derive.feature_resolve import FeatureArtefacts, load_projects, resolve  # noqa: E402

STAGE_ORDER = ["planning", "trd", "code_dev", "rollout"]


def _min_ts(conn, subjects: list[str], event_types: list[str] | None = None,
            after_ts: str | None = None) -> str | None:
    if not subjects:
        return None
    ph = ",".join("?" for _ in subjects)
    sql = f"SELECT MIN(ts) FROM events WHERE subject IN ({ph})"
    params = list(subjects)
    if event_types:
        tph = ",".join("?" for _ in event_types)
        sql += f" AND event_type IN ({tph})"
        params += event_types
    if after_ts:
        sql += " AND ts >= ?"
        params.append(after_ts)
    row = conn.execute(sql, params).fetchone()
    return row[0] if row and row[0] else None


def _shipped_pr_subjects(conn, release_cmrs: list[str]) -> list[str]:
    """The exact PRs the feature's CMRs shipped (precise PR-in-CMR link),
    far tighter than the broad keyword-attributed github pool."""
    if not release_cmrs:
        return []
    ph = ",".join("?" for _ in release_cmrs)
    rows = conn.execute(
        f"SELECT pr_urls_json FROM feature_release WHERE cmr_subject IN ({ph})",
        release_cmrs,
    ).fetchall()
    out: set[str] = set()
    for (j,) in rows:
        try:
            out.update(json.loads(j) or [])
        except (json.JSONDecodeError, TypeError):
            continue
    return sorted(out)


def _rollout(conn, release_cmrs: list[str], after_ts: str | None = None) -> dict | None:
    """Rollout stage from feature_release for the given CMR set (feature rows)."""
    if not release_cmrs:
        return None
    ph = ",".join("?" for _ in release_cmrs)
    sql = (f"SELECT DISTINCT cmr_subject, outcome, released_at FROM feature_release "
           f"WHERE is_feature_release=1 AND cmr_subject IN ({ph})")
    params: list = list(release_cmrs)
    if after_ts:
        sql += " AND (released_at IS NULL OR released_at >= ?)"
        params.append(after_ts)
    rows = [(r[1], r[2]) for r in conn.execute(sql, params).fetchall()]
    if not rows:
        return None
    by_outcome: dict[str, int] = {}
    released_ts = [r[1] for r in rows if r[1]]
    for outcome, _ in rows:
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
    entered_at = min(released_ts) if released_ts else None
    return {
        "entered_at": entered_at,
        "artefact_count": len(rows),
        "detail": {
            "releases": len(rows),
            "by_outcome": by_outcome,
            "first_released_at": entered_at,
            "last_released_at": max(released_ts) if released_ts else None,
        },
        # released → high confidence; only approvals/pending so far → low
        "confidence": "high" if released_ts else "low",
    }


def compute_stages(conn, fa: FeatureArtefacts) -> list[dict]:
    """Derive stage onsets. In epic mode (fa.anchor_ts set) every stage is bounded
    to >= the epic's creation ts, which removes the cross-epic / cross-year noise
    that a domain slug otherwise drags in."""
    stages: list[dict] = []
    anchor = fa.anchor_ts  # None in slug mode

    # planning — epic mode: the anchor epic itself; slug mode: earliest epic
    if fa.mode == "epic" and anchor:
        stages.append({"stage": "planning", "entered_at": anchor, "detection_source": "jira_epic",
                       "confidence": "high", "artefact_count": 1, "detail": {"epics": fa.epics}})
    else:
        plan_ts = _min_ts(conn, fa.epics, ["issue_created"])
        if plan_ts:
            stages.append({"stage": "planning", "entered_at": plan_ts, "detection_source": "jira_epic",
                           "confidence": "high", "artefact_count": len(fa.epics), "detail": {"epics": fa.epics}})
        else:
            plan_ts = _min_ts(conn, fa.jira, ["issue_created"])
            if plan_ts:
                stages.append({"stage": "planning", "entered_at": plan_ts, "detection_source": "jira_ticket",
                               "confidence": "medium", "artefact_count": len(fa.jira), "detail": {}})

    # trd — prefer the curated pages declared in projects.yaml; use page *creation*
    # (doc birth) as the onset, not later edits. Fall back to broad keyword pages
    # (low signal) only if none declared.
    trd_ts = _min_ts(conn, fa.declared_confluence, ["page_created"], after_ts=anchor) \
        or _min_ts(conn, fa.declared_confluence, ["page_updated"], after_ts=anchor)
    if trd_ts:
        stages.append({"stage": "trd", "entered_at": trd_ts, "detection_source": "confluence_declared",
                       "confidence": "high", "artefact_count": len(fa.declared_confluence), "detail": {}})
    else:
        trd_ts = _min_ts(conn, fa.confluence, ["page_created", "page_updated"], after_ts=anchor)
        if trd_ts:
            stages.append({"stage": "trd", "entered_at": trd_ts, "detection_source": "confluence_keyword",
                           "confidence": "low", "artefact_count": len(fa.confluence), "detail": {}})

    # code_dev — earliest github event among the exact PRs the feature's CMRs
    # shipped (precise PR-in-CMR link). Falls back to the broad keyword github
    # pool only when no shipped PRs are known.
    shipped = _shipped_pr_subjects(conn, fa.release_cmrs)
    if shipped:
        dev_ts = _min_ts(conn, shipped, after_ts=anchor)
        src, conf, n = "github_shipped_pr", "high", len(shipped)
    else:
        dev_ts = _min_ts(conn, fa.github, after_ts=anchor)
        src, conf, n = "github_keyword", "low", len(fa.github)
    if dev_ts:
        stages.append({"stage": "code_dev", "entered_at": dev_ts, "detection_source": src,
                       "confidence": conf, "artefact_count": n, "detail": {}})

    # rollout — CMR Released dates from feature_release (driven by release_cmrs:
    # the epic's own CMRs in epic mode, the slug's feature CMRs otherwise)
    ro = _rollout(conn, fa.release_cmrs, after_ts=anchor)
    if ro:
        stages.append({"stage": "rollout", "entered_at": ro["entered_at"], "detection_source": "cmr_release",
                       "confidence": ro["confidence"], "artefact_count": ro["artefact_count"],
                       "detail": ro["detail"]})

    stages.sort(key=lambda s: STAGE_ORDER.index(s["stage"]))
    return stages


def write_stages(conn, slug: str, scope: str, stages: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM feature_stage WHERE slug=? AND scope=?", (slug, scope))
    for s in stages:
        conn.execute(
            "INSERT INTO feature_stage "
            "(slug, scope, stage, entered_at, detection_source, confidence, artefact_count, detail_json, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (slug, scope, s["stage"], s["entered_at"], s["detection_source"], s["confidence"],
             s["artefact_count"], json.dumps(s.get("detail", {})), now),
        )
    conn.commit()


def _fmt_date(ts: str | None) -> str:
    return ts[:10] if ts else "—"


def print_timeline(fa: FeatureArtefacts, stages: list[dict]) -> None:
    if fa.mode == "epic":
        tag = f"epic:{fa.epic}, releases={fa.release_scope}({len(fa.release_cmrs)})"
    else:
        tag = "domain rollup"
    print(f"\n{fa.slug}  ({fa.name})  [{tag}]")
    present = {s["stage"]: s for s in stages}
    prev_ts = None
    for stage in STAGE_ORDER:
        s = present.get(stage)
        if not s:
            print(f"  {stage:<9} not detected")
            continue
        gap = ""
        if prev_ts and s["entered_at"]:
            try:
                d = (datetime.fromisoformat(s["entered_at"].replace("Z", "+00:00"))
                     - datetime.fromisoformat(prev_ts.replace("Z", "+00:00"))).days
                gap = f"  (+{d}d)"
            except ValueError:
                gap = ""
        extra = ""
        if stage == "rollout":
            bo = s["detail"].get("by_outcome", {})
            extra = "  " + ", ".join(f"{k}:{v}" for k, v in bo.items())
        print(f"  {stage:<9} {_fmt_date(s['entered_at'])}{gap}  [{s['detection_source']}, {s['confidence']}, n={s['artefact_count']}]{extra}")
        prev_ts = s["entered_at"] or prev_ts


def run_one(conn, token: str, write: bool = True) -> bool:
    fa = resolve(conn, token)
    if fa is None:
        print(f"no feature matched {token!r}")
        return False
    stages = compute_stages(conn, fa)
    if write:
        write_stages(conn, fa.slug, fa.epic or "", stages)
    print_timeline(fa, stages)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Compute feature lifecycle stages.")
    ap.add_argument("tokens", nargs="*", help="slugs / epic keys / names; omit for --all")
    ap.add_argument("--all", action="store_true", help="compute for every slug in projects.yaml")
    ap.add_argument("--dry-run", action="store_true", help="print timeline, do not write")
    args = ap.parse_args()

    conn = get_db()
    if args.all:
        tokens = [p.slug for p in load_projects()]
    else:
        tokens = args.tokens
    if not tokens:
        ap.error("pass at least one slug/epic/name, or --all")

    ok = 0
    for t in tokens:
        if run_one(conn, t, write=not args.dry_run):
            ok += 1
    print(f"\n{ok}/{len(tokens)} features processed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

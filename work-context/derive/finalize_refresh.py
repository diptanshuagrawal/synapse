"""
finalize_refresh.py — single-chat-phase orchestrator for label + enrichment.

Replaces the older two-phase loop (label_clusters → enrich_clusters) with one
combined dump + one combined apply. Chat reads ONE pending file with member
content AND enrichment context, writes ONE verdicts file covering label +
status + decisions + blockers + outcomes + followups + risk_areas + root_cause
+ stakeholders + artifacts + participant_roles.

Flow
----
    1. dump   — re-uses cluster_diff PLAN to find new + relabel cluster_ids,
                builds a combined dump per cluster with members + context.
                Writes state/pending_cluster_finalize.json + sibling rules.md.

    2. (chat) — chat reads rules + dump, writes
                state/verdicts.cluster_finalize.json with one combined entry
                per cluster.

    3. apply  — reads verdicts, UPDATEs topic_brief in place. Runs
                auto_recurring.stub() after to catch any Recurring labels
                that came in without explicit status. Refreshes
                state/last_topic_brief_validate.json so cron-status reflects.

CLI
---
    .venv/bin/python derive/finalize_refresh.py dump
        Default scope = clusters from cluster_diff_plan.json with action in
        {new, relabel}. Override with --cluster-ids 12 45 67.

    .venv/bin/python derive/finalize_refresh.py apply
        Reads verdicts file, updates topic_brief, runs auto_recurring,
        refreshes validate cache.

    .venv/bin/python derive/finalize_refresh.py status
        Show counts in pending + verdicts + topic_brief.

Rationale
---------
- Reduces chat work from 2 phases × 39 clusters = 78 reads to 1 phase × 39 = 39.
- Same fields persisted; same rules; just collapsed.
- Older label_clusters.py + enrich_clusters.py still work for ad-hoc per-tier
  passes. This is the recommended path post-refresh.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db, DB_PATH  # noqa: E402
from derive.subject_content import get_content  # noqa: E402
from derive.actor_behavior import _build_actor_canonical_map  # noqa: E402

ROOT = _PKG_ROOT
PENDING_PATH   = ROOT / "state" / "pending_cluster_finalize.json"
RULES_PATH     = ROOT / "state" / "pending_cluster_finalize.json.rules.md"
RULES_SOURCE   = ROOT / "derive" / "cluster_finalize_rules.md"
VERDICTS_PATH  = ROOT / "state" / "verdicts.cluster_finalize.json"
CLUSTER_DIFF_PLAN = ROOT / "state" / "cluster_diff_plan.json"
CLUSTER_DIFF_PLAN_APPLIED = ROOT / "state" / "cluster_diff_plan.json.applied"
TB_VALIDATE_CACHE = ROOT / "state" / "last_topic_brief_validate.json"

_VALID_STATUS = {"ACTIVE", "RESOLVED", "STALE", "RECURRING"}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── dump ────────────────────────────────────────────────────────────────────


def _resolve_cluster_ids(args) -> list[int]:
    """Pick cluster_ids to dump:
      1. --cluster-ids if provided
      2. Else from cluster_diff_plan.json (live) — new + relabel actions
      3. Else from cluster_diff_plan.json.applied — same
      4. Else error.
    """
    if args.cluster_ids:
        return [int(x) for x in args.cluster_ids]
    plan_path = CLUSTER_DIFF_PLAN if CLUSTER_DIFF_PLAN.exists() else CLUSTER_DIFF_PLAN_APPLIED
    if not plan_path.exists():
        return []
    plan = json.loads(plan_path.read_text())
    out: list[int] = []
    for new_cid_str, entry in plan.get("plan", {}).items():
        if entry.get("action") in ("new", "relabel"):
            out.append(int(new_cid_str))
    return sorted(out)


def _cluster_payload(conn, cid: int, actor_map: dict[str, str], member_chars: int = 600) -> dict:
    """Build per-cluster dump payload: label-relevant samples + enrichment context."""
    row = conn.execute(
        "SELECT label, summary, source_breakdown_json, member_count, "
        "       first_ts, last_activity_ts, status "
        "  FROM topic_brief WHERE cluster_id = ?",
        (cid,),
    ).fetchone()
    if not row:
        return {}
    existing_label, existing_summary, src_json, member_count, first_ts, last_ts, existing_status = row
    members = [r[0] for r in conn.execute(
        "SELECT subject FROM topic_brief_member WHERE cluster_id = ? ORDER BY subject", (cid,)
    ).fetchall()]

    cluster_actor_counts: dict[str, int] = defaultdict(int)
    member_blocks = []
    for subj in members:
        actor_rows = conn.execute(
            "SELECT actor, COUNT(*) FROM events WHERE subject = ? AND actor IS NOT NULL "
            "GROUP BY actor", (subj,)
        ).fetchall()
        for a, n in actor_rows:
            cluster_actor_counts[a] += n
        _, content = get_content(conn, subj)
        cont = " ".join((content or "").split())
        if len(cont) > member_chars:
            cont = cont[: member_chars].rstrip() + "…"
        # Pull title separately for hint context (PR title, page title, etc).
        title_row = conn.execute(
            "SELECT title FROM events WHERE subject = ? AND title IS NOT NULL AND title != '' "
            "ORDER BY ts LIMIT 1", (subj,)
        ).fetchone()
        title = (title_row[0] or "")[:160] if title_row else ""
        member_blocks.append({"subject": subj, "title": title, "content": cont})

    participants_observed: dict[str, int] = defaultdict(int)
    for actor_id, count in cluster_actor_counts.items():
        if "[bot]" in (actor_id or "").lower():
            continue
        canon = actor_map.get(actor_id) or f"<raw:{actor_id}>"
        participants_observed[canon] += count

    return {
        "cluster_id": cid,
        "existing_label": existing_label,
        "existing_summary": existing_summary,
        "existing_status": existing_status,
        "source_breakdown": json.loads(src_json) if src_json else {},
        "member_count": member_count,
        "first_ts": first_ts,
        "last_activity_ts": last_ts,
        "participants_observed": dict(sorted(participants_observed.items(), key=lambda kv: -kv[1])),
        "members": member_blocks,
    }


def cmd_dump(args):
    conn = get_db()
    cluster_ids = _resolve_cluster_ids(args)
    if not cluster_ids:
        print("no clusters resolved — pass --cluster-ids OR ensure cluster_diff_plan exists "
              "with new/relabel entries", file=sys.stderr)
        return 1

    actor_map = _build_actor_canonical_map()
    payloads = []
    for cid in cluster_ids:
        p = _cluster_payload(conn, cid, actor_map, args.member_chars)
        if p:
            payloads.append(p)

    out = {
        "computed_at": _now_iso(),
        "n_clusters": len(payloads),
        "clusters": payloads,
    }
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(out, indent=2))
    if RULES_SOURCE.exists():
        RULES_PATH.write_text(RULES_SOURCE.read_text())

    print(f"✓ finalize-dump complete")
    print(f"  pending file: {PENDING_PATH}")
    print(f"  rules file:   {RULES_PATH}")
    print(f"  clusters:     {len(payloads)}")
    print(f"  total members: {sum(c['member_count'] or 0 for c in payloads)}")
    print()
    print(f"Next: in chat, read the rules FIRST, then the dump, then write verdicts to:")
    print(f"  {VERDICTS_PATH}")
    print(f"Then run: .venv/bin/python derive/finalize_refresh.py apply")
    return 0


# ── apply ───────────────────────────────────────────────────────────────────


def cmd_apply(args):
    if not VERDICTS_PATH.exists():
        print(f"missing {VERDICTS_PATH} — chat must write verdicts first", file=sys.stderr)
        return 1
    if not PENDING_PATH.exists():
        print(f"missing {PENDING_PATH} — run dump first", file=sys.stderr)
        return 1
    verdicts = json.loads(VERDICTS_PATH.read_text())
    if not isinstance(verdicts, list):
        print("verdicts file must be a JSON array", file=sys.stderr)
        return 1
    pending = json.loads(PENDING_PATH.read_text())
    pending_by_id = {c["cluster_id"]: c for c in pending["clusters"]}

    conn = get_db()
    now = _now_iso()
    applied = 0
    skipped = 0
    errors: list[str] = []

    for v in verdicts:
        cid = v.get("cluster_id")
        if cid is None or cid not in pending_by_id:
            errors.append(f"cluster_id {cid!r}: not in pending dump")
            skipped += 1
            continue
        ctx = pending_by_id[cid]
        label = (v.get("label") or "").strip()
        if not label:
            errors.append(f"cluster_id {cid}: empty label")
            skipped += 1
            continue
        what_work = (v.get("what_work") or "").strip()
        confidence = float(v.get("confidence") or 0.0)
        status = (v.get("status") or "").strip().upper()
        if status and status not in _VALID_STATUS:
            errors.append(f"cluster_id {cid}: invalid status {status!r}")
            skipped += 1
            continue
        decisions = v.get("decisions") or []
        blockers = v.get("blockers") or []
        outcomes = v.get("outcomes") or []
        followups = v.get("followups") or []
        risk_areas = v.get("risk_areas") or []
        root_cause = v.get("root_cause")
        stakeholders = v.get("stakeholders") or []
        artifacts = v.get("artifacts") or []
        roles = v.get("participant_roles") or {}

        # Build participants_json from script-computed counts + chat-assigned roles.
        participants_payload = []
        for person, count in ctx.get("participants_observed", {}).items():
            entry = {"person": person, "contribution_count": count}
            if person in roles:
                entry["role"] = roles[person]
            participants_payload.append(entry)
        # Stable sort: counts desc, then name.
        participants_payload.sort(key=lambda x: (-x["contribution_count"], x["person"]))

        try:
            conn.execute(
                "UPDATE topic_brief SET "
                "  label = ?, summary = ?, confidence = ?, status = ?, "
                "  decisions_json = ?, blockers_json = ?, root_cause = ?, "
                "  outcomes_json = ?, followups_json = ?, risk_areas_json = ?, "
                "  stakeholders_json = ?, artifacts_json = ?, "
                "  participants_json = ?, computed_at = ? "
                "WHERE cluster_id = ?",
                (
                    label, what_work, confidence, status or None,
                    json.dumps(decisions, sort_keys=True),
                    json.dumps(blockers, sort_keys=True),
                    root_cause,
                    json.dumps(outcomes, sort_keys=True),
                    json.dumps(followups, sort_keys=True),
                    json.dumps(risk_areas, sort_keys=True),
                    json.dumps(stakeholders, sort_keys=True),
                    json.dumps(artifacts, sort_keys=True),
                    json.dumps(participants_payload, sort_keys=True),
                    now,
                    cid,
                ),
            )
            applied += 1
        except Exception as e:
            errors.append(f"cluster_id {cid}: {type(e).__name__}: {e}")
            skipped += 1
    conn.commit()

    # Auto-stub any Recurring-labelled clusters that came in without explicit
    # status (chat may omit fields for Recurring patterns).
    from derive.auto_recurring import stub_label_prefix as _auto_stub
    auto = _auto_stub(conn, dry_run=False)

    # Auto-link clusters to projects.yaml slugs after every finalize apply.
    # Deterministic (jira_epic / confluence_page / domain / keyword rules).
    # Idempotent — re-runs produce same map. Unmapped clusters surface
    # via `link_clusters_to_projects.py unmapped` for owner triage.
    link_summary: dict = {}
    try:
        from derive.link_clusters_to_projects import (
            _load_index as _link_idx,
            compute_plan as _link_plan,
            apply_plan as _link_apply,
        )
        _plan = _link_plan(conn, _link_idx())
        _result = _link_apply(conn, _plan)
        link_summary = {
            "clusters_linked": _plan["clusters_linked"],
            "clusters_unmapped": _plan["clusters_unmapped"],
            "links_total": _plan["links_total"],
            "rows_inserted": _result["rows_inserted"],
        }
    except Exception as e:
        link_summary = {"error": f"{type(e).__name__}: {e}"}

    # Re-derive per-cluster owner_distribution_json. Cluster membership just
    # changed (new/relabelled clusters), so the home_team_owned_pct that
    # /ask + /retro read would otherwise go stale until the next /rollup.
    # Idempotent; reads corrected subject-level ownership from subject_summary.
    own_summary: dict = {}
    try:
        from derive.cluster_ownership_rollup import apply as _own_apply
        own_summary = _own_apply(conn)
    except Exception as e:
        own_summary = {"error": f"{type(e).__name__}: {e}"}

    summary = {
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "auto_recurring_stubbed": auto.get("stubbed", 0),
        "cluster_project_link": link_summary,
        "cluster_ownership": own_summary,
        "topic_brief_rows": conn.execute("SELECT COUNT(*) FROM topic_brief").fetchone()[0],
    }
    print(json.dumps(summary, indent=2))

    # Refresh validate cache so cron-status reflects the new state immediately.
    try:
        from derive.topic_brief_validate import compute as _tb_compute
        report = _tb_compute(conn)
        TB_VALIDATE_CACHE.write_text(json.dumps(report, indent=2, default=str))
        print(f"\n✓ topic_brief_validate cache refreshed → {TB_VALIDATE_CACHE.name}")
        for sev, check, msg in report.get("findings", []):
            print(f"    {sev:4s}  {check:22s}  {msg[:80]}")
    except Exception as e:
        print(f"\n⚠ validate refresh failed: {type(e).__name__}: {e}", file=sys.stderr)

    return 0 if not errors else 2


# ── status ──────────────────────────────────────────────────────────────────


def cmd_status(args):
    info: dict = {
        "pending":   PENDING_PATH.exists(),
        "rules":     RULES_PATH.exists(),
        "verdicts":  VERDICTS_PATH.exists(),
    }
    if PENDING_PATH.exists():
        try:
            info["pending_clusters"] = len(json.loads(PENDING_PATH.read_text())["clusters"])
        except Exception as e:
            info["pending_clusters_error"] = str(e)
    if VERDICTS_PATH.exists():
        try:
            info["verdicts_count"] = len(json.loads(VERDICTS_PATH.read_text()))
        except Exception as e:
            info["verdicts_count_error"] = str(e)
    conn = get_db()
    info["topic_brief_rows"] = conn.execute("SELECT COUNT(*) FROM topic_brief").fetchone()[0]
    info["topic_brief_with_label"] = conn.execute(
        "SELECT COUNT(*) FROM topic_brief WHERE label IS NOT NULL"
    ).fetchone()[0]
    info["topic_brief_with_status"] = conn.execute(
        "SELECT COUNT(*) FROM topic_brief WHERE status IS NOT NULL"
    ).fetchone()[0]
    print(json.dumps(info, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="Combined label + enrichment dump for chat")
    d.add_argument("--cluster-ids", nargs="*",
                   help="Override which clusters to dump. Default = new+relabel from cluster_diff_plan.")
    d.add_argument("--member-chars", type=int, default=600,
                   help="Cap chars per member content snippet")
    d.set_defaults(fn=cmd_dump)

    a = sub.add_parser("apply", help="Read combined verdicts, persist to topic_brief")
    a.set_defaults(fn=cmd_apply)

    s = sub.add_parser("status", help="Show pending + verdicts + topic_brief counts")
    s.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

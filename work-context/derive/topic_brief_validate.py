#!/usr/bin/env python3
"""
topic_brief_validate.py — pipeline-side validation for `topic_brief`.

Catches the silent gap that opens after every `cluster_diff apply`:
new + relabel clusters land with NULL label / NULL status / NULL
enrichment fields, and the owner forgets to run the label → enrich loop.

v1 checks:
  1. label_coverage       — rows with NULL label
  2. status_coverage      — rows with NULL status
  3. enrichment_coverage  — non-RECURRING rows with NULL decisions_json
  4. participants_coverage — rows with NULL participants_json

v2 checks (added with finalize_refresh):
  5. outcomes_coverage    — non-RECURRING ACTIVE/RESOLVED rows missing outcomes_json
  6. v2_field_presence    — ACTIVE rows with ALL v2 fields NULL → never enriched at v2

Severity policy:
  - FAIL : any NULL label OR NULL status
  - WARN : NULL decisions_json on a non-RECURRING row
  - WARN : NULL participants_json
  - PASS : zero NULLs across all checks

Output schema mirrors the per-source validate contract so cron-status
can render it under a PIPELINE block.

CLI
---
    .venv/bin/python derive/topic_brief_validate.py            # human-readable
    .venv/bin/python derive/topic_brief_validate.py --json     # for cron-status
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH  # noqa: E402

RED, YEL, GRN, DIM, RST = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[0m"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def compute(conn: sqlite3.Connection) -> dict:
    n_total = conn.execute("SELECT COUNT(*) FROM topic_brief").fetchone()[0]
    if n_total == 0:
        return {
            "computed_at": _now_iso(),
            "source": "topic_brief",
            "n_total": 0,
            "findings": [["WARN", "empty", "topic_brief is empty — run derive/cluster_diff.py plan + apply"]],
        }

    n_null_label = conn.execute(
        "SELECT COUNT(*) FROM topic_brief WHERE label IS NULL"
    ).fetchone()[0]
    n_null_status = conn.execute(
        "SELECT COUNT(*) FROM topic_brief WHERE status IS NULL"
    ).fetchone()[0]
    n_null_decisions = conn.execute(
        "SELECT COUNT(*) FROM topic_brief "
        "WHERE decisions_json IS NULL AND (status IS NULL OR status != 'RECURRING')"
    ).fetchone()[0]
    n_null_participants = conn.execute(
        "SELECT COUNT(*) FROM topic_brief WHERE participants_json IS NULL"
    ).fetchone()[0]
    # v2 fields: check NULL across all 5. We only WARN, not FAIL — these are
    # optional and old rows pre-date the v2 migration.
    n_null_outcomes_v2 = conn.execute(
        "SELECT COUNT(*) FROM topic_brief "
        "WHERE outcomes_json IS NULL AND status IN ('ACTIVE','RESOLVED')"
    ).fetchone()[0]
    # v2-never-touched: ACTIVE rows where ALL 5 new columns are NULL.
    n_v2_never_touched = conn.execute(
        "SELECT COUNT(*) FROM topic_brief "
        "WHERE status = 'ACTIVE' "
        "  AND outcomes_json     IS NULL "
        "  AND followups_json    IS NULL "
        "  AND risk_areas_json   IS NULL "
        "  AND stakeholders_json IS NULL "
        "  AND artifacts_json    IS NULL"
    ).fetchone()[0]

    # Sample offending cluster_ids for actionability.
    null_label_sample = [r[0] for r in conn.execute(
        "SELECT cluster_id FROM topic_brief WHERE label IS NULL ORDER BY cluster_id LIMIT 8"
    ).fetchall()]
    null_status_sample = [r[0] for r in conn.execute(
        "SELECT cluster_id FROM topic_brief WHERE status IS NULL ORDER BY cluster_id LIMIT 8"
    ).fetchall()]
    null_enrich_sample = [r[0] for r in conn.execute(
        "SELECT cluster_id FROM topic_brief "
        "WHERE decisions_json IS NULL AND (status IS NULL OR status != 'RECURRING') "
        "ORDER BY cluster_id LIMIT 8"
    ).fetchall()]

    findings: list[list[str]] = []
    if n_null_label > 0:
        findings.append([
            "FAIL", "label_coverage",
            f"{n_null_label} cluster(s) with NULL label — run label flow on cids: {null_label_sample}",
        ])
    if n_null_status > 0:
        findings.append([
            "FAIL", "status_coverage",
            f"{n_null_status} cluster(s) with NULL status — run enrich_clusters apply (cids: {null_status_sample})",
        ])
    if n_null_decisions > 0:
        findings.append([
            "WARN", "enrichment_coverage",
            f"{n_null_decisions} non-RECURRING cluster(s) with NULL decisions_json (cids: {null_enrich_sample})",
        ])
    if n_null_participants > 0:
        findings.append([
            "WARN", "participants_coverage",
            f"{n_null_participants} cluster(s) with NULL participants_json — run participants migration",
        ])
    if n_v2_never_touched > 0:
        findings.append([
            "WARN", "v2_field_presence",
            f"{n_v2_never_touched} ACTIVE cluster(s) have NO v2 enrichment fields populated — "
            f"re-run finalize_refresh for richer queries",
        ])
    if not findings:
        findings.append(["PASS", "all_fields", f"all {n_total} cluster(s) fully populated"])

    # Status distribution (handy for the cron-status renderer).
    status_dist: dict[str, int] = {}
    for status, cnt in conn.execute(
        "SELECT COALESCE(status, '<NULL>'), COUNT(*) FROM topic_brief GROUP BY status"
    ).fetchall():
        status_dist[status] = cnt

    return {
        "computed_at": _now_iso(),
        "source": "topic_brief",
        "n_total": n_total,
        "n_null_label": n_null_label,
        "n_null_status": n_null_status,
        "n_null_decisions_non_recurring": n_null_decisions,
        "n_null_participants": n_null_participants,
        "n_null_outcomes_active_resolved": n_null_outcomes_v2,
        "n_v2_never_touched_active": n_v2_never_touched,
        "status_distribution": status_dist,
        "findings": findings,
    }


def _render_human(report: dict) -> None:
    print(f"\n=== topic_brief_validate · {report['computed_at']} ===")
    print(f"  total clusters:          {report['n_total']}")
    print(f"  null label:              {report.get('n_null_label', 0)}")
    print(f"  null status:             {report.get('n_null_status', 0)}")
    print(f"  null decisions (non-RECURRING): {report.get('n_null_decisions_non_recurring', 0)}")
    print(f"  null participants:       {report.get('n_null_participants', 0)}")
    sd = report.get("status_distribution", {})
    if sd:
        print(f"\n  status:")
        for k, v in sorted(sd.items(), key=lambda x: -x[1]):
            print(f"    {k:15s} {v}")
    print()
    for sev, check, msg in report.get("findings", []):
        col = GRN if sev == "PASS" else (YEL if sev == "WARN" else RED)
        print(f"  {col}{sev:4s}{RST}  {check:22s}  {msg}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    if not DB_PATH.exists():
        print(f"db missing: {DB_PATH}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(DB_PATH)
    report = compute(conn)
    conn.close()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render_human(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

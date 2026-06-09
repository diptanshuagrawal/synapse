#!/usr/bin/env python3
"""
jira_validate.py — sanity-check Jira-ingested data in events.db.

v1 checks (will grow iteratively):
  1. attribution    — actors not in any people.yaml entry (scope team/org/external)
  2. status_capture — to_status NULL rate on issue_created + status_change
                       events ingested in last 14d. Catches regressions where
                       jira.py stops populating the new-status field (code
                       revert, API change, migration rollback).

Note on jira actor shape
------------------------
Jira ingest writes `actor` as the user's email (e.g. "alice@example.com")
NOT the jira account-id. So people.yaml needs `email:` set for canonicalisation
to work. Entries lacking email show raw and pollute the unmapped bucket —
add `email:` to the people.yaml entry (set `scope:` appropriately) so they
resolve. The identity self-heal pipeline back-fills email from observed
signals automatically over time (see derive/identity_reconcile.py).

Output schema mirrors github_validate.py — same {computed_at, source,
n_total_events, n_actors_mapped, ...} contract for cron-status.

Severity policy
---------------
- FAIL : raw_unknown actor with > 100 jira events
- WARN : any raw_unknown
- PASS : zero raw_unknown

CLI
---
    .venv/bin/python derive/jira_validate.py            # human-readable
    .venv/bin/python derive/jira_validate.py --json     # for cron-status
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
from derive.actor_behavior import _build_actor_scope_map  # noqa: E402

SOURCE = "jira"

UNKNOWN_FAIL_COUNT = 100  # jira chatty per ticket; raise threshold vs github

RED, YEL, GRN, DIM, RST = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[0m"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def compute(conn: sqlite3.Connection) -> dict:
    scope_map = _build_actor_scope_map()

    n_total = conn.execute(
        "SELECT COUNT(*) FROM events WHERE source='jira'"
    ).fetchone()[0]

    rows = conn.execute(
        "SELECT actor, COUNT(*) FROM events WHERE source='jira' GROUP BY actor"
    ).fetchall()

    n_mapped = 0
    raw_known: list[tuple[str, int]] = []
    raw_unknown: list[tuple[str, int]] = []
    for actor, n in rows:
        if not actor:
            continue
        scope = scope_map.get(actor)
        if scope == "team":
            n_mapped += n
        elif scope in ("org", "external"):
            raw_known.append((actor, n))
        else:
            raw_unknown.append((actor, n))

    raw_known.sort(key=lambda x: -x[1])
    raw_unknown.sort(key=lambda x: -x[1])

    findings: list[list[str]] = []
    if not raw_unknown:
        findings.append(["PASS", "attribution", "all actors scoped (team/org/external)"])
    else:
        biggest = raw_unknown[0]
        sev = "FAIL" if biggest[1] > UNKNOWN_FAIL_COUNT else "WARN"
        sample = ", ".join(f"{a}({n})" for a, n in raw_unknown[:3])
        findings.append([
            sev, "attribution",
            f"{len(raw_unknown)} unmapped actor(s); top: {sample}",
        ])

    # status_capture check — flag if recent issue_created / status_change
    # rows are missing to_status. Catches regressions in jira.py ingest.
    # Trailing 14-day window keeps the check sensitive without false-positives
    # from one-day cursor blips.
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=14)).isoformat(timespec="seconds").replace("+00:00", "Z")
    recent_total = conn.execute(
        "SELECT COUNT(*) FROM events WHERE source='jira' "
        "  AND event_type IN ('issue_created','status_change') "
        "  AND ts >= ?",
        (cutoff,),
    ).fetchone()[0]
    recent_null = conn.execute(
        "SELECT COUNT(*) FROM events WHERE source='jira' "
        "  AND event_type IN ('issue_created','status_change') "
        "  AND ts >= ? AND to_status IS NULL",
        (cutoff,),
    ).fetchone()[0]
    if recent_total == 0:
        findings.append(["WARN", "status_capture",
                         "no jira issue_created / status_change events in last 14d — check ingest"])
    else:
        null_pct = 100.0 * recent_null / recent_total
        if null_pct > 5:
            findings.append([
                "FAIL", "status_capture",
                f"{recent_null}/{recent_total} ({null_pct:.0f}%) recent events have NULL to_status — "
                f"jira.py may have regressed; run derive/jira_backfill_status.py to backfill + check ingest"
            ])
        elif null_pct > 1:
            findings.append([
                "WARN", "status_capture",
                f"{recent_null}/{recent_total} ({null_pct:.1f}%) recent events have NULL to_status — minor drift, monitor"
            ])
        else:
            findings.append([
                "PASS", "status_capture",
                f"{recent_total} recent events tracked, to_status populated"
            ])

    return {
        "computed_at": _now_iso(),
        "source": SOURCE,
        "n_total_events": n_total,
        "n_actors_mapped": n_mapped,
        "n_actors_raw_known": sum(n for _, n in raw_known),
        "n_actors_raw_unknown": sum(n for _, n in raw_unknown),
        "raw_unknown_top": [[a, n] for a, n in raw_unknown[:10]],
        "findings": findings,
    }


def _render_human(report: dict) -> None:
    print(f"\n=== jira_validate · {report['computed_at']} ===")
    print(f"  total events:      {report['n_total_events']:,}")
    print(f"  mapped:            {GRN}{report['n_actors_mapped']:,}{RST}")
    print(f"  raw (known ext):   {DIM}{report['n_actors_raw_known']:,}{RST}")
    print(f"  raw (UNKNOWN):     {YEL}{report['n_actors_raw_unknown']:,}{RST}")
    if report["raw_unknown_top"]:
        print(f"\n  top unmapped:")
        for a, n in report["raw_unknown_top"]:
            print(f"    {a:50s} {n}")
    print()
    for sev, check, msg in report["findings"]:
        col = GRN if sev == "PASS" else (YEL if sev == "WARN" else RED)
        print(f"  {col}{sev:4s}{RST}  {check:14s}  {msg}")
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

    try:
        conn = sqlite3.connect(DB_PATH)
        report = compute(conn)
        conn.close()
    except Exception as e:
        print(f"validate failed: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render_human(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

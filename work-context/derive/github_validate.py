#!/usr/bin/env python3
"""
github_validate.py — sanity-check Github-ingested data in events.db.

v1 checks (will grow iteratively):
  1. attribution — actors not in any people.yaml entry (scope team/slice/external)

Output
------
JSON to stdout (stable schema; cron-status reads it):
    {"computed_at": ISO,
     "source": "github",
     "n_total_events": N,
     "n_commit_events": N,
     "n_actors_mapped": N,
     "n_actors_raw_known": N,         # matched people.yaml scope=slice|external
     "n_actors_raw_unknown": N,       # NEW — needs ack or people.yaml entry
     "raw_unknown_top": [[actor, count], ...],
     "findings": [["PASS|WARN|FAIL", "check_name", "msg"], ...]}

Severity policy
---------------
- FAIL : any raw_unknown actor with > 50 commits (likely an active team member
         being missed)
- WARN : any raw_unknown actor at all
- PASS : zero raw_unknown actors

Exit codes
----------
    0   any findings (still surfaces — fail-soft for ingest runner)
    2   env error (db missing / yaml unreadable)

CLI
---
    .venv/bin/python derive/github_validate.py            # human-readable
    .venv/bin/python derive/github_validate.py --json     # for cron-status
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

SOURCE = "github"

# severity thresholds
UNKNOWN_FAIL_COUNT = 50

# ANSI
RED, YEL, GRN, DIM, RST = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[0m"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def compute(conn: sqlite3.Connection) -> dict:
    scope_map = _build_actor_scope_map()

    n_total = conn.execute(
        "SELECT COUNT(*) FROM events WHERE source='github'"
    ).fetchone()[0]

    # We only care about commit_in_pr for attribution — comments/reviews come
    # from bots a lot and skew the gap signal.
    rows = conn.execute(
        "SELECT actor, COUNT(*) FROM events "
        "WHERE source='github' AND event_type='commit_in_pr' "
        "GROUP BY actor"
    ).fetchall()

    n_commit = sum(n for _, n in rows)
    n_mapped = 0
    raw_known: list[tuple[str, int]] = []
    raw_unknown: list[tuple[str, int]] = []
    for actor, n in rows:
        if not actor:
            continue
        if "[bot]" in actor.lower():
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
        findings.append(["PASS", "attribution", "all non-bot actors scoped (team/slice/external)"])
    else:
        biggest = raw_unknown[0]
        worst_sev = "FAIL" if biggest[1] > UNKNOWN_FAIL_COUNT else "WARN"
        sample = ", ".join(f"{a}({n})" for a, n in raw_unknown[:3])
        findings.append([
            worst_sev,
            "attribution",
            f"{len(raw_unknown)} unmapped actor(s); top: {sample}",
        ])

    return {
        "computed_at": _now_iso(),
        "source": SOURCE,
        "n_total_events": n_total,
        "n_commit_events": n_commit,
        "n_actors_mapped": n_mapped,
        "n_actors_raw_known": sum(n for _, n in raw_known),
        "n_actors_raw_unknown": sum(n for _, n in raw_unknown),
        "raw_unknown_top": [[a, n] for a, n in raw_unknown[:10]],
        "findings": findings,
    }


def _render_human(report: dict) -> None:
    print(f"\n=== github_validate · {report['computed_at']} ===")
    print(f"  total events:      {report['n_total_events']:,}")
    print(f"  commit events:     {report['n_commit_events']:,}")
    print(f"  mapped:            {GRN}{report['n_actors_mapped']:,}{RST}")
    print(f"  raw (known ext):   {DIM}{report['n_actors_raw_known']:,}{RST}")
    print(f"  raw (UNKNOWN):     {YEL}{report['n_actors_raw_unknown']:,}{RST}")
    if report["raw_unknown_top"]:
        print(f"\n  top unmapped:")
        for a, n in report["raw_unknown_top"]:
            print(f"    {a:40s} {n}")
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

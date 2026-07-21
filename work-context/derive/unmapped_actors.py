#!/usr/bin/env python3
"""unmapped_actors.py — list EVERY ingest actor not scoped in people.yaml.

The {github,jira,confluence}_validate.py validators only surface the top-10
unmapped actors (enough for a dashboard glance). The ingest-autofix routine
needs the COMPLETE per-source list so it can resolve each identity (Atlassian
lookup) and add a people.yaml mapping.

Detection mirrors each validator EXACTLY — same actor selection, same bot
handling — so "unmapped here" == "the WARN the validator raised":
  - github     : commit_in_pr actors only, actors containing "[bot]" skipped
  - jira       : all actors
  - confluence : all actors
An actor is "unmapped" when _build_actor_scope_map() returns no scope for it
(i.e. it is absent from people.yaml under every identity key).

Output (--json, the routine reads this):
    {"computed_at": ISO,
     "n_unmapped_total": N,
     "by_source": {
        "github":     [{"actor": str, "count": int, "samples": [subject, ...],
                        "signals": {key_type: [{"value": v, "n_obs": n}, ...]}}, ...],
        "jira":       [...],
        "confluence": [...]}}

The samples (up to 3 distinct subjects) give the resolver context — which repo /
page / ticket the actor touched — to disambiguate look-ups.

`signals` carries the identity pairs OBSERVED for this actor at ingest time
(from events.db::identity_signals) — e.g. a github login's commit-author email
+ git name, or an accountId's linked email. This is GROUND TRUTH captured from
the payloads, not a name guess: it lets the resolver map a login/accountId to a
real person deterministically (login → corp email → people.yaml) instead of a
fuzzy name search that returns several candidates. Empty {} means no pair was
ever observed (then the resolver must fall back to a name look-up).

CLI
---
    .venv/bin/python derive/unmapped_actors.py            # human-readable
    .venv/bin/python derive/unmapped_actors.py --json     # for the routine

Exit codes
----------
    0   ran (whether or not anything is unmapped)
    2   env error (db missing)
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

# (sql actor query, skip_bot) per source — mirrors the validators verbatim.
SOURCE_QUERIES: dict[str, tuple[str, bool]] = {
    "github": (
        "SELECT actor, COUNT(*) FROM events "
        "WHERE source='github' AND event_type='commit_in_pr' GROUP BY actor",
        True,
    ),
    "jira": (
        "SELECT actor, COUNT(*) FROM events WHERE source='jira' GROUP BY actor",
        False,
    ),
    "confluence": (
        "SELECT actor, COUNT(*) FROM events WHERE source='confluence' GROUP BY actor",
        False,
    ),
}

SAMPLE_LIMIT = 3

# ANSI
YEL, GRN, DIM, RST = "\033[33m", "\033[32m", "\033[2m", "\033[0m"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _samples(conn: sqlite3.Connection, source: str, actor: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT subject FROM events "
        "WHERE source=? AND actor=? AND subject IS NOT NULL "
        "ORDER BY ts DESC LIMIT ?",
        (source, actor, SAMPLE_LIMIT),
    ).fetchall()
    return [r[0] for r in rows]


def _signals(conn: sqlite3.Connection, actor: str) -> dict[str, list[dict]]:
    """Identity pairs observed for `actor` in events.db::identity_signals.

    Pairs are stored with a canonical (type, value) ordering, so the actor can
    appear on either side of a row — we match its value against both and keep
    the OTHER side of each row. Returns {key_type: [{"value", "n_obs"}, ...]}
    with each type's values sorted by observation count (highest confidence
    first). Ground truth from ingest payloads — e.g. a github login's
    commit-author email — so the resolver maps login/accountId → person without
    a fuzzy name guess. Missing table / any error degrades to {} (never fatal).
    """
    al = actor.lower()
    try:
        # Case-insensitive match on both sides: signal values are stored with
        # source case preserved for non-email types, so compare lowercased.
        # identity_signals is small (~hundreds of rows) — the lost index use is
        # negligible and case-insensitivity is worth more than the micro-scan.
        rows = conn.execute(
            "SELECT key_a_type, key_a_value, key_b_type, key_b_value, n_obs "
            "FROM identity_signals "
            "WHERE LOWER(key_a_value)=? OR LOWER(key_b_value)=?",
            (al, al),
        ).fetchall()
    except sqlite3.Error:
        return {}
    acc: dict[str, dict[str, int]] = {}
    for at, av, bt, bv, n in rows:
        for typ, val in ((at, av), (bt, bv)):
            if val is None or val.lower() == al:
                continue  # the actor's own identity — skip; keep the linked side
            acc.setdefault(typ, {})
            acc[typ][val] = max(acc[typ].get(val, 0), n)
    return {
        typ: [{"value": v, "n_obs": nn}
              for v, nn in sorted(vals.items(), key=lambda kv: -kv[1])]
        for typ, vals in sorted(acc.items())
    }


def compute(conn: sqlite3.Connection) -> dict:
    scope_map = _build_actor_scope_map()
    by_source: dict[str, list[dict]] = {}
    total = 0
    for source, (query, skip_bot) in SOURCE_QUERIES.items():
        unmapped: list[dict] = []
        for actor, n in conn.execute(query).fetchall():
            if not actor:
                continue
            if skip_bot and "[bot]" in actor.lower():
                continue
            if scope_map.get(actor) is not None:
                continue
            unmapped.append(
                {
                    "actor": actor,
                    "count": n,
                    "samples": _samples(conn, source, actor),
                    "signals": _signals(conn, actor),
                }
            )
        unmapped.sort(key=lambda x: -x["count"])
        by_source[source] = unmapped
        total += len(unmapped)
    return {
        "computed_at": _now_iso(),
        "n_unmapped_total": total,
        "by_source": by_source,
    }


def _render_human(report: dict) -> None:
    print(f"\n=== unmapped_actors · {report['computed_at']} ===")
    total = report["n_unmapped_total"]
    if total == 0:
        print(f"  {GRN}✓ all ingest actors scoped (team/org/external){RST}\n")
        return
    print(f"  {YEL}{total} unmapped actor(s){RST} across sources\n")
    for source, rows in report["by_source"].items():
        if not rows:
            continue
        print(f"  {source} ({len(rows)}):")
        for r in rows:
            samp = DIM + (", ".join(r["samples"]) or "—") + RST
            print(f"    {YEL}{r['actor']:42s}{RST} {r['count']:>4}  {samp}")
            for typ, vals in (r.get("signals") or {}).items():
                shown = ", ".join(f"{v['value']}({v['n_obs']})" for v in vals[:3])
                print(f"      {DIM}↳ {typ}: {shown}{RST}")
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
    raise SystemExit(main())

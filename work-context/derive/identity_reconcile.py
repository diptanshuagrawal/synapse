#!/usr/bin/env python3
"""Reconcile observed `identity_signals` into `config/people.yaml`.

Algorithm
---------
1. Load people.yaml entries.
2. For each entry, collect its currently-known {type → values} map.
3. Walk identity_signals: every signal where any known value of the entry
   appears as either side → the OTHER side becomes a candidate fill for
   the same entry.
4. For each candidate, fill the entry's missing field. If the field is a
   list (`git_names`, `github_aliases`), append. If multiple candidates
   compete for one scalar slot, the highest `n_obs` wins.
5. Optionally create new `scope: slice` entries for signals whose values
   match no existing entry (only above a confidence threshold; gated by
   --create-orphans).
6. Write back people.yaml on diff. Idempotent.

CLI
---
    .venv/bin/python derive/identity_reconcile.py            # apply
    .venv/bin/python derive/identity_reconcile.py --dry-run  # report only
    .venv/bin/python derive/identity_reconcile.py --create-orphans
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from derive.identity_signals import init as init_signals  # noqa: E402

PEOPLE = REPO / "config" / "people.yaml"
DB = REPO / "index" / "events.db"

# people.yaml field ↔ signal key_type (1-to-1, both directions).
TYPE_TO_FIELD = {
    "email":         "email",
    "jira_id":       "jira_id",
    "slack_id":      "slack_id",
    "slack_handle":  "slack_handle",
    "github":        "github",
    "name":          "name",
    "git_name":      "git_names",
}
LIST_FIELDS = {"git_names", "github_aliases"}
# Reverse map for orphan-creation
FIELD_TO_TYPE = {v: k for k, v in TYPE_TO_FIELD.items()}

MIN_CONFIDENCE_FILL = 1     # any sighting fills a missing field
MIN_CONFIDENCE_ORPHAN = 2   # >=2 sightings before auto-creating new entry


def load_people() -> dict:
    return yaml.safe_load(PEOPLE.read_text()) or {"people": []}


def collect_entry_values(entry: dict) -> dict[str, set[str]]:
    """Return {signal_type → set(values)} for an entry's known fields."""
    out: dict[str, set[str]] = defaultdict(set)
    for typ, field in TYPE_TO_FIELD.items():
        v = entry.get(field)
        if v is None:
            continue
        if isinstance(v, list):
            for item in v:
                if item:
                    out[typ].add(str(item).strip())
        else:
            out[typ].add(str(v).strip())
    # github_aliases also map to github type for matching.
    for alias in entry.get("github_aliases") or []:
        if alias:
            out["github"].add(str(alias).strip())
    return out


def fetch_signals(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT key_a_type, key_a_value, key_b_type, key_b_value, n_obs "
        "FROM identity_signals"
    ).fetchall()


def reconcile(conn: sqlite3.Connection,
              dry_run: bool = False,
              create_orphans: bool = False) -> dict:
    cfg = load_people()
    people = cfg.get("people") or []
    signals = fetch_signals(conn)

    # Index signals by every value for fast lookup.
    by_value: dict[tuple[str, str], list[tuple[str, str, int]]] = defaultdict(list)
    for at, av, bt, bv, n in signals:
        by_value[(at, av)].append((bt, bv, n))
        by_value[(bt, bv)].append((at, av, n))

    changes: list[tuple[int, str, str]] = []
    matched_values: set[tuple[str, str]] = set()

    for idx, entry in enumerate(people):
        known = collect_entry_values(entry)
        for ktype, kvals in known.items():
            for kv in kvals:
                matched_values.add((ktype, kv))

        # Walk transitively: collect candidates reachable from any known value.
        candidates: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for ktype, kvals in known.items():
            for kv in kvals:
                for other_type, other_val, n in by_value.get((ktype, kv), []):
                    if n < MIN_CONFIDENCE_FILL:
                        continue
                    if other_val in known.get(other_type, set()):
                        continue
                    candidates[other_type].append((other_val, n))

        # Apply candidates to entry.
        for ctype, vlist in candidates.items():
            field = TYPE_TO_FIELD.get(ctype)
            if not field:
                continue
            vlist.sort(key=lambda x: -x[1])
            best_val, best_n = vlist[0]
            if field in LIST_FIELDS:
                cur = list(entry.get(field) or [])
                if best_val not in cur:
                    cur.append(best_val)
                    entry[field] = cur
                    changes.append((idx, field, best_val))
                    matched_values.add((ctype, best_val))
            else:
                if entry.get(field):
                    continue  # do not overwrite existing scalar
                entry[field] = best_val
                changes.append((idx, field, best_val))
                matched_values.add((ctype, best_val))

    # Optional: auto-create slice entries for high-confidence orphan signals.
    orphans_created: list[dict] = []
    if create_orphans:
        # Group signals into connected components by shared values.
        # Simple union-find on (type, value) nodes.
        parent: dict[tuple[str, str], tuple[str, str]] = {}

        def find(x):
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for at, av, bt, bv, n in signals:
            if n < MIN_CONFIDENCE_ORPHAN:
                continue
            a, b = (at, av), (bt, bv)
            parent.setdefault(a, a)
            parent.setdefault(b, b)
            union(a, b)

        # Group by root, skip anything already matched to an entry.
        groups: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for node in parent:
            groups[find(node)].append(node)

        for root, nodes in groups.items():
            if any(n in matched_values for n in nodes):
                continue
            # Build new entry.
            new_entry: dict = {"scope": "org"}
            for ntype, nval in nodes:
                field = TYPE_TO_FIELD.get(ntype)
                if not field:
                    continue
                if field in LIST_FIELDS:
                    new_entry.setdefault(field, []).append(nval)
                else:
                    new_entry.setdefault(field, nval)
            if new_entry.get("email") or new_entry.get("jira_id") or \
               new_entry.get("slack_id") or new_entry.get("github"):
                orphans_created.append(new_entry)

        people.extend(orphans_created)

    if not dry_run and (changes or orphans_created):
        cfg["people"] = people
        PEOPLE.write_text(yaml.safe_dump(
            cfg, sort_keys=False, allow_unicode=True,
            default_flow_style=False, width=200,
        ))

    # Per-scope tally
    scope_tally = {"team": 0, "org": 0, "external": 0}
    coverage = {"email": 0, "jira_id": 0, "slack_id": 0, "github": 0}
    for p in people:
        scope_tally[p.get("scope", "org")] = scope_tally.get(p.get("scope", "org"), 0) + 1
        for f in coverage:
            if p.get(f) or (f == "github" and p.get("github_aliases")):
                coverage[f] += 1

    # Per-field counts of fills applied this run
    fill_breakdown: dict[str, int] = {}
    for _, field, _ in changes:
        fill_breakdown[field] = fill_breakdown.get(field, 0) + 1

    return {
        "computed_at": (__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(timespec="seconds").replace("+00:00", "Z")),
        "applied": not dry_run,
        "total_entries": len(people),
        "by_scope": scope_tally,
        "coverage": coverage,
        "changes": changes,
        "fill_breakdown": fill_breakdown,
        "orphans_created": orphans_created,
        "signals_total": len(signals),
    }


STATE_FILE = REPO / "state" / "last_identity_reconcile.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--create-orphans", action="store_true",
                    help="auto-create scope=slice entries for high-confidence "
                         "signal clusters with no existing match")
    ap.add_argument("--no-state", action="store_true",
                    help="skip writing state/last_identity_reconcile.json")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    init_signals(conn)
    res = reconcile(conn, dry_run=args.dry_run,
                    create_orphans=args.create_orphans)
    conn.close()

    print(f"signals={res['signals_total']}  entries={res['total_entries']}  "
          f"changes={len(res['changes'])}  orphans={len(res['orphans_created'])}  "
          f"applied={res['applied']}")
    for idx, field, val in res["changes"][:30]:
        print(f"  fill[{idx}] {field:18s} := {str(val)[:80]}")
    for o in res["orphans_created"][:20]:
        print(f"  orphan {o}")

    # Persist state for cron-status. Atomic via tmp + replace.
    if not args.no_state:
        import json, os
        snapshot = {
            "computed_at": res["computed_at"],
            "applied": res["applied"],
            "total_entries": res["total_entries"],
            "by_scope": res["by_scope"],
            "coverage": res["coverage"],
            "n_changes": len(res["changes"]),
            "fill_breakdown": res["fill_breakdown"],
            "n_orphans": len(res["orphans_created"]),
            "signals_total": res["signals_total"],
        }
        # Per-process tmp name: concurrent ingest runs (github/jira/confluence
        # each call this) must not share one tmp, else the first os.replace
        # moves it away and the second hits FileNotFoundError.
        tmp = STATE_FILE.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(snapshot, indent=2))
        os.replace(tmp, STATE_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())

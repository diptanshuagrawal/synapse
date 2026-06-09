#!/usr/bin/env python3
"""
compare_person_versions.py — one-off: V1 vs V2 vs V3 for all team members.

Runs build_v3 (which internally computes V1 person_profile + V2 person_census)
for every scope=team canonical over a window, extracts the comparison-relevant
fields, and writes a markdown comparison report. Read-only.

Usage:
    .venv/bin/python derive/compare_person_versions.py \\
        --since 2026-05-01T00:00:00Z --until 2026-05-28T23:59:59Z \\
        --out ../management/narratives/person-version-comparison-2026-05.md
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import yaml  # noqa: E402
from ingest.common import DB_PATH  # noqa: E402
from derive.person_v3 import build_v3  # noqa: E402


def _team() -> list[str]:
    people = yaml.safe_load((_REPO_ROOT / "config" / "people.yaml").read_text())["people"]
    return [p["canonical"] for p in people if p.get("scope") == "team" and p.get("canonical")]


def _own(block) -> int:
    return len((block or {}).get("primary", []))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    rows = []
    for name in _team():
        v3 = build_v3(conn, name, args.since, args.until)
        d = v3["delivery"]
        v1 = v3["v1_signals"]
        r = v3["rating"]
        dom = Counter(o.get("label") for o in (v1.get("domain_ownership") or []))
        rows.append({
            "person": name,
            "track": r["window_work_mix"],
            "baseline": r["baseline_role_120d"],
            "feature_applicable": r["feature_yardstick_applicable"],
            "v1_tier": (r["v1_feature_verdict"] or {}).get("tier_deviation"),
            "subjects": v3["coverage"]["subjects"],
            "coverage_ok": v3["coverage_ok"],
            "shipped": _own(d.get("shipped")),
            "fixed": _own(d.get("fixed")),
            "designed": _own(d.get("designed")),
            "built": _own(d.get("built")),
            "responded": _own(d.get("responded_to")),
            "ops": _own(d.get("ops")),
            "sp_attr": v1.get("sp_attributed"),
            "team_rank": v1.get("team_rank"),
            "pr_shipped": (v1.get("pr_fate_summary") or {}).get("shipped", 0),
            "pr_inflight": (v1.get("pr_fate_summary") or {}).get("in_flight", 0),
            "dom_owned": dom.get("OWNED", 0) + dom.get("DROVE", 0),
            "dom_jira_only": dom.get("JIRA_ONLY", 0),
            "own_by_signal": v3["own_by_signal"],
            "window_edge": len(v3.get("window_edge", [])),
        })

    L = []
    L.append(f"# Per-person version comparison — {args.since[:10]} → {args.until[:10]}")
    L.append("")
    L.append("V1 = person_profile (rating) · V2 = person_census (discovery) · V3 = merged + track-router.")
    L.append("")
    L.append("## Summary table")
    L.append("")
    L.append("| Person | window work-mix | baseline (120d) | feat-yardstick | V1 tier verdict | subjects | shipped(own) | fixed | designed | built | responded | V1 sp | V1 PR ship/flight | dom OWNED/DROVE |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        L.append(
            f"| {r['person']} | **{r['track']}** | {r['baseline']} | {'yes' if r['feature_applicable'] else 'NO'} | "
            f"{r['v1_tier']} | {r['subjects']} | {r['shipped']} | {r['fixed']} | {r['designed']} | "
            f"{r['built']} | {r['responded']} | {r['sp_attr']} | {r['pr_shipped']}/{r['pr_inflight']} | "
            f"{r['dom_owned']} |"
        )
    L.append("")
    L.append("## Mis-rating flags (V1 feature verdict NOT representative)")
    L.append("")
    flagged = [r for r in rows if not r["feature_applicable"]]
    if not flagged:
        L.append("_None — all members read as feature/mixed track; V1 feature verdict applies._")
    for r in flagged:
        L.append(
            f"- **{r['person']}** — track={r['track']}, V1 says `{r['v1_tier']}` but that's "
            f"feature-SP-based; real work = {r['shipped']} shipped / {r['designed']} designed / "
            f"{r['fixed']} fixed (own). own_by_signal={r['own_by_signal']}"
        )
    L.append("")
    L.append("## Coverage check")
    L.append("")
    for r in rows:
        L.append(f"- {r['person']}: coverage_ok={r['coverage_ok']}, subjects={r['subjects']}, window_edge={r['window_edge']}")

    out = (_REPO_ROOT / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)
    out.write_text("\n".join(L) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

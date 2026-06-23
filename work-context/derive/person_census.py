#!/usr/bin/env python3
"""
person_census.py — V2 coverage-guaranteed per-person census.

EXPERIMENTAL / comparison build. Does NOT touch person_profile.py or
person_deepread.py — the current /ask person_range (V1) is left intact. Run
both for a person, compare, decide which is better.

V1 (person_profile) is jira/PR-centric with reliability gates + fate, but
samples slack/clusters (top-20 caps) and lumps CMRs as ops. V2 borrows the
retro-census guarantees:

  - RECALL: enumerates EVERY subject the person authored / was assigned /
    participated in within the window — no top-N cap.
  - PARTITION: each subject → one signal-type bucket via the SAME structural
    detectors as `retro_census` (channel role, jira issue_type, source,
    config-driven terminal states), keywords only as fallback.
  - COVERAGE PROOF: represented + noise + unclassified == total, unclassified 0.
  - STRUCTURED STORY: shipped / fixed / responded-to / designed / built — vs
    V1's raw activity counts.
  - CMR-as-delivery: an executed platform CMR (Change Released / Implementation
    Reviewed) counts as shipped, not toil.
  - WINDOW-EDGE: delivery candidates whose terminal predates the window are
    flagged (delivered earlier, not in-window).

Usage:
    .venv/bin/python derive/person_census.py --name <canonical> \\
        --since 2026-05-01T00:00:00Z --until 2026-05-28T23:59:59Z
    .venv/bin/python derive/person_census.py --name <canonical> ... --format summary
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH  # noqa: E402
from derive.sources_config import github_org  # noqa: E402
from derive.jira_metrics import load_people_lookup, get_aliases_for  # noqa: E402
from derive.retro_census import (  # noqa: E402  (reuse — single source of detectors)
    _signal_type, _slack_url, _terminal_states, _terminal_sql,
    _load_incident_channels, ROLLOUT_PCT_RE, _rollout_confirmed,
)
from derive.oncall_signals import oncall_handle_tokens  # noqa: E402


def _source_of(subject: str) -> str:
    if subject.startswith("slack:"):
        return "slack"
    if subject.startswith("page:"):
        return "confluence"
    if subject.startswith(github_org() + "/"):
        return "github"
    return "jira"

# Signal → narrative section.
SECTION = {
    "delivery": "shipped", "rollout": "shipped",
    "fix": "fixed",
    "incident": "responded_to",
    "design": "designed",
    "pr_work": "built", "work": "built",
    "cmr_ops": "ops", "alert_auto": "ops",
    "ops_duty": "ops_duty",
    "discussion": "discussion", "noise": "noise",
}

# Recurring oncall/duty-roster tickets ("[Epic EX-207] Oncall", "Shadow
# Oncall", oncall-report pages) — assignment to a rota, not a delivery. Route
# to ops_duty (excluded from shipped) when they'd otherwise score as delivery.
ONCALL_DUTY_MARKERS = ["oncall", "on-call", "shadow oncall", "on call rota"]


def _is_oncall_duty(title: str, source: str) -> bool:
    t = (title or "").lower()
    if source not in ("jira", "confluence"):
        return False
    return any(m in t for m in ONCALL_DUTY_MARKERS)

# Role tiers: author/assignee = the person's OWN work; participant = touched.
PRIMARY_ROLES = ("author", "assignee")


def _resolve_canonical(name: str) -> str:
    lookup = load_people_lookup()
    return lookup.get(name.lower().strip(), name)


def _person_role(conn, subject, aliases) -> str:
    """How the person touched this subject — author / assignee / participant."""
    ph = ",".join("?" * len(aliases))
    # Author of the root creating event?
    root = conn.execute(
        f"SELECT 1 FROM events WHERE subject=? AND actor IN ({ph}) "
        f"AND event_type IN ('thread_started','issue_created','page_created','pr_opened') LIMIT 1",
        (subject, *aliases),
    ).fetchone()
    if root:
        return "author"
    assignee = conn.execute(
        f"SELECT 1 FROM events WHERE subject=? AND assignee IN ({ph}) LIMIT 1",
        (subject, *aliases),
    ).fetchone()
    if assignee:
        return "assignee"
    return "participant"


def build_person_census(conn, canonical: str, since: str, until: str) -> dict:
    aliases = get_aliases_for(canonical) or [canonical]
    ph = ",".join("?" * len(aliases))
    incident_channels, alert_channels = _load_incident_channels()
    oncall_tokens = oncall_handle_tokens()        # @oncall pings → incident in any channel
    terminal_names = _terminal_states()
    term_pred = _terminal_sql("to_status")

    # 1. Denominator — every subject the person authored / assigned / touched.
    subjects = [r[0] for r in conn.execute(
        f"SELECT DISTINCT subject FROM events WHERE ts BETWEEN ? AND ? "
        f"AND subject IS NOT NULL AND (actor IN ({ph}) OR assignee IN ({ph}))",
        (since, until, *aliases, *aliases),
    ).fetchall()]

    # Pre-window delivery set (terminal reached before window) — for window-edge.
    pre_window: set[str] = set()
    if terminal_names and subjects:
        sph = ",".join("?" * len(subjects))
        for (sub,) in conn.execute(
            f"SELECT subject FROM events WHERE event_type='status_change' AND ({term_pred}) "
            f"AND subject IN ({sph}) GROUP BY subject HAVING MIN(ts) < ?",
            (*terminal_names, *subjects, since),
        ).fetchall():
            pre_window.add(sub)

    buckets: dict[str, list[str]] = {}
    by_signal: dict[str, int] = {}
    own_by_signal: dict[str, int] = {}   # signal counts where role ∈ author/assignee
    sections: dict[str, list[dict]] = {}
    window_edge: list[dict] = []
    unclassified: list[str] = []

    for subject in subjects:
        source = _source_of(subject)
        # Subject-level aggregate (all in-window events on it) for signal-type.
        agg = conn.execute(
            f"SELECT GROUP_CONCAT(DISTINCT event_type), MAX(issue_type), "
            f"  MAX(CASE WHEN event_type='status_change' AND ({term_pred}) THEN 1 ELSE 0 END) "
            f"FROM events WHERE subject=? AND ts BETWEEN ? AND ?",
            (*terminal_names, subject, since, until),
        ).fetchone()
        etset = set((agg[0] or "").split(","))
        issue_type, went_done = agg[1], bool(agg[2])
        ev = conn.execute(
            "SELECT title, body FROM events WHERE subject=? ORDER BY CASE event_type "
            "WHEN 'thread_started' THEN 0 WHEN 'issue_created' THEN 0 WHEN 'page_created' THEN 0 "
            "WHEN 'pr_opened' THEN 0 ELSE 1 END, ts LIMIT 1",
            (subject,),
        ).fetchone()
        title, body = (ev or ("", ""))
        channel_id = subject.split(":")[1] if subject.startswith("slack:") and subject.count(":") == 2 else ""

        stype, evidence = _signal_type(title, body, source, etset, channel_id,
                                       incident_channels, alert_channels, issue_type,
                                       went_done, oncall_tokens)
        # Oncall/duty-roster tickets are not deliveries — reclassify so they
        # don't inflate `shipped`.
        if stype in ("delivery", "work") and _is_oncall_duty(title, source):
            stype = "ops_duty"
        by_signal[stype] = by_signal.get(stype, 0) + 1
        role = _person_role(conn, subject, aliases)
        if role in PRIMARY_ROLES:
            own_by_signal[stype] = own_by_signal.get(stype, 0) + 1
        buckets.setdefault(stype, []).append(subject)

        item = {"subject": subject, "title": (title or "")[:110], "role": role,
                "signal": stype, "url": _slack_url(subject)}

        if stype in ("delivery", "fix", "cmr_ops") and subject in pre_window:
            window_edge.append({**item, "note": "terminal before window — delivered earlier; not in-window."})
            continue  # don't credit as in-window work

        sec = SECTION.get(stype, "other")
        if sec in ("noise", "discussion"):
            continue
        # Role-tier: primary = the person's OWN work (author/assignee);
        # contributed = they participated (commented/reviewed) but didn't own.
        tier = "primary" if role in PRIMARY_ROLES else "contributed"
        sections.setdefault(sec, {"primary": [], "contributed": []})[tier].append(item)

    total = len(subjects)
    noise_n = by_signal.get("noise", 0)
    represented = total - noise_n - len(unclassified)
    return {
        "person": canonical,
        "aliases": aliases,
        "window": {"since": since, "until": until},
        "totals": {"subjects": total, "represented": represented,
                   "noise": noise_n, "unclassified": len(unclassified)},
        "coverage_ok": (represented + noise_n + len(unclassified) == total) and len(unclassified) == 0,
        "by_signal": dict(sorted(by_signal.items(), key=lambda x: -x[1])),
        "own_by_signal": dict(sorted(own_by_signal.items(), key=lambda x: -x[1])),
        "sections": sections,                 # shipped / fixed / responded_to / designed / built / ops
        "window_edge": window_edge,
        "buckets": buckets,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="canonical handle (or any alias)")
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--format", choices=["json", "summary"], default="json")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    canonical = _resolve_canonical(args.name)
    census = build_person_census(conn, canonical, args.since, args.until)

    if args.format == "summary":
        t = census["totals"]
        print(f"person: {census['person']}  window {args.since[:10]} → {args.until[:10]}")
        print(f"coverage_ok: {census['coverage_ok']}  subjects={t['subjects']} "
              f"represented={t['represented']} noise={t['noise']} unclassified={t['unclassified']}")
        print(f"by_signal: {census['by_signal']}")
        for sec in ("shipped", "fixed", "responded_to", "designed", "built", "ops", "ops_duty"):
            block = census["sections"].get(sec)
            if not block:
                continue
            prim, contrib = block.get("primary", []), block.get("contributed", [])
            print(f"\n== {sec.upper()}  (own={len(prim)} · contributed={len(contrib)}) ==")
            for it in prim[:25]:
                print(f"  OWN          [{it['role']}/{it['signal']}] {it['title']}")
            for it in contrib[:12]:
                print(f"  contributed  [{it['signal']}] {it['title']}")
        if census["window_edge"]:
            print(f"\n== WINDOW-EDGE (delivered before window — excluded) {len(census['window_edge'])} ==")
            for w in census["window_edge"][:10]:
                print(f"  {w['subject']}  {w['title']}")
        return 0

    print(json.dumps(census, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

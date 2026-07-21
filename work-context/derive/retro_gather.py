#!/usr/bin/env python3
"""One-shot gather for /retro — replaces ~12 sequential chat-driven commands.

The /retro skill previously ran: retro_census.py (twice), 7 sqlite heredocs
(1a-1f, 1h), alerts.md + per-domain + per-person file reads, 3 ask_engine
invocations (window / rootcauses / projects-window), and mom_extractor — each
as its own tool round-trip. This script runs ALL of it in one call:

    .venv/bin/python derive/retro_gather.py \
        --since "<START_TS>" --until "<END_TS>" > /tmp/retro_gather_summary.json

It writes the full bundle to /tmp/retro_gather.json and ALSO writes the legacy
per-piece files (/tmp/retro_census.json, /tmp/retro_active_clusters.json,
/tmp/retro_root_causes.json, /tmp/retro_projects.json, /tmp/retro_moms.json)
so downstream skill references keep working. Stdout is a compact summary
(coverage gate + counts + paths) — the model reads the bundle, not 12 outputs.

Bundle keys:
  census            — retro_census.build_census output (the recall denominator)
  event_volume      — 1a per source/event_type counts
  pr_cycle          — 1b opened→merged hours (n/avg/min/max)
  person_activity   — 1c per-actor event counts (bots excluded)
  shipped_done      — 1d jira status_change → Done rows
  sprints           — 1e sprint composition (top 10)
  risk_flags        — 1f risk-flagged subjects (top 30 by confidence)
  alerts_md         — 1g derived/alerts.md full text (current-state snapshot)
  domain_volume     — 1h top 10 domains by window subject count
  project_rollups   — 1i derived/projects/<slug>.md text for top-5 domains
  people_profiles   — 1j derived/people/<handle>.md text for top contributors
  active_clusters   — 1k ask_engine window
  root_causes       — 1k ask_engine rootcauses
  projects_window   — 1k-bis ask_engine projects-window
  moms              — 1m mom_extractor output
  team_velocity     — Phase-0 team_velocity_baseline (per-actor deduped SP)
  ops_by_person     — Phase-0 detect_ops_tickets per team canonical
  ownership_by_person — Phase-0 compute_pr_author_ownership per team canonical
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

_VENV_PY = _PKG_ROOT / ".venv" / "bin" / "python"
_DB = _PKG_ROOT / "index" / "events.db"
_TMP = Path("/tmp")
_FILE_CAP = 8000  # chars kept per derived md file (truncation noted in-band)


def _rows(cur, sql, params):
    cur.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _sqlite_metrics(conn, since, until):
    cur = conn.cursor()
    w = (since, until)
    out = {}
    out["event_volume"] = _rows(cur, """
        SELECT source, event_type, COUNT(*) AS n FROM events
        WHERE ts BETWEEN ? AND ? GROUP BY source, event_type
        ORDER BY source, n DESC""", w)
    out["pr_cycle"] = _rows(cur, """
        WITH opens AS (
          SELECT subject, MIN(ts) AS opened_ts FROM events
          WHERE event_type = 'pr_opened' AND ts BETWEEN ? AND ? GROUP BY subject),
        merges AS (
          SELECT subject, MIN(ts) AS merged_ts FROM events
          WHERE event_type = 'pr_merged' AND ts BETWEEN ? AND ? GROUP BY subject)
        SELECT COUNT(*) AS n_merged,
          ROUND(AVG((julianday(merged_ts)-julianday(opened_ts))*24),1) AS avg_hours,
          ROUND(MIN((julianday(merged_ts)-julianday(opened_ts))*24),1) AS min_hours,
          ROUND(MAX((julianday(merged_ts)-julianday(opened_ts))*24),1) AS max_hours
        FROM merges m JOIN opens o USING (subject)
        WHERE m.merged_ts >= o.opened_ts""", w + w)
    out["person_activity"] = _rows(cur, """
        SELECT actor, event_type, COUNT(*) AS n FROM events
        WHERE ts BETWEEN ? AND ? AND actor IS NOT NULL
          AND actor NOT LIKE '%[bot]%' AND actor != 'matterai'
        GROUP BY actor, event_type ORDER BY actor, n DESC""", w)
    out["shipped_done"] = _rows(cur, """
        SELECT subject, actor, title, ts FROM events
        WHERE source='jira' AND event_type='status_change'
          AND ts BETWEEN ? AND ? AND title LIKE '%→ Done%' ORDER BY ts""", w)
    out["sprints"] = _rows(cur, """
        SELECT sprint_name, sprint_state, COUNT(DISTINCT subject) AS tickets,
               ROUND(SUM(story_points),1) AS total_points
        FROM events WHERE source='jira' AND event_type='issue_created'
          AND sprint_name IS NOT NULL AND sprint_name != ''
        GROUP BY sprint_name, sprint_state ORDER BY tickets DESC LIMIT 10""", ())
    out["risk_flags"] = _rows(cur, """
        SELECT s.subject, s.summary, s.risk_flags, s.confidence
        FROM subject_summary s JOIN events e ON e.subject = s.subject
        WHERE e.ts BETWEEN ? AND ? AND s.risk_flags != '[]' AND s.risk_flags != ''
        GROUP BY s.subject ORDER BY s.confidence DESC LIMIT 30""", w)
    out["domain_volume"] = _rows(cur, """
        WITH win_subjects AS (
          SELECT DISTINCT subject FROM events
          WHERE ts BETWEEN ? AND ? AND subject IS NOT NULL)
        SELECT domain.value AS domain, COUNT(*) AS subjects
        FROM subject_summary, json_each(subject_summary.domains) AS domain
        WHERE subject IN (SELECT subject FROM win_subjects)
        GROUP BY domain.value ORDER BY subjects DESC LIMIT 10""", w)
    return out


def _read_capped(path: Path) -> str | None:
    if not path.exists():
        return None
    t = path.read_text(errors="replace")
    if len(t) > _FILE_CAP:
        t = t[:_FILE_CAP] + f"\n…[truncated at {_FILE_CAP} chars — Read {path} for the rest]"
    return t


def _subprocess_json(argv, out_file):
    r = subprocess.run(argv, capture_output=True, text=True, cwd=str(_PKG_ROOT))
    if r.returncode != 0:
        return {"_error": r.stderr.strip()[:400]}
    out_file.write_text(r.stdout)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        return {"_error": f"non-JSON output: {e}"}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--out", default="/tmp/retro_gather.json")
    args = ap.parse_args()
    since, until = args.since, args.until

    # 4 independent subprocesses run concurrently with the in-process work
    jobs = {
        "active_clusters": ([str(_VENV_PY), "derive/ask_engine.py", "window",
                             "--since", since, "--until", until],
                            _TMP / "retro_active_clusters.json"),
        "root_causes": ([str(_VENV_PY), "derive/ask_engine.py", "rootcauses",
                         "--since", since, "--until", until],
                        _TMP / "retro_root_causes.json"),
        "projects_window": ([str(_VENV_PY), "derive/ask_engine.py", "projects-window",
                             "--since", since, "--until", until],
                            _TMP / "retro_projects.json"),
        "moms": ([str(_VENV_PY), "derive/mom_extractor.py",
                  "--since", since, "--until", until],
                 _TMP / "retro_moms.json"),
    }
    pool = ThreadPoolExecutor(max_workers=4)
    futures = {k: pool.submit(_subprocess_json, argv, f) for k, (argv, f) in jobs.items()}

    bundle = {"window": {"since": since, "until": until}}

    conn = sqlite3.connect(str(_DB))
    conn.execute("PRAGMA busy_timeout=30000")

    from derive.retro_census import build_census
    census = build_census(conn, since, until)
    (_TMP / "retro_census.json").write_text(json.dumps(census, default=str))
    bundle["census"] = census

    bundle.update(_sqlite_metrics(conn, since, until))
    bundle["alerts_md"] = _read_capped(_PKG_ROOT / "derived" / "alerts.md")

    # 1i — per-domain rollups for the top 5 window domains
    bundle["project_rollups"] = {}
    for row in bundle["domain_volume"][:5]:
        slug = row["domain"]
        bundle["project_rollups"][slug] = _read_capped(
            _PKG_ROOT / "derived" / "projects" / f"{slug}.md")

    # Phase-0 jira_metrics (shared module — never re-implemented)
    from derive.jira_metrics import (team_velocity_baseline, all_team_canonicals,
                                     get_aliases_for, detect_ops_tickets,
                                     compute_pr_author_ownership)
    bundle["team_velocity"] = team_velocity_baseline(conn, since, until)
    bundle["ops_by_person"], bundle["ownership_by_person"] = {}, {}
    for canon in all_team_canonicals():
        aliases = get_aliases_for(canon)
        ops = detect_ops_tickets(conn, aliases, since, until)
        bundle["ops_by_person"][canon] = [
            dataclasses.asdict(o) if dataclasses.is_dataclass(o) else o for o in ops]
        bundle["ownership_by_person"][canon] = compute_pr_author_ownership(
            conn, aliases, since, until)

    # 1j — per-person profiles for top contributors by window activity.
    # person_activity actors are RAW ids (slack U…, emails, gh logins) — rank on
    # canonical handles via the shared identity map so profile paths resolve.
    from derive.jira_metrics import load_people_lookup
    lookup = load_people_lookup()
    totals = {}
    for r in bundle["person_activity"]:
        canon = lookup.get(r["actor"]) or lookup.get(str(r["actor"]).lower())
        if canon:
            totals[canon] = totals.get(canon, 0) + r["n"]
    top_actors = sorted(totals, key=totals.get, reverse=True)[:8]
    # profile files are keyed by raw handle (often the github login), not the
    # canonical — match any alias of the person against the existing files.
    profile_stems = {p.stem for p in (_PKG_ROOT / "derived" / "people").glob("*.md")}
    bundle["people_profiles"] = {}
    for canon in top_actors:
        stem = next((a for a in [canon] + get_aliases_for(canon)
                     if a in profile_stems), None)
        bundle["people_profiles"][canon] = (
            _read_capped(_PKG_ROOT / "derived" / "people" / f"{stem}.md")
            if stem else None)

    for k, fut in futures.items():
        bundle[k] = fut.result()
    pool.shutdown()

    Path(args.out).write_text(json.dumps(bundle, default=str))

    summary = {
        "bundle": args.out,
        "legacy_files": {k: str(f) for k, (_, f) in jobs.items()}
        | {"census": str(_TMP / "retro_census.json")},
        "coverage_ok": census.get("coverage_ok"),
        "unclassified": (census.get("totals") or {}).get("unclassified"),
        "counts": {
            "census_subjects": (census.get("totals") or {}).get("subjects"),
            "shipped_done": len(bundle["shipped_done"]),
            "risk_flags": len(bundle["risk_flags"]),
            "active_clusters": len(bundle["active_clusters"])
            if isinstance(bundle["active_clusters"], list)
            else bundle["active_clusters"].get("_error", "see bundle"),
            "moms": len(bundle["moms"]) if isinstance(bundle["moms"], list)
            else bundle["moms"].get("_error", "see bundle"),
            "top_domains": [r["domain"] for r in bundle["domain_volume"][:5]],
            "top_actors": top_actors,
        },
        "errors": {k: v["_error"] for k, v in bundle.items()
                   if isinstance(v, dict) and "_error" in v},
    }
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()

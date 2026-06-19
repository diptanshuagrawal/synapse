#!/usr/bin/env python3
"""
pipeline_validate.py — cross-cutting INTEGRITY checks on events.db.

Sibling to the per-source data-quality validators (jira/github/confluence/
slack_validate). Those answer "is the *content* attributed correctly?". This
answers "is the *pipeline* structurally sound?" — the invariants that hold
regardless of source, and whose violation means the ingest plumbing (not the
data) broke:

  schema_nulls    NOT NULL columns (id/source/event_type/ts/raw_path) really
                  non-null — a bypassed writer or partial migration shows here.
  ts_format       every ts parses as YYYY-MM-DDThh… ISO8601 (cheap GLOB scan).
  ts_future       no events stamped implausibly in the future (clock-skew /
                  bad parse upstream) — silently poisons every date window.
  source_vocab    source ∈ known set; an unexpected source is a typo or a new
                  ingest path that hasn't been registered (WARN, not fatal).
  type_vocab      event_type ∈ known set (WARN — vocabulary evolves).
  orphan_refs     event_refs rows with no parent event (referential leak).
  fts_sync        events_fts row count == events row count (full-text search
                  silently misses rows when these drift).
  raw_path_dupes  raw_path collisions within ingested sources — the exact
                  symptom of the append_raw line-number race (see common.py).
  freshness       per active source, hours since its latest event vs a
                  staleness budget. This is the SILENT-STALE guard: the
                  placeholder-auth freeze that froze jira+confluence for 2 days
                  behind green markers would have tripped here.

  ── ingest-shape invariants (what breaks when a parser drifts) ──
  slack_channel_id  slack events with a NULL channel_id — breaks channel
                  attribution + the team-involved filter (a real seed-era bug).
  null_actor_subject  non-'service' events missing actor or subject — an
                  upstream parse regression; 'service' briefs legitimately have
                  a null actor and are exempt.
  subject_shape   per-source subject matches its expected id grammar (jira
                  PROJ-N, github org/repo#N|@sha, confluence page:ID, slack
                  slack:C…:ts, service service:…); off-shape = parse bug.
  ref_vocab       event_refs.ref_type ∈ known set + ref_value non-empty — an
                  unknown ref_type is a new (unregistered) ref path.

Report-only: every problem becomes a PASS/WARN/FAIL finding in the same JSON
contract the other validators use. It NEVER blocks ingest (main() returns 0
unless the DB is missing / unreadable), exactly like its siblings — the run-*.sh
wrappers refresh the cache fail-soft and cron-status renders it.

CLI
---
    .venv/bin/python derive/pipeline_validate.py            # human-readable
    .venv/bin/python derive/pipeline_validate.py --json     # for cron-status
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH, STATE_PATH  # noqa: E402

SOURCE = "pipeline"

RED, YEL, GRN, DIM, RST = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[0m"

# Known vocabulary. Generous on purpose — an unknown value is a WARN signal to
# register a new ingest path, never a hard failure. Keep in sync with the
# event_type literals in ingest/*.py + derive/slack_upsert.py.
KNOWN_SOURCES = {"github", "jira", "confluence", "slack", "service"}
KNOWN_EVENT_TYPES = {
    # jira
    "issue_created", "status_change", "assignment", "sprint_change",
    "story_points_change", "comment",
    # github
    "pr_opened", "pr_merged", "pr_closed", "pr_merged_by", "review",
    "commit_in_pr", "commit_pushed",
    # confluence
    "page_created", "page_updated",
    # slack
    "thread_started", "thread_reply",
    # derived service briefs
    "service_brief",
}

# Per-source freshness budget (hours). Beyond WARN → drifting; beyond FAIL →
# likely a silent stall (auth/cursor freeze). Sources not listed are skipped
# (e.g. 'service' is generated on demand, not on a cadence).
FRESHNESS_BUDGET_H = {
    "github":     {"warn": 36, "fail": 96},
    "jira":       {"warn": 36, "fail": 96},
    "confluence": {"warn": 48, "fail": 120},
    "slack":      {"warn": 24, "fail": 72},
}

FUTURE_SKEW_H = 48  # ts more than this far ahead of now → implausible

# event_refs.ref_type vocabulary — an unknown value is a new ref path that
# hasn't been registered (WARN, not fatal). Keep in sync with the ref_type
# literals emitted by common.enrich_refs + ingest/*.py.
KNOWN_REF_TYPES = {"page", "person", "project", "pull_request", "slack_thread", "ticket"}

# Per-source subject id grammar. A subject that doesn't match its source's
# shape is an upstream parse bug (or a new subject family to register). Anchored,
# compiled lazily in compute(). 'service' derived briefs are intentionally loose.
SUBJECT_SHAPE = {
    "jira":       r"^[A-Z][A-Z0-9]+-\d+$",
    "github":     r"^[\w.-]+/[\w.-]+(#\d+|@[0-9a-f]+)$",
    "confluence": r"^page:\d+$",
    "slack":      r"^slack:[A-Z0-9]+:\d+\.\d+$",
    "service":    r"^service:",
}

# Sources whose events must carry an actor. 'service' briefs are author-less by
# design, so they're exempt from the null-actor check (but not null-subject).
ACTOR_EXEMPT_SOURCES = {"service"}


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def compute(conn: sqlite3.Connection) -> dict:
    findings: list[list[str]] = []
    stats: dict = {}

    n_total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    stats["n_total_events"] = n_total

    if n_total == 0:
        findings.append(["WARN", "empty_db", "events table is empty — nothing to validate"])
        return {"computed_at": _now_iso(), "source": SOURCE,
                "n_total_events": 0, "stats": stats, "findings": findings}

    # ── schema_nulls ───────────────────────────────────────────────────────
    n_null = conn.execute(
        "SELECT COUNT(*) FROM events WHERE id IS NULL OR source IS NULL "
        "OR event_type IS NULL OR ts IS NULL OR raw_path IS NULL"
    ).fetchone()[0]
    if n_null:
        findings.append(["FAIL", "schema_nulls",
                         f"{n_null} row(s) violate a NOT NULL column "
                         "(id/source/event_type/ts/raw_path) — corrupt writer or bad migration"])
    else:
        findings.append(["PASS", "schema_nulls", "required columns populated"])

    # ── ts_format (GLOB shape scan; cheap, full table) ──────────────────────
    n_badts = conn.execute(
        "SELECT COUNT(*) FROM events "
        "WHERE ts NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*'"
    ).fetchone()[0]
    if n_badts:
        sample = [r[0] for r in conn.execute(
            "SELECT ts FROM events "
            "WHERE ts NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*' LIMIT 3"
        )]
        findings.append(["FAIL", "ts_format",
                         f"{n_badts} event(s) have non-ISO8601 ts; e.g. {sample}"])
    else:
        findings.append(["PASS", "ts_format", "all ts are ISO8601-shaped"])

    # ── ts_future (lexical compare works for Z-suffixed ISO) ────────────────
    future_cut = (_now() + timedelta(hours=FUTURE_SKEW_H)).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    n_future = conn.execute(
        "SELECT COUNT(*) FROM events WHERE ts > ?", (future_cut,)
    ).fetchone()[0]
    if n_future:
        findings.append(["WARN", "ts_future",
                         f"{n_future} event(s) stamped >{FUTURE_SKEW_H}h in the future — "
                         "clock skew or upstream parse bug; pollutes date windows"])
    else:
        findings.append(["PASS", "ts_future", "no implausible future timestamps"])

    # ── source / event_type vocabulary ──────────────────────────────────────
    src_rows = conn.execute(
        "SELECT source, COUNT(*) FROM events GROUP BY source").fetchall()
    stats["by_source"] = {s: n for s, n in src_rows}
    unknown_src = [(s, n) for s, n in src_rows if s not in KNOWN_SOURCES]
    if unknown_src:
        findings.append(["WARN", "source_vocab",
                         f"unregistered source(s): {', '.join(f'{s}({n})' for s, n in unknown_src)}"])
    else:
        findings.append(["PASS", "source_vocab", f"{len(src_rows)} known source(s)"])

    type_rows = conn.execute(
        "SELECT event_type, COUNT(*) FROM events GROUP BY event_type").fetchall()
    unknown_types = [(t, n) for t, n in type_rows if t not in KNOWN_EVENT_TYPES]
    if unknown_types:
        findings.append(["WARN", "type_vocab",
                         f"unregistered event_type(s): {', '.join(f'{t}({n})' for t, n in unknown_types)}"])
    else:
        findings.append(["PASS", "type_vocab", f"{len(type_rows)} known event_type(s)"])

    # ── orphan_refs (referential integrity) ──────────────────────────────────
    if _table_exists(conn, "event_refs"):
        n_orphan = conn.execute(
            "SELECT COUNT(*) FROM event_refs r "
            "LEFT JOIN events e ON r.event_id = e.id WHERE e.id IS NULL"
        ).fetchone()[0]
        if n_orphan:
            findings.append(["FAIL", "orphan_refs",
                             f"{n_orphan} event_refs row(s) point to a missing event "
                             "— refs leaked past their parent (bad delete / partial insert)"])
        else:
            findings.append(["PASS", "orphan_refs", "every ref has a parent event"])

    # ── fts_sync ─────────────────────────────────────────────────────────────
    if _table_exists(conn, "events_fts"):
        try:
            n_fts = conn.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0]
            if n_fts != n_total:
                findings.append(["WARN", "fts_sync",
                                 f"events_fts has {n_fts} rows vs {n_total} events — "
                                 "full-text search is missing rows; rebuild the FTS index"])
            else:
                findings.append(["PASS", "fts_sync", f"FTS index matches ({n_fts} rows)"])
        except sqlite3.DatabaseError as e:
            findings.append(["WARN", "fts_sync", f"could not read events_fts: {e}"])

    # ── raw_path_dupes (the append_raw line-number race) ──────────────────────
    # Scope to the append_raw back-reference shape ('raw/<src>/…#<line>') only.
    # Derived sources (e.g. 'service' briefs) legitimately reuse a rendered .md
    # path as raw_path — they never went through append_raw, so a shared value
    # there is not the line-number race and must not false-alarm.
    _RAW_GLOB = "raw/*#*"
    dupes = conn.execute(
        "SELECT raw_path, COUNT(*) c FROM events "
        "WHERE raw_path GLOB ? "
        "GROUP BY raw_path HAVING c > 1 ORDER BY c DESC LIMIT 5",
        (_RAW_GLOB,),
    ).fetchall()
    n_dupe_groups = conn.execute(
        "SELECT COUNT(*) FROM (SELECT raw_path FROM events "
        "WHERE raw_path GLOB ? "
        "GROUP BY raw_path HAVING COUNT(*) > 1)",
        (_RAW_GLOB,),
    ).fetchone()[0]
    if n_dupe_groups:
        sample = ", ".join(f"{rp}×{c}" for rp, c in dupes)
        findings.append(["FAIL", "raw_path_dupes",
                         f"{n_dupe_groups} raw_path value(s) shared by >1 event "
                         f"(append_raw line-number race); e.g. {sample}"])
    else:
        findings.append(["PASS", "raw_path_dupes", "raw_path back-references are unique"])

    # ── slack_channel_id (attribution integrity) ─────────────────────────────
    if "slack" in stats["by_source"]:
        n_no_chan = conn.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE source='slack' AND (channel_id IS NULL OR channel_id='')"
        ).fetchone()[0]
        if n_no_chan:
            findings.append(["FAIL", "slack_channel_id",
                             f"{n_no_chan} slack event(s) have no channel_id — "
                             "breaks channel attribution + the team-involved filter"])
        else:
            findings.append(["PASS", "slack_channel_id", "every slack event has a channel_id"])

    # ── null_actor_subject (parse-regression guard) ──────────────────────────
    exempt = ",".join(f"'{s}'" for s in sorted(ACTOR_EXEMPT_SOURCES)) or "''"
    n_no_actor = conn.execute(
        f"SELECT COUNT(*) FROM events "
        f"WHERE (actor IS NULL OR actor='') AND source NOT IN ({exempt})"
    ).fetchone()[0]
    n_no_subj = conn.execute(
        "SELECT COUNT(*) FROM events WHERE subject IS NULL OR subject=''"
    ).fetchone()[0]
    if n_no_actor or n_no_subj:
        bits = []
        if n_no_actor:
            bits.append(f"{n_no_actor} non-service event(s) with no actor")
        if n_no_subj:
            bits.append(f"{n_no_subj} event(s) with no subject")
        findings.append(["FAIL", "null_actor_subject",
                         "; ".join(bits) + " — upstream parse regression"])
    else:
        findings.append(["PASS", "null_actor_subject",
                         "actor + subject populated (service actor-exempt)"])

    # ── subject_shape (per-source id grammar) ────────────────────────────────
    shape_bad: dict[str, int] = {}
    for src, pat in SUBJECT_SHAPE.items():
        if src not in stats["by_source"]:
            continue
        rx = re.compile(pat)
        n_bad = sum(
            1 for (subj,) in conn.execute(
                "SELECT subject FROM events WHERE source=? AND subject IS NOT NULL", (src,))
            if not rx.match(subj)
        )
        if n_bad:
            shape_bad[src] = n_bad
    stats["subject_shape_bad"] = shape_bad
    if shape_bad:
        by = ", ".join(f"{s}({n})" for s, n in shape_bad.items())
        findings.append(["WARN", "subject_shape",
                         f"off-shape subject id(s): {by} — upstream parse bug or "
                         "a new subject family to register"])
    else:
        findings.append(["PASS", "subject_shape", "subjects match their source grammar"])

    # ── ref_vocab (event_refs ref_type + ref_value sanity) ───────────────────
    if _table_exists(conn, "event_refs"):
        rt_rows = conn.execute(
            "SELECT ref_type, COUNT(*) FROM event_refs GROUP BY ref_type").fetchall()
        unknown_rt = [(t, n) for t, n in rt_rows if t not in KNOWN_REF_TYPES]
        n_empty_rv = conn.execute(
            "SELECT COUNT(*) FROM event_refs WHERE ref_value IS NULL OR ref_value=''"
        ).fetchone()[0]
        if unknown_rt or n_empty_rv:
            bits = []
            if unknown_rt:
                bits.append("unregistered ref_type(s): "
                            + ", ".join(f"{t}({n})" for t, n in unknown_rt))
            if n_empty_rv:
                bits.append(f"{n_empty_rv} ref(s) with empty ref_value")
            findings.append(["WARN", "ref_vocab", "; ".join(bits)])
        else:
            findings.append(["PASS", "ref_vocab",
                             f"{len(rt_rows)} known ref_type(s), all ref_values populated"])

    # ── freshness (silent-stale guard) ───────────────────────────────────────
    now = _now()
    fresh_stats: dict = {}
    worst = "PASS"
    by_source = stats["by_source"]
    for src, budget in FRESHNESS_BUDGET_H.items():
        if src not in by_source:
            continue  # source not ingested into this DB; skip rather than false-alarm
        row = conn.execute(
            "SELECT MAX(ts) FROM events WHERE source = ?", (src,)).fetchone()
        last = _parse_ts(row[0] if row else None)
        if last is None:
            continue
        age_h = (now - last).total_seconds() / 3600
        fresh_stats[src] = round(age_h, 1)
        if age_h >= budget["fail"]:
            worst = "FAIL"
            findings.append(["FAIL", f"freshness:{src}",
                             f"latest {src} event is {age_h:.0f}h old "
                             f"(budget {budget['fail']}h) — likely silent stall (auth/cursor freeze)"])
        elif age_h >= budget["warn"]:
            if worst != "FAIL":
                worst = "WARN"
            findings.append(["WARN", f"freshness:{src}",
                             f"latest {src} event is {age_h:.0f}h old (budget {budget['warn']}h) — drifting"])
    stats["freshness_age_h"] = fresh_stats
    if worst == "PASS" and fresh_stats:
        findings.append(["PASS", "freshness",
                         "all active sources within freshness budget"])

    return {
        "computed_at": _now_iso(),
        "source": SOURCE,
        "n_total_events": n_total,
        "stats": stats,
        "findings": findings,
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone() is not None


def _render_human(report: dict) -> None:
    print(f"\n=== pipeline_validate · {report['computed_at']} ===")
    print(f"  total events:  {report['n_total_events']:,}")
    by_src = report.get("stats", {}).get("by_source", {})
    if by_src:
        print("  by source:     " + "  ".join(f"{s}={n:,}" for s, n in sorted(by_src.items())))
    fresh = report.get("stats", {}).get("freshness_age_h", {})
    if fresh:
        print("  freshness(h):  " + "  ".join(f"{s}={h}" for s, h in sorted(fresh.items())))
    print()
    for sev, check, msg in report["findings"]:
        col = GRN if sev == "PASS" else (YEL if sev == "WARN" else RED)
        print(f"  {col}{sev:4s}{RST}  {check:18s}  {msg}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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

#!/usr/bin/env python3
"""
auto_recurring.py — auto-stub enrichment for `Recurring …` clusters.

Many clusters fall into a stable RECURRING pattern: channel joins/leaves,
alerting ack templates, weekly oncall report postings, video call links.
The labelling rule asks chat to lead such labels with `Recurring `. There's
no real enrichment to do — they have no decisions, no blockers, status is
fixed RECURRING.

Two detection paths:

1. `--label-prefix` (default + always-on): finds rows with
   `label LIKE 'Recurring %'` AND `status IS NULL`, and stubs them.

2. `--scan-content`: ALSO scans member bodies for template patterns
   even when the label doesn't say "Recurring". Detected patterns:
     - channel-join / leave events (regex on body)
     - daily-report templates (shared 40-char body prefix across ≥80% members)
     - CMR-request pings (subteam-ping + execute/help/process verbs)
     - bot-dominant clusters (raw-bot author > 50% of members)
     - standup-nudge templates (USLACKBOT + "please join standup")

   When a content pattern matches, label is REWRITTEN as
   `Recurring <pattern>` and the row is stubbed.

   Use `--scan-content --dry-run` first to inspect proposed
   re-labels before committing.

Stubbed fields:

    status            = 'RECURRING'
    decisions_json    = '[]'
    blockers_json     = '[]'
    outcomes_json     = '[]'   (v2 schema)
    followups_json    = '[]'   (v2 schema)
    risk_areas_json   = '[]'   (v2 schema)
    root_cause        = NULL
    first_ts, last_activity_ts = computed from member events (when null)

Idempotent: rows with status already set are skipped. Re-running is safe.

CLI
---
    .venv/bin/python derive/auto_recurring.py                       # label-prefix only
    .venv/bin/python derive/auto_recurring.py --scan-content        # + content patterns
    .venv/bin/python derive/auto_recurring.py --scan-content --dry-run  # propose only
    .venv/bin/python derive/auto_recurring.py --scan-content --max 20   # cap detections (small-batch testing)
    .venv/bin/python derive/auto_recurring.py --json                 # machine-readable

Hook
----
Should be invoked right after label_clusters.py apply (or the direct-UPDATE
path used during cluster_diff apply). Reduces the chat enrichment batch by
~30-50% per refresh cycle.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH  # noqa: E402
from derive.sources_config import recurring_prefixes  # noqa: E402


# ── Template-pattern regexes ──────────────────────────────────────────────

_RE_CHANNEL_JOIN = re.compile(
    r"<@U[A-Z0-9]+(\|[^>]+)?>\s*has\s+(joined|left)\s+the\s+channel",
    re.IGNORECASE,
)
_RE_SUBTEAM_PING_HELP = re.compile(
    r"<!subteam\^S[A-Z0-9]+(\|[^>]+)?>\s*(can\s+you|please|pls)\s*(execute|help|process|check)",
    re.IGNORECASE,
)
_RE_STANDUP_NUDGE = re.compile(
    r"please\s+(join|drop)\s+(standup|updates|the\s+standup)",
    re.IGNORECASE,
)
_RE_ORDER_RESOLVED = re.compile(
    r"Issue\s+(Resolved|Marked\s+as)|Order\s+Numbers:",
    re.IGNORECASE,
)
_RE_DAILY_STATS_REPORT = re.compile(
    r"Daily\s+(Oncall|Slack)\s+Stats\s+Report|Bumped\s+Up",
    re.IGNORECASE,
)

# Body prefixes that indicate a recurring template — leading 40 chars matched
# across ≥ TEMPLATE_PREFIX_THRESHOLD share of members.
_TEMPLATE_PREFIX_HINTS = [
    "release branch for today",
    "today's release branch",
    "*tl;dr - mom",
    "tl;dr - mom",
    "*mom: ",
    "mom: ",
    "daily oncall stats report",
    ":alert1: ",
    "issue resolved:",
    "*inward unreconciled",
    "inward unreconciled",
] + recurring_prefixes()

TEMPLATE_PREFIX_THRESHOLD = 0.80     # ≥80% of members must share prefix
TEMPLATE_REGEX_THRESHOLD  = 0.80     # ≥80% of members must match regex

# Label patterns that signal a thin/insufficient-content cluster
# (set by older labelling passes when no theme could be extracted).
_THIN_LABEL_PATTERNS = re.compile(
    r"^(Insufficient content|Sparse\b|empty extracted bodies|"
    r"no\s+coherent|Thin signal|Untriaged|Unlabel)",
    re.IGNORECASE,
)


def _classify_cluster_by_content(
    cluster_id: int, members: list[tuple[str, str | None, str | None]]
) -> tuple[str | None, dict]:
    """Inspect cluster members. Return (template_label, debug_dict).

    `members`: list of (subject, actor, body) tuples.
    `template_label`: e.g. "Recurring channel-join template" — None if no pattern matches.
    """
    if not members:
        return None, {"reason": "no_members"}

    n = len(members)
    bodies = [(b or "") for _, _, b in members]

    # Regex matchers — count members whose body matches.
    rx_counts = {
        "channel_join": sum(1 for b in bodies if _RE_CHANNEL_JOIN.search(b)),
        "subteam_ping_help": sum(1 for b in bodies if _RE_SUBTEAM_PING_HELP.search(b)),
        "standup_nudge": sum(1 for b in bodies if _RE_STANDUP_NUDGE.search(b)),
        "order_resolved": sum(1 for b in bodies if _RE_ORDER_RESOLVED.search(b)),
        "daily_stats_report": sum(1 for b in bodies if _RE_DAILY_STATS_REPORT.search(b)),
    }

    # Body-prefix matchers
    prefixes_lower = [b[:80].lower().strip() for b in bodies]
    prefix_hits = Counter()
    for p in prefixes_lower:
        for hint in _TEMPLATE_PREFIX_HINTS:
            if p.startswith(hint):
                prefix_hits[hint] += 1
                break

    debug = {
        "n": n,
        "regex_hits": rx_counts,
        "prefix_hits": dict(prefix_hits),
    }

    # Decision rules — only fire on content patterns. Bot-share was dropped
    # because GitHub repos generate legitimate review-bot / merge / commit bot
    # events on real workstreams (telemetry migration, payments subscription, etc.).
    # Stricter regex / prefix matches only.

    # 1. Channel-join dominant
    if rx_counts["channel_join"] / n >= TEMPLATE_REGEX_THRESHOLD:
        return "Recurring channel-membership events (join/leave)", debug
    # 2. CMR-request ping pattern
    if rx_counts["subteam_ping_help"] / n >= TEMPLATE_REGEX_THRESHOLD:
        return "Recurring subteam help/CMR-execute pings", debug
    # 3. Standup nudge
    if rx_counts["standup_nudge"] / n >= TEMPLATE_REGEX_THRESHOLD:
        return "Recurring standup / async-update nudge templates", debug
    # 4. Order-resolution / daily-stats bot
    if (rx_counts["order_resolved"] + rx_counts["daily_stats_report"]) / n >= TEMPLATE_REGEX_THRESHOLD:
        return "Recurring oncall/order-resolution bot notifications", debug
    # 5. Shared prefix template (any single prefix hit ≥ threshold)
    for hint, count in prefix_hits.items():
        if count / n >= TEMPLATE_PREFIX_THRESHOLD:
            return f"Recurring template: {hint.strip().rstrip(':')}", debug

    return None, debug


def _fetch_cluster_members(
    conn: sqlite3.Connection, cluster_id: int, limit: int = 50
) -> list[tuple[str, str | None, str | None]]:
    """Fetch up to `limit` subject / actor / body tuples for a cluster.

    Strategy: pick the latest event per subject so we eyeball the most
    representative body.
    """
    rows = conn.execute(
        """
        SELECT m.subject,
               (SELECT actor FROM events WHERE subject = m.subject
                  ORDER BY ts DESC LIMIT 1) AS actor,
               (SELECT body FROM events WHERE subject = m.subject
                  ORDER BY ts DESC LIMIT 1) AS body
          FROM topic_brief_member m
         WHERE m.cluster_id = ?
         LIMIT ?
        """,
        (cluster_id, limit),
    ).fetchall()
    return list(rows)


def find_label_candidates(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Rows with Recurring label AND null status (the legacy path)."""
    return conn.execute(
        "SELECT cluster_id, label FROM topic_brief "
        "WHERE label LIKE 'Recurring %' AND status IS NULL"
    ).fetchall()


def find_thin_label_candidates(conn: sqlite3.Connection) -> list[tuple[int, str, str | None]]:
    """Rows whose label signals thin / insufficient content. These are
    structurally non-workstreams — stub as RECURRING (template-y) without
    rewriting the label."""
    rows = conn.execute(
        "SELECT cluster_id, label, status FROM topic_brief "
        "WHERE label IS NOT NULL "
        "  AND COALESCE(status,'') NOT IN ('RECURRING')"
    ).fetchall()
    out: list[tuple[int, str, str | None]] = []
    for cid, lbl, st in rows:
        if lbl and _THIN_LABEL_PATTERNS.match(lbl.strip()):
            out.append((cid, lbl, st))
    return out


def find_content_candidates(
    conn: sqlite3.Connection, max_proposals: int | None = None
) -> list[dict]:
    """Walk every ACTIVE/STALE/null-status cluster, return proposed recurring
    rewrites where content patterns match.

    Skips clusters already RECURRING. Skips clusters already labelled Recurring %
    (those go through the legacy path).
    """
    cluster_rows = conn.execute(
        "SELECT cluster_id, label, status FROM topic_brief "
        "WHERE COALESCE(status,'') NOT IN ('RECURRING') "
        "  AND (label IS NULL OR label NOT LIKE 'Recurring %') "
        "ORDER BY cluster_id"
    ).fetchall()

    proposals: list[dict] = []
    for cid, lbl, st in cluster_rows:
        members = _fetch_cluster_members(conn, cid, limit=50)
        new_label, debug = _classify_cluster_by_content(cid, members)
        if not new_label:
            continue
        proposals.append({
            "cluster_id": cid,
            "old_label": (lbl or "")[:70],
            "old_status": st,
            "new_label": new_label,
            "evidence": debug,
        })
        if max_proposals is not None and len(proposals) >= max_proposals:
            break
    return proposals


_STUB_UPDATE_SQL_BY_LABEL = (
    "UPDATE topic_brief "
    "   SET status           = 'RECURRING', "
    "       decisions_json   = COALESCE(decisions_json, '[]'), "
    "       blockers_json    = COALESCE(blockers_json,  '[]'), "
    "       outcomes_json    = COALESCE(outcomes_json,  '[]'), "
    "       followups_json   = COALESCE(followups_json, '[]'), "
    "       risk_areas_json  = COALESCE(risk_areas_json,'[]'), "
    "       first_ts         = COALESCE(first_ts, ?), "
    "       last_activity_ts = COALESCE(last_activity_ts, ?) "
    " WHERE cluster_id = ? AND status IS NULL"
)

_STUB_UPDATE_SQL_BY_CONTENT = (
    "UPDATE topic_brief "
    "   SET label            = ?, "
    "       status           = 'RECURRING', "
    "       decisions_json   = COALESCE(decisions_json, '[]'), "
    "       blockers_json    = COALESCE(blockers_json,  '[]'), "
    "       outcomes_json    = COALESCE(outcomes_json,  '[]'), "
    "       followups_json   = COALESCE(followups_json, '[]'), "
    "       risk_areas_json  = COALESCE(risk_areas_json,'[]'), "
    "       first_ts         = COALESCE(first_ts, ?), "
    "       last_activity_ts = COALESCE(last_activity_ts, ?) "
    " WHERE cluster_id = ?"
)


def _cluster_event_bounds(
    conn: sqlite3.Connection, cluster_id: int
) -> tuple[str | None, str | None]:
    members = [r[0] for r in conn.execute(
        "SELECT subject FROM topic_brief_member WHERE cluster_id = ?",
        (cluster_id,),
    ).fetchall()]
    if not members:
        return None, None
    ph = ",".join("?" * len(members))
    row = conn.execute(
        f"SELECT MIN(ts), MAX(ts) FROM events WHERE subject IN ({ph})",
        members,
    ).fetchone()
    return (row or (None, None))


def stub_label_prefix(conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    """Legacy path — stub clusters whose label already starts with 'Recurring '."""
    cands = find_label_candidates(conn)
    if not cands:
        return {"stubbed": 0, "candidates": []}

    sample = [{"cluster_id": cid, "label": (lbl or "")[:70]} for cid, lbl in cands[:10]]

    if dry_run:
        return {"would_stub": len(cands), "candidates": sample}

    stubbed = 0
    for cid, _ in cands:
        first_ts, last_ts = _cluster_event_bounds(conn, cid)
        conn.execute(_STUB_UPDATE_SQL_BY_LABEL, (first_ts, last_ts, cid))
        stubbed += 1
    conn.commit()
    return {"stubbed": stubbed, "candidates": sample}


def stub_thin_labels(conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    """Stub clusters whose label signals thin / insufficient content
    (older labelling passes produced these when no theme could be extracted).
    Label is preserved as-is — only status + empty arrays are set."""
    cands = find_thin_label_candidates(conn)
    if not cands:
        return {"stubbed": 0, "candidates": []}

    sample = [{"cluster_id": cid, "label": (lbl or "")[:70], "status": st}
              for cid, lbl, st in cands[:10]]

    if dry_run:
        return {"would_stub": len(cands), "candidates": sample}

    stubbed = 0
    for cid, _, _ in cands:
        first_ts, last_ts = _cluster_event_bounds(conn, cid)
        # Use the by-label-stub path (doesn't rewrite label).
        # Allow update even when status is set (we are overwriting STALE→RECURRING).
        conn.execute(
            "UPDATE topic_brief "
            "   SET status           = 'RECURRING', "
            "       decisions_json   = COALESCE(decisions_json, '[]'), "
            "       blockers_json    = COALESCE(blockers_json,  '[]'), "
            "       outcomes_json    = COALESCE(outcomes_json,  '[]'), "
            "       followups_json   = COALESCE(followups_json, '[]'), "
            "       risk_areas_json  = COALESCE(risk_areas_json,'[]'), "
            "       first_ts         = COALESCE(first_ts, ?), "
            "       last_activity_ts = COALESCE(last_activity_ts, ?) "
            " WHERE cluster_id = ?",
            (first_ts, last_ts, cid),
        )
        stubbed += 1
    conn.commit()
    return {"stubbed": stubbed, "candidates": sample}


def stub_content_patterns(
    conn: sqlite3.Connection, dry_run: bool = False, max_proposals: int | None = None
) -> dict:
    """Content-scan path — rewrite label + stub for template-pattern matches."""
    proposals = find_content_candidates(conn, max_proposals=max_proposals)
    sample = proposals[:10]

    if dry_run:
        return {"would_relabel": len(proposals), "proposals": sample,
                "all_proposals_count": len(proposals)}

    stubbed = 0
    for p in proposals:
        cid = p["cluster_id"]
        new_label = p["new_label"]
        first_ts, last_ts = _cluster_event_bounds(conn, cid)
        conn.execute(_STUB_UPDATE_SQL_BY_CONTENT, (new_label, first_ts, last_ts, cid))
        stubbed += 1
    conn.commit()
    return {"stubbed": stubbed, "proposals": sample, "all_proposals_count": len(proposals)}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true", help="report candidates, don't write")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--scan-content", action="store_true",
                    help="also scan member bodies for template patterns (rewrites label)")
    ap.add_argument("--label-only", action="store_true",
                    help="skip the legacy label-prefix path; only run content scan")
    ap.add_argument("--max", type=int, default=None,
                    help="cap content-scan proposals (use with --scan-content for small-batch testing)")
    ap.add_argument("--scan-thin-labels", action="store_true",
                    help="also stub clusters whose label signals thin/insufficient content")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"db missing: {DB_PATH}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(DB_PATH)

    out = {}
    if not args.label_only:
        out["label_prefix"] = stub_label_prefix(conn, dry_run=args.dry_run)
    if args.scan_content:
        out["content_scan"] = stub_content_patterns(
            conn, dry_run=args.dry_run, max_proposals=args.max,
        )
    if args.scan_thin_labels:
        out["thin_labels"] = stub_thin_labels(conn, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        if "label_prefix" in out:
            key = "would_stub" if args.dry_run else "stubbed"
            n = out["label_prefix"].get(key, 0)
            print(f"[label-prefix] {key}={n}")
            for c in out["label_prefix"].get("candidates", []):
                print(f"  cid={c['cluster_id']:4d}  {c['label']}")
        if "content_scan" in out:
            key = "would_relabel" if args.dry_run else "stubbed"
            n = out["content_scan"].get(key, 0) or out["content_scan"].get("all_proposals_count", 0)
            print(f"\n[content-scan] {key}={n}")
            for p in out["content_scan"].get("proposals", []):
                print(f"  cid={p['cluster_id']:4d}")
                print(f"     OLD : {p['old_label']!r:<60}  status={p['old_status']!r}")
                print(f"     NEW : {p['new_label']!r}")
                ev = p.get("evidence", {})
                print(f"     EV  : n={ev.get('n')} regex={ev.get('regex_hits')} prefix={ev.get('prefix_hits')}")
        if "thin_labels" in out:
            key = "would_stub" if args.dry_run else "stubbed"
            n = out["thin_labels"].get(key, 0)
            print(f"\n[thin-labels] {key}={n}")
            for c in out["thin_labels"].get("candidates", []):
                print(f"  cid={c['cluster_id']:4d}  status={c['status']!r}  {c['label']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
person_profile.py — deterministic per-person signals over (person, since, until).

Computes the 14+ contribution / behavioral / throughput / quality signals that
the `/ask person_range` route synthesises into prose. Emits a stable JSON
contract so the chat output shape is identical across runs and people — chat
no longer re-implements the SQL inline (drift + skipped signals under context
pressure).

CLI
---
    .venv/bin/python derive/person_profile.py --name grace --since 2026-03-21 --until 2026-05-21
    .venv/bin/python derive/person_profile.py --name grace --since 2026-03-21 --until 2026-05-21 --format text

JSON contract (schema_version=1) — top-level keys
-------------------------------------------------
    person, tier, window, aliases
    contribution      — work signals (authorship is intentionally last)
    behavioral        — workload / engagement style (window-scoped)
    throughput        — feature_track + ops_track + quality_drift + verdict
    quality           — MatterAI + reverts + bug:feature ratio
    meta              — computed_at, schema_version

Reliability gates (see config/tier_expectations.yaml::reliability_gates) are
applied here — if any gate fails, the verdict is suppressed with the explicit
reason so chat MUST NOT emit a tier_deviation when reading this output.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import yaml

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db, _load_people  # noqa: E402


SCHEMA_VERSION = "3"   # v3 adds `fate` block + companion lookahead reads (sp_completion + domain_ownership)

TIER_EXPECTATIONS_PATH = _PKG_ROOT / "config" / "tier_expectations.yaml"

# Work-window defaults — overridden by config/tier_expectations.yaml::work_hours.
# Loaded once at module import via _load_work_hours() so callers don't pay the
# YAML cost per event.
_WORK_HOURS_CACHE: dict | None = None


def _load_work_hours() -> dict:
    global _WORK_HOURS_CACHE
    if _WORK_HOURS_CACHE is None:
        cfg = yaml.safe_load(TIER_EXPECTATIONS_PATH.read_text()).get("work_hours", {})
        _WORK_HOURS_CACHE = {
            "start_hour": cfg.get("start_hour", 12),
            "end_hour": cfg.get("end_hour", 20),
            "tz": timezone(timedelta(minutes=cfg.get("timezone_offset_minutes", 330))),
        }
    return _WORK_HOURS_CACHE

# Substring used to detect rectifications in title (ops-track quality signal).
RECTIFY_TITLE_RX = re.compile(r"^\s*(fix|rectify|rectification|data\s+correction)\b", re.IGNORECASE)

# MatterAI Code_Quality extraction — bot writes "Code_Quality-NN%" in PR review body.
MATTERAI_QUALITY_RX = re.compile(r"Code[_\s]Quality[\s\-:]*([0-9]{1,3})\s*%", re.IGNORECASE)
MATTERAI_CRITICAL_RX = re.compile(r"(?i)\b(critical\s+issues?\s+found|critical\b.{0,30}\bissues?)\b")

# Assignment changelog title shape: 'assignee: <old> → <new>'.
ASSIGN_RX = re.compile(r"assignee:\s*.*?→\s*(.+?)$")

# Resolution markers — same set as actor_behavior.py.
RESOLUTION_RX = re.compile(
    r"(?i)(?:\bresolved\b|\bfixed\b|\bmerged\b|\bdeployed\b|\brolled out\b|"
    r"\bshipped\b|\blive\b|\bcompleted\b|\bclosed\b|"
    r"✅|:white_check_mark:|:tada:|/pull/|/commit/)"
)


# ── People resolution ────────────────────────────────────────────────────────


def _build_person_alias_map() -> dict[str, dict]:
    """canonical → {role, aliases: [...], alias_lower_set: {...}}."""
    out: dict[str, dict] = {}
    for p in _load_people():
        canon = p.get("canonical")
        if not canon:
            continue
        aliases: list[str] = []
        for key in ("canonical", "github", "email", "jira_id", "slack_id",
                    "slack_handle", "name", "git_name"):
            v = p.get(key)
            if v:
                aliases.append(str(v))
        for gn in (p.get("git_names") or []):
            if gn:
                aliases.append(str(gn))
        out[canon] = {
            "role": p.get("role"),
            "aliases": aliases,
            "alias_lower": {a.lower() for a in aliases},
        }
    return out


def _resolve_canonical(name: str, person_map: dict[str, dict]) -> str | None:
    name_low = name.lower()
    for canon in person_map:
        if name_low in canon.lower():
            return canon
    return None


# ── Time helpers ─────────────────────────────────────────────────────────────


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _is_after_hours(dt: datetime) -> bool:
    """True if event is outside the team work-window.

    Window read from `config/tier_expectations.yaml::work_hours`. After-hours
    = hour < start_hour OR hour >= end_hour, in the configured timezone.
    """
    wh = _load_work_hours()
    h = dt.astimezone(wh["tz"]).hour
    return h < wh["start_hour"] or h >= wh["end_hour"]


def _is_weekend(dt: datetime) -> bool:
    wh = _load_work_hours()
    return dt.astimezone(wh["tz"]).weekday() >= 5  # Sat=5, Sun=6


def _pctl(values, p):
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return float(s[k])


# ── Tier expectations + status taxonomy ──────────────────────────────────────


def _load_tier_expectations() -> dict:
    with open(TIER_EXPECTATIONS_PATH) as f:
        return yaml.safe_load(f)


def _classify_status(status: str | None, classes: dict[str, list[str]]) -> str:
    if not status:
        return "unknown"
    for klass, names in classes.items():
        if status in names:
            return klass
    return "other"


# ── Contribution signals ─────────────────────────────────────────────────────


def _ph(seq) -> str:
    return ",".join("?" * len(seq))


def compute_contribution(
    conn: sqlite3.Connection, aliases: list[str], since: str, until: str
) -> dict:
    ph = _ph(aliases)
    cur = conn.cursor()

    # 1. Authorship — pr_opened + issue_created + thread_started.
    auth = cur.execute(
        f"""SELECT COUNT(*) FROM events
            WHERE actor IN ({ph}) AND ts >= ? AND ts < ?
              AND event_type IN ('pr_opened','issue_created','thread_started')""",
        (*aliases, since, until),
    ).fetchone()[0]

    # 2. Substantive PR commits — commit_in_pr events authored by person.
    pr_commits = cur.execute(
        f"""SELECT COUNT(*) FROM events
            WHERE source='github' AND event_type='commit_in_pr'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?""",
        (*aliases, since, until),
    ).fetchone()[0]

    # 3. Substantive PR reviews — github 'review' events > 200 chars.
    pr_reviews = cur.execute(
        f"""SELECT COUNT(*) FROM events
            WHERE source='github' AND event_type='review'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?
              AND LENGTH(COALESCE(body,'')) > 200""",
        (*aliases, since, until),
    ).fetchone()[0]

    # 3b. Raw PR review count (any length) + distinct PRs reviewed.
    # Distinguishes "many short approves" from "few long reviews" — both
    # land at substantive=0 but mean different things in narrative.
    pr_reviews_total, pr_reviews_distinct = cur.execute(
        f"""SELECT COUNT(*), COUNT(DISTINCT subject) FROM events
            WHERE source='github' AND event_type='review'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?""",
        (*aliases, since, until),
    ).fetchone()

    # 4. Substantive jira comments > 300 chars on tickets they didn't create.
    # Two-step: collect their own-authored subjects to exclude.
    own_jira_subjects = {
        r[0] for r in cur.execute(
            f"""SELECT subject FROM events
                WHERE source='jira' AND event_type='issue_created'
                  AND actor IN ({ph})""",
            aliases,
        ).fetchall()
    }
    jira_comment_rows = cur.execute(
        f"""SELECT subject FROM events
            WHERE source='jira' AND event_type='comment'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?
              AND LENGTH(COALESCE(body,'')) > 300""",
        (*aliases, since, until),
    ).fetchall()
    substantive_jira_comments = sum(1 for (s,) in jira_comment_rows if s not in own_jira_subjects)

    # 4b. Raw jira-comment count (any length) on others' tickets.
    jira_comment_rows_total = cur.execute(
        f"""SELECT subject FROM events
            WHERE source='jira' AND event_type='comment'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?""",
        (*aliases, since, until),
    ).fetchall()
    jira_comments_total = sum(1 for (s,) in jira_comment_rows_total if s not in own_jira_subjects)

    # 5. Jira state transitions triggered by person.
    state_trans = cur.execute(
        f"""SELECT COUNT(*) FROM events
            WHERE source='jira' AND event_type='status_change'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?""",
        (*aliases, since, until),
    ).fetchone()[0]

    # 6. Confluence edits — page_updated count + sum body length as bytes proxy.
    conf_edit_rows = cur.execute(
        f"""SELECT body FROM events
            WHERE source='confluence' AND event_type='page_updated'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?""",
        (*aliases, since, until),
    ).fetchall()
    conf_edits_n = len(conf_edit_rows)
    conf_bytes = sum(len(b or "") for (b,) in conf_edit_rows)

    # 7. Confluence inline comments.
    conf_inline = cur.execute(
        f"""SELECT COUNT(*) FROM events
            WHERE source='confluence' AND event_type='comment'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?""",
        (*aliases, since, until),
    ).fetchone()[0]

    # 8. Substantive slack replies > 200 chars.
    slack_substantive = cur.execute(
        f"""SELECT COUNT(*) FROM events
            WHERE source='slack' AND event_type='thread_reply'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?
              AND LENGTH(COALESCE(body,'')) > 200""",
        (*aliases, since, until),
    ).fetchone()[0]

    # 8b. Raw slack reply count (any length).
    slack_replies_total = cur.execute(
        f"""SELECT COUNT(*) FROM events
            WHERE source='slack' AND event_type='thread_reply'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?""",
        (*aliases, since, until),
    ).fetchone()[0]

    # 9. Coordination spans — own thread_started / issue_created / pr_opened with ≥3 distinct other replyers/commenters.
    own_starts = cur.execute(
        f"""SELECT subject FROM events
            WHERE actor IN ({ph}) AND ts >= ? AND ts < ?
              AND event_type IN ('thread_started','issue_created','pr_opened')""",
        (*aliases, since, until),
    ).fetchall()
    coord_spans = 0
    alias_set = set(aliases)
    for (sub,) in own_starts:
        rows = cur.execute(
            """SELECT DISTINCT actor FROM events
                WHERE subject=? AND event_type IN ('thread_reply','comment','review','commit_in_pr')
                  AND actor IS NOT NULL""",
            (sub,),
        ).fetchall()
        others = {a for (a,) in rows if a not in alias_set}
        if len(others) >= 3:
            coord_spans += 1

    # 10. Cross-surface breadth — count of sources with ≥10 substantive events.
    src_event_counts = dict(cur.execute(
        f"""SELECT source, COUNT(*) FROM events
            WHERE actor IN ({ph}) AND ts >= ? AND ts < ?
              AND (
                (source='github' AND event_type IN ('pr_opened','commit_in_pr','review','comment'))
                OR (source='jira' AND event_type IN ('issue_created','comment','status_change'))
                OR (source='confluence' AND event_type IN ('page_created','page_updated','comment'))
                OR (source='slack' AND event_type IN ('thread_started','thread_reply'))
              )
            GROUP BY source""",
        (*aliases, since, until),
    ).fetchall())
    surfaces = {k: src_event_counts.get(k, 0) for k in ("slack", "jira", "github", "confluence")}
    surfaces_above_thresh = sum(1 for v in surfaces.values() if v >= 10)

    # 11. Active workstreams — distinct clusters touched in window with status='ACTIVE'.
    active_ws = cur.execute(
        f"""SELECT COUNT(DISTINCT m.cluster_id) FROM events e
            JOIN topic_brief_member m ON m.subject = e.subject
            JOIN topic_brief tb ON tb.cluster_id = m.cluster_id
            WHERE e.actor IN ({ph}) AND e.ts >= ? AND e.ts < ?
              AND tb.status = 'ACTIVE'""",
        (*aliases, since, until),
    ).fetchone()[0]

    # 12. Recurring share — % of cluster-attached events that are in RECURRING clusters.
    total_clustered, recurring_clustered = cur.execute(
        f"""SELECT
              COUNT(*),
              SUM(CASE WHEN tb.status='RECURRING' THEN 1 ELSE 0 END)
            FROM events e
            JOIN topic_brief_member m ON m.subject = e.subject
            JOIN topic_brief tb ON tb.cluster_id = m.cluster_id
            WHERE e.actor IN ({ph}) AND e.ts >= ? AND e.ts < ?""",
        (*aliases, since, until),
    ).fetchone()
    recurring_share_pct = (
        round((recurring_clustered or 0) * 100.0 / total_clustered, 1)
        if total_clustered else None
    )

    return {
        "substantive_pr_commits": pr_commits,
        "substantive_pr_reviews": pr_reviews,
        "pr_reviews_total": pr_reviews_total,            # any length
        "pr_reviews_distinct_subjects": pr_reviews_distinct,
        "substantive_jira_comments": substantive_jira_comments,
        "jira_comments_total": jira_comments_total,       # any length
        "jira_state_transitions": state_trans,
        "confluence_edits": {"events": conf_edits_n, "body_bytes": conf_bytes},
        "confluence_inline_comments": conf_inline,
        "substantive_slack_replies": slack_substantive,
        "slack_replies_total": slack_replies_total,       # any length
        "coordination_spans": coord_spans,
        "cross_surface_breadth": {
            **surfaces,
            "sources_above_thresh": surfaces_above_thresh,
            "thresh": 10,
        },
        "active_workstreams": active_ws,
        "recurring_share_pct": recurring_share_pct,
        "clustered_events_total": total_clustered or 0,
        "authorship": auth,  # last, weakest signal
    }


# ── Behavioral signals (window-scoped) ───────────────────────────────────────


def compute_behavioral(
    conn: sqlite3.Connection, aliases: list[str], since: str, until: str
) -> dict:
    cur = conn.cursor()
    ph = _ph(aliases)

    # All events in window for after-hours / weekend share.
    rows = cur.execute(
        f"""SELECT ts FROM events
            WHERE actor IN ({ph}) AND ts >= ? AND ts < ?""",
        (*aliases, since, until),
    ).fetchall()
    after_hours = 0
    weekend = 0
    total = 0
    for (ts,) in rows:
        dt = _parse_iso(ts)
        if not dt:
            continue
        total += 1
        if _is_after_hours(dt):
            after_hours += 1
        if _is_weekend(dt):
            weekend += 1
    after_hours_pct = round(after_hours * 100.0 / total, 1) if total else None
    weekend_pct = round(weekend * 100.0 / total, 1) if total else None

    # Thread-followup rate — own thread_started in window with at least 1 own reply.
    own_threads = cur.execute(
        f"""SELECT subject FROM events
            WHERE source='slack' AND event_type='thread_started'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?""",
        (*aliases, since, until),
    ).fetchall()
    own_thread_n = len(own_threads)
    followed = 0
    if own_thread_n:
        # Batch query for self-replies across all own threads.
        sub_list = [s for (s,) in own_threads]
        sub_ph = _ph(sub_list)
        replied_set = {
            s for (s,) in cur.execute(
                f"""SELECT DISTINCT subject FROM events
                    WHERE source='slack' AND event_type='thread_reply'
                      AND subject IN ({sub_ph})
                      AND actor IN ({ph})""",
                (*sub_list, *aliases),
            ).fetchall()
        }
        followed = len(replied_set)
    thread_followup_pct = round(followed * 100.0 / own_thread_n, 1) if own_thread_n else None

    # Question-vs-answer ratio — own thread_reply ending with '?' vs not.
    q_rows = cur.execute(
        f"""SELECT body FROM events
            WHERE source='slack' AND event_type='thread_reply'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?""",
        (*aliases, since, until),
    ).fetchall()
    q_count = 0
    a_count = 0
    for (b,) in q_rows:
        if not b:
            continue
        body = b.rstrip()
        if body.endswith("?"):
            q_count += 1
        else:
            a_count += 1
    q_a_ratio = f"{q_count}:{a_count}" if (q_count + a_count) else None

    # First-responder / resolver / response-latency — restricted to threads
    # they participated in within window. We mimic actor_behavior.py compute
    # but window-scoped and unconditional on cluster status.
    # Identify threads they touched in window.
    touched_threads = cur.execute(
        f"""SELECT DISTINCT subject FROM events
            WHERE source='slack'
              AND event_type IN ('thread_started','thread_reply')
              AND actor IN ({ph}) AND ts >= ? AND ts < ?
              AND subject IS NOT NULL""",
        (*aliases, since, until),
    ).fetchall()
    threads_touched_n = len(touched_threads)
    first_responder_n = 0
    not_authored_n = 0
    resolver_n = 0
    reply_count_total = 0
    latencies: list[float] = []
    alias_set = set(aliases)

    if touched_threads:
        sub_list = [s for (s,) in touched_threads]
        sub_ph = _ph(sub_list)
        # Pull starter + all replies for these threads (NOT window-scoped — we
        # need the actual thread_started ts even if before `since` to measure
        # latency for in-window replies).
        starter_rows = cur.execute(
            f"""SELECT subject, ts, actor FROM events
                WHERE subject IN ({sub_ph}) AND event_type='thread_started'""",
            sub_list,
        ).fetchall()
        starter_by_sub = {s: (ts, a) for s, ts, a in starter_rows}
        reply_rows = cur.execute(
            f"""SELECT subject, ts, actor, COALESCE(body,'') FROM events
                WHERE subject IN ({sub_ph}) AND event_type='thread_reply'
                ORDER BY subject, ts ASC""",
            sub_list,
        ).fetchall()
        replies_by_sub: dict[str, list] = defaultdict(list)
        for sub, ts, actor, body in reply_rows:
            replies_by_sub[sub].append((ts, actor, body))

        for sub, reps in replies_by_sub.items():
            start = starter_by_sub.get(sub)
            authored_by_them = bool(start and start[1] in alias_set)
            if not authored_by_them:
                not_authored_n += 1
            # First non-author replyer in the entire thread.
            first_replyer = None
            for ts, actor, _b in reps:
                if start and actor == start[1]:
                    continue
                first_replyer = actor
                break
            if (not authored_by_them) and first_replyer in alias_set:
                first_responder_n += 1
            # Their first reply latency (against thread_started ts).
            start_dt = _parse_iso(start[0]) if start else None
            seen_self = False
            for ts, actor, body in reps:
                if actor in alias_set:
                    # Latency: their FIRST reply minus start.
                    if not seen_self and start_dt:
                        rep_dt = _parse_iso(ts)
                        if rep_dt and rep_dt >= start_dt:
                            latencies.append((rep_dt - start_dt).total_seconds())
                        seen_self = True
                    reply_count_total += 1
                    if RESOLUTION_RX.search(body or ""):
                        resolver_n += 1
    first_responder_rate_pct = (
        round(first_responder_n * 100.0 / not_authored_n, 1)
        if not_authored_n else None
    )
    resolver_rate_pct = (
        round(resolver_n * 100.0 / reply_count_total, 1)
        if reply_count_total else None
    )
    p50_min = (_pctl(latencies, 50) / 60.0) if latencies else None
    p90_min = (_pctl(latencies, 90) / 60.0) if latencies else None

    return {
        "first_responder_rate_pct": first_responder_rate_pct,
        "resolver_rate_pct": resolver_rate_pct,
        "p50_response_latency_min": round(p50_min, 1) if p50_min is not None else None,
        "p90_response_latency_min": round(p90_min, 1) if p90_min is not None else None,
        "after_hours_share_pct": after_hours_pct,
        "weekend_share_pct": weekend_pct,
        "thread_followup_rate_pct": thread_followup_pct,
        "question_to_answer_ratio": q_a_ratio,
        "samples": {
            "first_reply_latency_n": len(latencies),
            "thread_started_n": own_thread_n,
            "all_events_n": total,
            "threads_touched_n": threads_touched_n,
            "not_authored_threads_n": not_authored_n,
            "reply_count_total": reply_count_total,
        },
    }


# ── Throughput (feature + ops tracks, verdict + gates) ───────────────────────


def _person_assigned_subjects(
    conn: sqlite3.Connection, aliases: list[str], alias_lower: set[str],
    since: str, until: str
) -> set[str]:
    """Subjects where person was assigned at any point inside window.

    Captures: (a) creation-assigned with creation_ts in window, (b) any
    assignment changelog in window where the title resolves to person.

    Doesn't try to catch pre-window assignment + no reassignment — accepts
    that drift in exchange for predictable, fast SQL.
    """
    cur = conn.cursor()
    ph = _ph(aliases)

    # Creation-assigned in window.
    rows = cur.execute(
        f"""SELECT subject FROM events
            WHERE source='jira' AND event_type='issue_created'
              AND assignee IN ({ph})
              AND ts >= ? AND ts < ?""",
        (*aliases, since, until),
    ).fetchall()
    out = {r[0] for r in rows if r[0]}

    # Assignment changelog in window targeting person.
    rows = cur.execute(
        """SELECT subject, title FROM events
           WHERE source='jira' AND event_type='assignment'
             AND ts >= ? AND ts < ?""",
        (since, until),
    ).fetchall()
    for sub, title in rows:
        m = ASSIGN_RX.search(title or "")
        if not m:
            continue
        new_assignee = m.group(1).strip().lower()
        if new_assignee in alias_lower:
            out.add(sub)
    return out


def _latest_status_per_subject(
    conn: sqlite3.Connection, subjects: list[str], until: str
) -> dict[str, str]:
    """Latest to_status as of `until` for each subject.

    Pulls all to_status-bearing events for these subjects, then per-subject
    picks the row with max ts <= until.
    """
    if not subjects:
        return {}
    out: dict[str, tuple[str, str]] = {}  # subject -> (status, ts)
    sub_ph = _ph(subjects)
    rows = conn.execute(
        f"""SELECT subject, to_status, ts FROM events
            WHERE subject IN ({sub_ph})
              AND to_status IS NOT NULL
              AND ts < ?""",
        (*subjects, until),
    ).fetchall()
    for sub, status, ts in rows:
        prev = out.get(sub)
        if prev is None or ts > prev[1]:
            out[sub] = (status, ts)
    return {sub: st for sub, (st, _ts) in out.items()}


def _issue_meta(conn: sqlite3.Connection, subjects: list[str]) -> dict[str, dict]:
    """Per-subject {issue_type, story_points, sprint_name, sprint_state, title}
    from the issue_created row."""
    if not subjects:
        return {}
    sub_ph = _ph(subjects)
    rows = conn.execute(
        f"""SELECT subject, issue_type, story_points, sprint_name, sprint_state, title, actor
            FROM events
            WHERE subject IN ({sub_ph}) AND event_type='issue_created'""",
        subjects,
    ).fetchall()
    return {
        sub: {
            "issue_type": it or "",
            "story_points": sp,
            "sprint_name": sn or "",
            "sprint_state": ss or "",
            "title": title or "",
            "creator": creator or "",
        }
        for sub, it, sp, sn, ss, title, creator in rows
    }


def _ever_sprinted(conn: sqlite3.Connection, subjects: list[str], meta: dict) -> set[str]:
    """Subjects that were ever placed in a sprint (creation sprint_name set OR
    sprint_change event exists)."""
    if not subjects:
        return set()
    out = {sub for sub, m in meta.items() if m["sprint_name"]}
    sub_ph = _ph(subjects)
    rows = conn.execute(
        f"""SELECT DISTINCT subject FROM events
            WHERE subject IN ({sub_ph}) AND event_type='sprint_change'""",
        subjects,
    ).fetchall()
    out.update(r[0] for r in rows)
    return out


def compute_throughput(
    conn: sqlite3.Connection,
    aliases: list[str],
    alias_lower: set[str],
    canonical: str,
    tier: str | None,
    since: str,
    until: str,
    tier_cfg: dict,
) -> dict:
    classes = tier_cfg.get("status_classes", {})
    gates = tier_cfg.get("reliability_gates", {})
    sprint_cfg = tier_cfg.get("sprint", {})
    tier_tiers = tier_cfg.get("tiers", {})

    subjects = sorted(_person_assigned_subjects(conn, aliases, alias_lower, since, until))
    meta = _issue_meta(conn, subjects)
    latest_status = _latest_status_per_subject(conn, subjects, until)
    sprinted = _ever_sprinted(conn, subjects, meta)

    # ── Bucket per status class ──
    by_class: dict[str, list[str]] = defaultdict(list)
    cmrs: list[str] = []
    non_cmrs: list[str] = []
    for sub in subjects:
        m = meta.get(sub, {})
        it = m.get("issue_type", "")
        status = latest_status.get(sub)
        klass = _classify_status(status, classes)
        by_class[klass].append(sub)
        if it == "CMR":
            cmrs.append(sub)
        else:
            non_cmrs.append(sub)

    # ── Feature track (SP-pointed work) ──
    # Restrict to ever-sprinted, non-CMR, status not unknown.
    feature_subjects = [s for s in subjects if s in sprinted and meta.get(s, {}).get("issue_type") != "CMR"]
    sp_committed = 0.0
    sp_shipped = 0.0
    sp_in_flight = 0.0
    sp_cancelled = 0.0
    sp_eligible_count = 0
    sp_eligible_with_points = 0
    tickets_shipped = 0
    tickets_in_flight = 0
    tickets_cancelled = 0
    for sub in feature_subjects:
        m = meta.get(sub, {})
        status = latest_status.get(sub)
        klass = _classify_status(status, classes)
        sp = m.get("story_points")
        sp_eligible_count += 1
        if sp is not None:
            sp_eligible_with_points += 1
            if klass == "shipped":
                sp_shipped += sp
                sp_committed += sp
            elif klass == "in_flight":
                sp_in_flight += sp
                sp_committed += sp
            elif klass == "cancelled":
                sp_cancelled += sp
                # cancellation excluded from committed-for-completion denom
                # but counted separately for cancellation_rate.
        if klass == "shipped":
            tickets_shipped += 1
        elif klass == "in_flight":
            tickets_in_flight += 1
        elif klass == "cancelled":
            tickets_cancelled += 1

    sp_denom = sp_shipped + sp_in_flight + sp_cancelled
    sp_completion_rate_pct = (
        round(sp_shipped * 100.0 / sp_denom, 1) if sp_denom > 0 else None
    )
    sp_coverage_pct = (
        round(sp_eligible_with_points * 100.0 / sp_eligible_count, 1)
        if sp_eligible_count else None
    )

    # ── Ops track (CMRs) ──
    cmr_authored = sum(
        1 for sub in subjects
        if meta.get(sub, {}).get("issue_type") == "CMR"
        and meta.get(sub, {}).get("creator", "").lower() in alias_lower
    )
    cmr_assigned = len(cmrs)
    cmrs_closed = 0
    for sub in cmrs:
        klass = _classify_status(latest_status.get(sub), classes)
        if klass in ("ops_closed", "shipped"):
            cmrs_closed += 1
    ops_close_rate_pct = (
        round(cmrs_closed * 100.0 / cmr_assigned, 1) if cmr_assigned else None
    )
    rectifications = sum(
        1 for sub, m in meta.items()
        if RECTIFY_TITLE_RX.match(m.get("title", ""))
    )

    # ── Quality drift ──
    total_assigned = len(subjects)
    cancellation_rate_pct = (
        round(tickets_cancelled * 100.0 / total_assigned, 1) if total_assigned else None
    )
    bugs_assigned = sum(
        1 for sub, m in meta.items() if m.get("issue_type") == "Bug"
    )
    # Bugs authored by person in window — issue_created actor in aliases AND type=Bug.
    ph = _ph(aliases)
    bugs_authored = conn.execute(
        f"""SELECT COUNT(*) FROM events
            WHERE source='jira' AND event_type='issue_created'
              AND actor IN ({ph}) AND issue_type='Bug'
              AND ts >= ? AND ts < ?""",
        (*aliases, since, until),
    ).fetchone()[0]

    # ── CMR share for gating ──
    cmr_share = (cmr_assigned / total_assigned) if total_assigned else 0.0

    # ── Reliability gates ──
    flags = {
        "sp_coverage_below_70pct": False,
        "insufficient_sprinted_tickets": False,
        "cmr_heavy_role": False,
        "window_too_short": False,
    }
    suppressed_reason = None

    # Window-vs-sprint-cadence — uses sprint.working_days from yaml as
    # calendar-day proxy (NOT working-day exact; surfaces obviously-short windows).
    sd = _parse_iso(since + "T00:00:00Z" if "T" not in since else since)
    ud = _parse_iso(until + "T00:00:00Z" if "T" not in until else until)
    window_days = (ud - sd).days if (sd and ud) else None
    min_days = sprint_cfg.get("working_days", 10)
    if window_days is not None and window_days < min_days:
        flags["window_too_short"] = True

    min_eligible = gates.get("min_sprinted_tickets_for_verdict", 5)
    sp_cov_min = gates.get("sp_coverage_min", 0.70)
    cmr_thresh = gates.get("cmr_share_threshold", 0.30)

    if sp_eligible_count < min_eligible:
        flags["insufficient_sprinted_tickets"] = True
    elif (sp_coverage_pct or 0.0) / 100.0 < sp_cov_min:
        flags["sp_coverage_below_70pct"] = True

    if cmr_share >= cmr_thresh:
        flags["cmr_heavy_role"] = True

    # ── Feature-track verdict ──
    tier_band = tier_tiers.get(tier or "", {}) if tier else {}
    sp_low = tier_band.get("sp_efficiency_low")
    sp_high = tier_band.get("sp_efficiency_high")
    tier_deviation = None
    if any(flags.values()):
        reasons = [k for k, v in flags.items() if v]
        suppressed_reason = ", ".join(reasons)
    elif sp_low is None or sp_high is None:
        suppressed_reason = f"tier '{tier}' not found in tier_expectations.yaml"
    elif sp_completion_rate_pct is None:
        suppressed_reason = "no SP-pointed feature-track work in window"
    else:
        ratio = sp_completion_rate_pct / 100.0
        if ratio < sp_low:
            tier_deviation = "below-band"
        elif ratio > sp_high:
            tier_deviation = "above-band"
        else:
            tier_deviation = "in-band"

    # ── Ops-track verdict ──
    # Only meaningful when cmr_heavy_role triggered. Compare cmrs_closed
    # against ops_band prorated to window length.
    ops_band = tier_cfg.get("ops_band", {})
    ops_low = ops_band.get("cmrs_closed_per_sprint_low")
    ops_high = ops_band.get("cmrs_closed_per_sprint_high")
    ops_deviation = None
    ops_expected_range = None
    if flags["cmr_heavy_role"] and ops_low is not None and ops_high is not None and window_days:
        sprints_in_window = window_days / max(min_days, 1)
        exp_low = ops_low * sprints_in_window
        exp_high = ops_high * sprints_in_window
        ops_expected_range = [round(exp_low, 1), round(exp_high, 1)]
        if cmrs_closed < exp_low:
            ops_deviation = "below-band"
        elif cmrs_closed > exp_high:
            ops_deviation = "above-band"
        else:
            ops_deviation = "in-band"

    return {
        "feature_track": {
            "story_points_committed": round(sp_committed, 2),
            "story_points_shipped": round(sp_shipped, 2),
            "story_points_in_flight": round(sp_in_flight, 2),
            "story_points_cancelled": round(sp_cancelled, 2),
            "sp_completion_rate_pct": sp_completion_rate_pct,
            "tickets_shipped": tickets_shipped,
            "tickets_in_flight": tickets_in_flight,
            "tickets_cancelled": tickets_cancelled,
            "sprinted_tickets_total": sp_eligible_count,
            "sp_eligible_with_points": sp_eligible_with_points,
            "sp_coverage_pct": sp_coverage_pct,
        },
        "ops_track": {
            "cmr_authored": cmr_authored,
            "cmr_assigned": cmr_assigned,
            "cmrs_closed": cmrs_closed,
            "ops_close_rate_pct": ops_close_rate_pct,
            "rectifications_authored": rectifications,
        },
        "quality_drift": {
            "cancellation_rate_pct": cancellation_rate_pct,
            "tickets_in_flight": tickets_in_flight,
            "bugs_assigned_to_person": bugs_assigned,
            "bugs_authored_by_person": bugs_authored,
        },
        "totals": {
            "assigned_subjects": total_assigned,
            "by_status_class": {k: len(v) for k, v in by_class.items()},
            "cmr_share_pct": round(cmr_share * 100.0, 1),
        },
        "tier_expectations": {
            "tier": tier,
            "sp_efficiency_low": sp_low,
            "sp_efficiency_high": sp_high,
            "ops_band_low": tier_cfg.get("ops_band", {}).get("cmrs_closed_per_sprint_low"),
            "ops_band_high": tier_cfg.get("ops_band", {}).get("cmrs_closed_per_sprint_high"),
        },
        "verdict": {
            "tier_deviation": tier_deviation,
            "ops_track_deviation": ops_deviation,
            "ops_expected_cmrs_in_window": ops_expected_range,
            "reliability_gates": flags,
            "verdict_suppressed_reason": suppressed_reason,
            "window_days": window_days,
            "min_window_days": min_days,
        },
    }


# ── Quality signals (MatterAI + reverts) ─────────────────────────────────────


def compute_quality(
    conn: sqlite3.Connection, aliases: list[str], since: str, until: str
) -> dict:
    cur = conn.cursor()
    ph = _ph(aliases)

    # Person's PRs in window — subjects where they opened.
    pr_rows = cur.execute(
        f"""SELECT subject, title FROM events
            WHERE source='github' AND event_type='pr_opened'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?""",
        (*aliases, since, until),
    ).fetchall()
    pr_subjects = [r[0] for r in pr_rows]
    pr_titles = {sub: title or "" for sub, title in pr_rows}

    quality_pcts: list[int] = []
    critical_flags = 0
    revert_count = sum(1 for t in pr_titles.values() if t.lower().startswith("revert"))

    if pr_subjects:
        sub_ph = _ph(pr_subjects)
        # Pull all bot comments/reviews on these PRs that may contain MatterAI signal.
        rows = cur.execute(
            f"""SELECT body FROM events
                WHERE source='github'
                  AND event_type IN ('review','comment')
                  AND subject IN ({sub_ph})
                  AND body IS NOT NULL""",
            pr_subjects,
        ).fetchall()
        for (body,) in rows:
            m = MATTERAI_QUALITY_RX.search(body)
            if m:
                try:
                    pct = int(m.group(1))
                    if 0 <= pct <= 100:
                        quality_pcts.append(pct)
                except ValueError:
                    pass
            if MATTERAI_CRITICAL_RX.search(body):
                critical_flags += 1

    p50 = statistics.median(quality_pcts) if quality_pcts else None

    return {
        "pr_count_in_window": len(pr_subjects),
        "pr_matterai_quality_p50_pct": p50,
        "pr_matterai_quality_samples_n": len(quality_pcts),
        "pr_matterai_critical_flags": critical_flags,
        "pr_revert_count": revert_count,
    }


# ── Per-ticket velocity (sprint-to-done days vs SP norm) ────────────────────


def compute_velocity(
    conn: sqlite3.Connection, aliases: list[str], alias_lower: set[str],
    since: str, until: str, classes: dict[str, list[str]],
    sp_norm_days: float = 1.0,
) -> dict:
    """Per shipped ticket: actual days from first-sprinted to done vs SP*norm.

    Identifies tickets that took meaningfully longer (or shorter) than their
    story-point estimate suggested. Flags outliers — useful for narrative
    "pace" framing.

    Method:
      1. Get all subjects assigned to person in window.
      2. For each subject with a final shipped/ops_closed status:
         - sprinted_ts = first event where sprint_name became set
                          (issue_created.sprint_name OR earliest sprint_change ts)
         - done_ts = ts of latest to_status in shipped/ops_closed class
         - actual_days = ceil((done_ts - sprinted_ts) / 86400)
         - expected_days = story_points * sp_norm_days
         - ratio = actual / expected
      3. Flag ticket if:
         - ratio > 3.0 → 'slow' (took 3x+ expected)
         - ratio < 0.3 → 'fast' (might be over-pointed)
         - sp is None or 0 → 'unsized' (can't judge)

    Returns:
      {
        "per_ticket": [{subject, title, sp, sprinted_ts, done_ts,
                         actual_days, expected_days, ratio, flag}, ...],
        "median_ratio": float | None,
        "slow_count": int,
        "fast_count": int,
        "sp_norm_days": float,
      }
    """
    assigned = sorted(_person_assigned_subjects(conn, aliases, alias_lower, since, until))
    if not assigned:
        return {"per_ticket": [], "median_ratio": None, "slow_count": 0,
                "fast_count": 0, "sp_norm_days": sp_norm_days}
    meta = _issue_meta(conn, assigned)
    sub_ph = _ph(assigned)

    # Lead time = creation → done. Team workflow flips status from To-Do →
    # In Progress → Done same day at close time (status not tracked
    # incrementally), so in-progress transitions are useless as a "coding
    # start" proxy. Lead time captures backlog + active time end-to-end —
    # imperfect but the only honest signal for this workflow.
    creation_rows = conn.execute(
        f"""SELECT subject, ts FROM events
            WHERE subject IN ({sub_ph}) AND event_type='issue_created'""",
        assigned,
    ).fetchall()
    created_ts = {sub: ts for sub, ts in creation_rows}

    # First shipped/ops_closed transition per subject — captures actual ship
    # event, even when it falls outside `until`.
    status_rows = conn.execute(
        f"""SELECT subject, to_status, ts FROM events
            WHERE subject IN ({sub_ph}) AND to_status IS NOT NULL
            ORDER BY ts""",
        assigned,
    ).fetchall()
    done_ts: dict[str, str] = {}
    for sub, status, ts in status_rows:
        if sub in done_ts:
            continue
        klass = _classify_status(status, classes)
        if klass in ("shipped", "ops_closed"):
            done_ts[sub] = ts

    per_ticket: list[dict] = []
    ratios: list[float] = []
    slow = 0
    fast = 0
    for sub in assigned:
        m = meta.get(sub, {})
        dts = done_ts.get(sub)
        if not dts:
            continue  # never shipped
        s_ts = created_ts.get(sub)
        if not s_ts:
            continue
        sd = _parse_iso(s_ts)
        dd = _parse_iso(dts)
        if not sd or not dd or dd < sd:
            continue
        lead_days = max(1, (dd - sd).days)
        sp = m.get("story_points")
        if not sp or sp < 1:
            # Skip sub-1-SP tickets from ratio compute — too noisy at day
            # granularity. Still emit for visibility, flagged 'unsized'.
            flag = "unsized"
            expected = None
            ratio = None
        else:
            # 1 SP ≈ 1 working day per team config. Allow generous slack
            # since lead time includes backlog. Flag slow only when 5× over
            # SP-estimated working time (i.e. 5 SP ticket > 25 days = slow).
            expected = sp * sp_norm_days
            ratio = lead_days / expected
            if ratio > 5.0:
                flag = "slow"
                slow += 1
            elif ratio < 0.5:
                flag = "fast"
                fast += 1
            else:
                flag = "ok"
            ratios.append(ratio)
        per_ticket.append({
            "subject": sub,
            "title": m.get("title", ""),
            "issue_type": m.get("issue_type", ""),
            "story_points": sp,
            "created_ts": s_ts[:10],
            "done_ts": dts[:10],
            "lead_days": lead_days,
            "expected_days": expected,
            "ratio": round(ratio, 1) if ratio is not None else None,
            "flag": flag,
        })

    median = statistics.median(ratios) if ratios else None
    return {
        "per_ticket": sorted(per_ticket, key=lambda x: -(x["ratio"] or 0)),
        "median_ratio": round(median, 2) if median is not None else None,
        "slow_count": slow,
        "fast_count": fast,
        "sp_norm_days": sp_norm_days,
        "shipped_with_sp_count": len(ratios),
    }


# ── Window-edge fate resolution (per-PR + per-ticket) ───────────────────────


def _load_window_cfg(tier_cfg: dict) -> dict:
    """Read window.lookahead_days / lookbehind_days / fate_max_days."""
    w = tier_cfg.get("window", {}) or {}
    return {
        "lookahead_days": int(w.get("lookahead_days", 30)),
        "lookbehind_days": int(w.get("lookbehind_days", 0)),
        "fate_max_days": int(w.get("fate_max_days", 90)),
    }


def _add_days(iso_ts: str, days: int) -> str:
    """Add days to ISO ts. Accepts date-only or full ts."""
    dt = _parse_iso(iso_ts if "T" in iso_ts else iso_ts + "T00:00:00Z")
    return (dt + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def compute_pr_fate(
    conn: sqlite3.Connection, aliases: list[str], since: str, until: str,
    fate_max_days: int,
) -> list[dict]:
    """Per-PR fate for every PR the person opened in window.

    For each pr_opened in [since, until), look up the earliest terminal event
    (pr_merged or pr_closed) within [opened_ts, opened_ts + fate_max_days].
    Status:
      shipped     — pr_merged seen
      abandoned   — pr_closed seen (without merge)
      in_flight   — no terminal event in fate window
    """
    cur = conn.cursor()
    ph = _ph(aliases)
    pr_rows = cur.execute(
        f"""SELECT subject, title, ts AS opened_ts FROM events
            WHERE source='github' AND event_type='pr_opened'
              AND actor IN ({ph}) AND ts >= ? AND ts < ?
            ORDER BY ts""",
        (*aliases, since, until),
    ).fetchall()
    out: list[dict] = []
    for sub, title, opened_ts in pr_rows:
        fate_end = _add_days(opened_ts, fate_max_days)
        row = cur.execute(
            """SELECT event_type, ts FROM events
                WHERE subject = ? AND ts > ? AND ts <= ?
                  AND event_type IN ('pr_merged','pr_closed')
                ORDER BY ts LIMIT 1""",
            (sub, opened_ts, fate_end),
        ).fetchone()
        if row:
            terminal_type, terminal_ts = row
            status = "shipped" if terminal_type == "pr_merged" else "abandoned"
            opened_dt = _parse_iso(opened_ts)
            term_dt = _parse_iso(terminal_ts)
            days = (term_dt - opened_dt).days if (opened_dt and term_dt) else None
            in_window_terminal = (since <= terminal_ts < until)
        else:
            terminal_type = None
            terminal_ts = None
            status = "in_flight"
            days = None
            in_window_terminal = False
        out.append({
            "subject": sub,
            "title": title,
            "opened_ts": opened_ts[:10],
            "status": status,
            "terminal_event": terminal_type,
            "terminal_ts": terminal_ts[:10] if terminal_ts else None,
            "days_to_terminal": days,
            "terminal_in_window": in_window_terminal,
        })
    return out


def compute_ticket_fate(
    conn: sqlite3.Connection, subjects: list[str],
    since: str, until: str, lookahead_days: int,
    classes: dict[str, list[str]],
) -> dict:
    """Per-ticket fate diff between `until` and `until + lookahead_days`.

    For each subject, compare latest-status-as-of-`until` to latest-status-as-of-
    `until + lookahead_days`. Surfaces tickets that resolved in the lookahead.

    Returns:
        {
          "resolved_in_lookahead": [{subject, status_at_until, status_at_lookahead}, ...],
          "in_flight_at_until_total": int,
          "still_in_flight_at_lookahead": int,
          "shifted_to_shipped": int,
          "shifted_to_cancelled": int,
        }
    """
    if not subjects:
        return {"resolved_in_lookahead": [], "in_flight_at_until_total": 0,
                "still_in_flight_at_lookahead": 0, "shifted_to_shipped": 0,
                "shifted_to_cancelled": 0}
    lookahead_until = _add_days(until, lookahead_days)
    status_at_until = _latest_status_per_subject(conn, subjects, until)
    status_at_lookahead = _latest_status_per_subject(conn, subjects, lookahead_until)
    resolved: list[dict] = []
    in_flight_total = 0
    still_in_flight = 0
    to_shipped = 0
    to_cancelled = 0
    for sub in subjects:
        s_until = status_at_until.get(sub)
        klass_until = _classify_status(s_until, classes)
        if klass_until != "in_flight":
            continue
        in_flight_total += 1
        s_la = status_at_lookahead.get(sub)
        klass_la = _classify_status(s_la, classes)
        if klass_la == "in_flight":
            still_in_flight += 1
            continue
        if klass_la == "shipped":
            to_shipped += 1
        elif klass_la == "cancelled":
            to_cancelled += 1
        resolved.append({
            "subject": sub,
            "status_at_until": s_until,
            "status_at_lookahead": s_la,
            "transitioned_to": klass_la,
        })
    return {
        "resolved_in_lookahead": resolved,
        "in_flight_at_until_total": in_flight_total,
        "still_in_flight_at_lookahead": still_in_flight,
        "shifted_to_shipped": to_shipped,
        "shifted_to_cancelled": to_cancelled,
    }


def compute_lookahead_throughput(
    conn: sqlite3.Connection, aliases: list[str], alias_lower: set[str],
    canonical: str, tier: str | None, since: str, until: str, tier_cfg: dict,
    lookahead_days: int,
) -> dict:
    """Re-run feature_track sp_completion + ops_track on [since, until+lookahead].

    Returns the SAME shape as throughput.feature_track + ops_track but
    extended window. Caller diffs against primary to expose boundary bias.
    """
    extended_until = _add_days(until, lookahead_days)
    full = compute_throughput(
        conn, aliases, alias_lower, canonical, tier, since, extended_until, tier_cfg
    )
    return {
        "window_extended_to": extended_until[:10],
        "feature_track": full["feature_track"],
        "ops_track": full["ops_track"],
        "totals": full["totals"],
        "verdict_at_lookahead": full["verdict"],
    }


def compute_lookahead_ownership(
    conn: sqlite3.Connection, aliases: list[str],
    since: str, until: str, lookahead_days: int,
) -> list[dict]:
    """Re-run jira_metrics.compute_pr_author_ownership on extended window."""
    import jira_metrics as jm
    extended_until = _add_days(until, lookahead_days)
    try:
        return jm.compute_pr_author_ownership(conn, aliases, since, extended_until)
    except sqlite3.OperationalError:
        return []


# ── Narrative signals (jira_metrics integration) ─────────────────────────────


def compute_narrative_signals(
    conn: sqlite3.Connection, canonical: str, aliases: list[str],
    since: str, until: str,
) -> dict:
    """Signals previously owned by /narrative skill.

    Folded here so /ask person_range gets:
      - domain_ownership[]   — PR-author share per project domain (OWNED/DROVE/CONTRIBUTED/JIRA_ONLY)
      - by_sprint[]           — SP + ticket breakdown per sprint
      - team_rank             — rank by sp_attributed across team in this window
      - attribution_chain     — changelog / creation_fallback / unknown
      - ops_tickets[]         — title-regex-matched ops/incident/RCA tickets
      - risk_flagged_prs[]    — subject_summary.risk_flags on their PRs
    """
    import jira_metrics as jm  # local import — avoids top-level dep when unused

    people_lookup = jm.load_people_lookup()
    out: dict = {
        "domain_ownership": [],
        "by_sprint": [],
        "team_rank": None,
        "team_median_sp": None,
        "team_sp_count": None,
        "attribution_chain": {},
        "ops_tickets": [],
        "risk_flagged_prs": [],
    }

    # --- 1. PR-author domain ownership labels ---
    try:
        ownership = jm.compute_pr_author_ownership(conn, aliases, since, until)
        out["domain_ownership"] = ownership
    except sqlite3.OperationalError:
        # subject_summary may not exist on a freshly bootstrapped DB.
        out["domain_ownership"] = []

    # --- 2. Window-scoped done-credits → sprint breakdown + attribution chain ---
    credits = jm.compute_done_credits(conn, since, until, people_lookup)
    person_credits = jm.filter_credits_for(credits, canonical)
    by_sprint = jm.aggregate_velocity_by_sprint(person_credits)
    out["by_sprint"] = [
        {"sprint_name": k, "sp": v["sp"], "tickets": v["tickets"], "state": v["state"]}
        for k, v in sorted(by_sprint.items(), key=lambda kv: kv[0])
    ]
    out["attribution_chain"] = jm.attribution_source_summary(person_credits)

    # --- 3. Team velocity baseline → rank ---
    team_velocity = jm.aggregate_velocity_by_actor(credits)
    sp_list = sorted(((c, d["sp"]) for c, d in team_velocity.items()), key=lambda kv: -kv[1])
    out["team_sp_count"] = len(sp_list)
    if sp_list:
        rank = next((i + 1 for i, (c, _sp) in enumerate(sp_list) if c == canonical), None)
        out["team_rank"] = rank
        sps = [sp for _c, sp in sp_list]
        out["team_median_sp"] = round(statistics.median(sps), 2) if sps else None
        out["team_top_sp"] = round(sps[0], 2) if sps else None
        own = team_velocity.get(canonical, {}).get("sp", 0.0)
        out["sp_attributed"] = round(own, 2)
        out["tickets_attributed"] = team_velocity.get(canonical, {}).get("tickets", 0)

    # --- 4. Ops/incident-pattern detection ---
    try:
        ops = jm.detect_ops_tickets(conn, aliases, since, until)
        out["ops_tickets"] = [
            {"subject": o.subject, "title": o.title, "event_type": o.event_type,
             "ts": o.ts, "issue_type": o.issue_type, "sprint_name": o.sprint_name,
             "story_points": o.story_points}
            for o in ops
        ]
    except sqlite3.OperationalError:
        out["ops_tickets"] = []

    # --- 5. Risk-flagged PRs ---
    try:
        ph = _ph(aliases)
        rows = conn.execute(
            f"""SELECT ss.subject, ss.summary, ss.risk_flags
                FROM subject_summary ss
                WHERE ss.subject IN (
                    SELECT DISTINCT subject FROM events
                    WHERE source='github' AND event_type='pr_opened'
                      AND actor IN ({ph}) AND ts >= ? AND ts < ?
                )
                AND ss.risk_flags IS NOT NULL
                AND ss.risk_flags != '' AND ss.risk_flags != '[]'""",
            (*aliases, since, until),
        ).fetchall()
        out["risk_flagged_prs"] = [
            {"subject": r[0], "summary": r[1], "risk_flags": r[2]} for r in rows
        ]
    except sqlite3.OperationalError:
        out["risk_flagged_prs"] = []

    return out


# ── Project footprint (cluster_project_map consumer) ─────────────────────────


# Role-priority table — used to pick highest-leverage role + detect drift.
# DECIDER > AUTHOR > RESOLVER > REVIEWER > RESPONDER.
_ROLE_PRIORITY = {"DECIDER": 5, "AUTHOR": 4, "RESOLVER": 3, "REVIEWER": 2, "RESPONDER": 1}

# Event type → window-role inference. Highest-leverage role per cluster wins
# when multiple events of different types land in window.
_EVENT_TYPE_TO_ROLE = {
    "issue_created":   "AUTHOR",
    "pr_opened":       "AUTHOR",
    "thread_started":  "AUTHOR",
    "page_created":    "AUTHOR",
    "pr_merged_by":    "RESOLVER",
    "review":          "REVIEWER",
    "comment":         "RESPONDER",
    "thread_reply":    "RESPONDER",
    "page_updated":    "RESPONDER",
    "commit_in_pr":    "RESPONDER",
}


def _infer_window_role(event_types: set[str], terminal_done: bool) -> str | None:
    """Pick highest-priority role from event types observed in window.

    `terminal_done` upgrades the role to RESOLVER when person triggered a
    status_change to a terminal-done state (closes the cluster's work).
    """
    if not event_types and not terminal_done:
        return None
    role_candidates: set[str] = set()
    if terminal_done:
        role_candidates.add("RESOLVER")
    for et in event_types:
        r = _EVENT_TYPE_TO_ROLE.get(et)
        if r:
            role_candidates.add(r)
    if not role_candidates:
        return None
    return max(role_candidates, key=lambda r: _ROLE_PRIORITY.get(r, 0))


def compute_project_footprint(
    conn: sqlite3.Connection, aliases: list[str], since: str, until: str,
) -> list[dict]:
    """Per-person project footprint via cluster_project_map.

    Bugfix 2026-05-27: previously walked `topic_brief.participants_json`
    (lifetime-scoped) and counted every cluster where person appeared in
    that field. Frank's service-b-refactor count read 11 in April when
    only 3 clusters had April events from him. Fix: restrict cluster set
    to clusters where person has ≥1 event in [since, until), AND derive
    `window_role` from those window events (separate from `lifetime_role`
    which still comes from participants_json).

    For each project_slug, list:
      - clusters[] — cluster_ids the person actively touched IN WINDOW
      - cluster_count, member_count_total (window-active clusters only)
      - top_role_in_project — highest-leverage WINDOW role (not lifetime)

    Each cluster entry carries both `lifetime_role` (from participants_json)
    and `window_role` (from window events). When they differ by ≥2 priority
    levels, `role_drift: true` flags the cluster — engagement shape
    shifted across months.

    Returns: sorted by cluster_count desc, then member_count_total desc.
    """
    if not aliases:
        return []

    ph = _ph(aliases)

    # 1. Window-scope: find ALL clusters where person has ≥1 event in
    #    [since, until). This replaces the lifetime participants_json walk
    #    as the primary cluster-discovery path. Also collect per-cluster
    #    event types + window event count + terminal-done flag for
    #    window_role derivation.
    rows = conn.execute(f"""
        SELECT m.cluster_id,
               e.event_type,
               COUNT(*) AS evt_count,
               SUM(CASE WHEN e.event_type = 'status_change'
                         AND e.body LIKE '%→ Done%' THEN 1 ELSE 0 END) AS done_count
          FROM events e
          JOIN topic_brief_member m ON m.subject = e.subject
         WHERE e.actor IN ({ph})
           AND e.ts >= ? AND e.ts < ?
         GROUP BY m.cluster_id, e.event_type
    """, (*aliases, since, until)).fetchall()
    if not rows:
        return []

    # Aggregate per-cluster signals.
    cluster_window_state: dict[int, dict] = {}
    for cid, et, n, done_n in rows:
        cw = cluster_window_state.setdefault(
            cid, {"event_types": set(), "window_event_count": 0, "terminal_done": False}
        )
        cw["event_types"].add(et)
        cw["window_event_count"] += n
        if done_n and done_n > 0:
            cw["terminal_done"] = True

    cluster_ids_in_window = list(cluster_window_state.keys())

    # 2. Pull topic_brief metadata + lifetime participants_json for these
    #    window-active clusters.
    placeholders = ",".join("?" * len(cluster_ids_in_window))
    tb_rows = conn.execute(f"""
        SELECT cluster_id, label, status, member_count,
               first_ts, last_activity_ts, participants_json
          FROM topic_brief
         WHERE cluster_id IN ({placeholders})
    """, cluster_ids_in_window).fetchall()

    person_clusters: list[dict] = []
    for cid, label, status, mc, ft, lt, pj_raw in tb_rows:
        try:
            pj = json.loads(pj_raw or "[]")
        except (json.JSONDecodeError, TypeError):
            pj = []
        lifetime_match = next(
            (p for p in pj if isinstance(p, dict) and p.get("person") in aliases),
            None,
        )
        lifetime_role = (lifetime_match or {}).get("role")
        lifetime_contrib = (lifetime_match or {}).get("contribution_count", 0)

        cw = cluster_window_state[cid]
        window_role = _infer_window_role(cw["event_types"], cw["terminal_done"])

        # Drift flag: if lifetime + window roles differ by ≥2 priority
        # levels, surface for owner attention. Cluster where person was
        # AUTHOR historically but only RESPONDER this window = drift.
        lt_prio = _ROLE_PRIORITY.get(lifetime_role or "", 0)
        wn_prio = _ROLE_PRIORITY.get(window_role or "", 0)
        role_drift = bool(lifetime_role and window_role and abs(lt_prio - wn_prio) >= 2)

        person_clusters.append({
            "cluster_id": cid,
            "label": label,
            "status": status,
            "member_count": mc or 0,
            "first_ts": ft,
            "last_activity_ts": lt,
            "lifetime_role": lifetime_role,
            "lifetime_contrib_count": lifetime_contrib,
            "window_role": window_role,
            "window_event_count": cw["window_event_count"],
            "role_drift": role_drift,
        })

    if not person_clusters:
        return []

    # 3. Join with cluster_project_map to get slug per cluster.
    cluster_ids = [pc["cluster_id"] for pc in person_clusters]
    placeholders = ",".join("?" * len(cluster_ids))
    try:
        link_rows = conn.execute(f"""
            SELECT cluster_id, project_slug, confidence
              FROM cluster_project_map
             WHERE cluster_id IN ({placeholders})
               AND confidence >= 0.60
        """, cluster_ids).fetchall()
    except sqlite3.OperationalError:
        return []

    # 4. Aggregate by slug. top_role_in_project derived from WINDOW role,
    #    not lifetime — fixes the false-narrative bug.
    by_slug: dict[str, dict] = {}
    cluster_by_id = {pc["cluster_id"]: pc for pc in person_clusters}
    for cid, slug, conf in link_rows:
        pc = cluster_by_id.get(cid)
        if not pc:
            continue
        a = by_slug.setdefault(slug, {
            "project_slug": slug,
            "clusters": [],
            "cluster_count": 0,
            "member_count_total": 0,
            "window_event_count_total": 0,
            "top_role_in_project": None,
            "role_drift_cluster_count": 0,
        })
        a["clusters"].append({
            "cluster_id": cid,
            "label": pc["label"],
            "status": pc["status"],
            "lifetime_role": pc["lifetime_role"],
            "window_role": pc["window_role"],
            "window_event_count": pc["window_event_count"],
            "members": pc["member_count"],
            "link_confidence": conf,
            "role_drift": pc["role_drift"],
        })
        a["cluster_count"] += 1
        a["member_count_total"] += pc["member_count"]
        a["window_event_count_total"] += pc["window_event_count"]
        if pc["role_drift"]:
            a["role_drift_cluster_count"] += 1
        # Top role across slug — pick highest-priority WINDOW role.
        cur_prio = _ROLE_PRIORITY.get(a["top_role_in_project"] or "", 0)
        new_prio = _ROLE_PRIORITY.get(pc["window_role"] or "", 0)
        if new_prio > cur_prio:
            a["top_role_in_project"] = pc["window_role"]

    out = list(by_slug.values())
    # Sort clusters within each slug by window_event_count desc (window
    # activity is the honest scope-of-engagement signal).
    for a in out:
        a["clusters"].sort(key=lambda c: -c["window_event_count"])
    out.sort(key=lambda a: (-a["cluster_count"], -a["member_count_total"]))
    return out


# ── Top-level compute ────────────────────────────────────────────────────────


def compute_profile(
    name: str, since: str, until: str
) -> dict:
    """Compute full profile for person, window.

    Times accepted as either YYYY-MM-DD or full ISO. Stored in output as given.
    """
    person_map = _build_person_alias_map()
    canon = _resolve_canonical(name, person_map)
    if not canon:
        return {
            "error": f"could not resolve canonical for {name!r}",
            "available": sorted(person_map.keys()),
        }
    person = person_map[canon]
    aliases = person["aliases"]
    alias_lower = person["alias_lower"]
    tier = person["role"]
    tier_cfg = _load_tier_expectations()

    conn = get_db()

    contribution = compute_contribution(conn, aliases, since, until)
    behavioral = compute_behavioral(conn, aliases, since, until)
    throughput = compute_throughput(
        conn, aliases, alias_lower, canon, tier, since, until, tier_cfg
    )
    quality = compute_quality(conn, aliases, since, until)
    narrative = compute_narrative_signals(conn, canon, aliases, since, until)
    project_footprint = compute_project_footprint(conn, aliases, since, until)

    # ── Per-ticket velocity / pace ──
    velocity = compute_velocity(
        conn, aliases, alias_lower, since, until,
        tier_cfg.get("status_classes", {}),
        sp_norm_days=tier_cfg.get("sprint", {}).get("default_sp_per_sprint", 10)
        and 1.0,  # 1 SP ≈ 1 day per tier_expectations.yaml::sprint comment
    )

    # ── Window-edge fate + lookahead companion reads (v3) ──
    window_cfg = _load_window_cfg(tier_cfg)
    pr_fate = compute_pr_fate(
        conn, aliases, since, until, window_cfg["fate_max_days"]
    )
    # Reconstruct assigned subjects for ticket-fate (cheap re-query).
    assigned = sorted(_person_assigned_subjects(conn, aliases, alias_lower, since, until))
    ticket_fate = compute_ticket_fate(
        conn, assigned, since, until,
        window_cfg["lookahead_days"], tier_cfg.get("status_classes", {}),
    )
    lookahead_throughput = compute_lookahead_throughput(
        conn, aliases, alias_lower, canon, tier, since, until, tier_cfg,
        window_cfg["lookahead_days"],
    )
    lookahead_ownership = compute_lookahead_ownership(
        conn, aliases, since, until, window_cfg["lookahead_days"]
    )

    shipped_days = [p["days_to_terminal"] for p in pr_fate
                    if p["status"] == "shipped" and p["days_to_terminal"] is not None]
    pr_cycle_median = round(statistics.median(shipped_days), 1) if shipped_days else None
    slow_prs = sum(1 for d in shipped_days if d > 14)
    same_day_prs = sum(1 for d in shipped_days if d <= 1)

    fate = {
        "config": window_cfg,
        "velocity": velocity,    # ticket-level (often noisy — see below)
        "pr_fate": pr_fate,
        "pr_fate_summary": {
            "shipped": sum(1 for p in pr_fate if p["status"] == "shipped"),
            "abandoned": sum(1 for p in pr_fate if p["status"] == "abandoned"),
            "in_flight": sum(1 for p in pr_fate if p["status"] == "in_flight"),
            "shipped_in_window": sum(1 for p in pr_fate if p["status"] == "shipped" and p["terminal_in_window"]),
            "shipped_in_lookahead": sum(1 for p in pr_fate if p["status"] == "shipped" and not p["terminal_in_window"]),
            # PR cycle aggregates — opened-to-merged days. UNLIKE ticket
            # lead time (where the team records ticket+done same day), PR
            # opened/merged timestamps are real events. Trustworthy pace
            # signal.
            "pr_cycle_median_days": pr_cycle_median,
            "slow_pr_count_over_14d": slow_prs,
            "same_day_pr_count": same_day_prs,
        },
        "ticket_fate": ticket_fate,
        "lookahead_throughput": lookahead_throughput,
        "lookahead_domain_ownership": lookahead_ownership,
    }

    return {
        "person": canon,
        "tier": tier,
        "window": {"since": since, "until": until},
        "aliases": aliases,
        "contribution": contribution,
        "behavioral": behavioral,
        "throughput": throughput,
        "quality": quality,
        "narrative": narrative,
        "project_footprint": project_footprint,
        "fate": fate,
        "meta": {
            "computed_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "schema_version": SCHEMA_VERSION,
        },
    }


# ── Text formatter (for quick CLI smoke tests; chat consumes JSON) ───────────


def format_text(p: dict) -> str:
    if "error" in p:
        return f"ERROR: {p['error']}\nAvailable: {', '.join(p['available'])}"
    lines = []
    lines.append(f"=== {p['person']} ({p['tier'] or 'NO_TIER'}) ===")
    lines.append(f"window: {p['window']['since']} → {p['window']['until']}")
    lines.append("")
    c = p["contribution"]
    lines.append("CONTRIBUTION")
    lines.append(f"  authorship                  {c['authorship']}")
    lines.append(f"  substantive_pr_commits      {c['substantive_pr_commits']}")
    lines.append(f"  substantive_pr_reviews      {c['substantive_pr_reviews']}   "
                 f"(any-length={c['pr_reviews_total']} across {c['pr_reviews_distinct_subjects']} PRs)")
    lines.append(f"  substantive_jira_comments   {c['substantive_jira_comments']}   "
                 f"(any-length={c['jira_comments_total']})")
    lines.append(f"  jira_state_transitions      {c['jira_state_transitions']}")
    ce = c["confluence_edits"]
    lines.append(f"  confluence_edits            {ce['events']} events / {ce['body_bytes']} bytes")
    lines.append(f"  confluence_inline_comments  {c['confluence_inline_comments']}")
    lines.append(f"  substantive_slack_replies   {c['substantive_slack_replies']}   "
                 f"(any-length={c['slack_replies_total']})")
    lines.append(f"  coordination_spans          {c['coordination_spans']}")
    csb = c["cross_surface_breadth"]
    lines.append(f"  cross_surface_breadth       {csb['sources_above_thresh']}/4  "
                 f"[slack={csb['slack']} jira={csb['jira']} github={csb['github']} conf={csb['confluence']}]")
    lines.append(f"  active_workstreams          {c['active_workstreams']}")
    lines.append(f"  recurring_share_pct         {c['recurring_share_pct']}%")
    lines.append("")
    b = p["behavioral"]
    lines.append("BEHAVIORAL (window-scoped)")
    lines.append(f"  first_responder_rate_pct    {b['first_responder_rate_pct']}")
    lines.append(f"  resolver_rate_pct           {b['resolver_rate_pct']}")
    lines.append(f"  p50_response_latency_min    {b['p50_response_latency_min']}")
    lines.append(f"  p90_response_latency_min    {b['p90_response_latency_min']}")
    lines.append(f"  after_hours_share_pct       {b['after_hours_share_pct']}")
    lines.append(f"  weekend_share_pct           {b['weekend_share_pct']}")
    lines.append(f"  thread_followup_rate_pct    {b['thread_followup_rate_pct']}")
    lines.append(f"  question_to_answer_ratio    {b['question_to_answer_ratio']}")
    lines.append("")
    t = p["throughput"]
    ft = t["feature_track"]
    lines.append("THROUGHPUT — feature_track")
    lines.append(f"  story_points_shipped        {ft['story_points_shipped']}")
    lines.append(f"  story_points_in_flight      {ft['story_points_in_flight']}")
    lines.append(f"  story_points_cancelled      {ft['story_points_cancelled']}")
    lines.append(f"  sp_completion_rate_pct      {ft['sp_completion_rate_pct']}")
    lines.append(f"  tickets shipped/in_flight/cancelled  "
                 f"{ft['tickets_shipped']}/{ft['tickets_in_flight']}/{ft['tickets_cancelled']}")
    lines.append(f"  sprinted_tickets_total      {ft['sprinted_tickets_total']}")
    lines.append(f"  sp_coverage_pct             {ft['sp_coverage_pct']} ({ft['sp_eligible_with_points']}/{ft['sprinted_tickets_total']})")
    ot = t["ops_track"]
    lines.append("THROUGHPUT — ops_track")
    lines.append(f"  cmr_authored / assigned     {ot['cmr_authored']} / {ot['cmr_assigned']}")
    lines.append(f"  cmrs_closed                 {ot['cmrs_closed']}")
    lines.append(f"  ops_close_rate_pct          {ot['ops_close_rate_pct']}")
    lines.append(f"  rectifications_authored     {ot['rectifications_authored']}")
    qd = t["quality_drift"]
    lines.append("THROUGHPUT — quality_drift")
    lines.append(f"  cancellation_rate_pct       {qd['cancellation_rate_pct']}")
    lines.append(f"  bugs assigned/authored      {qd['bugs_assigned_to_person']} / {qd['bugs_authored_by_person']}")
    tot = t["totals"]
    lines.append(f"  total assigned / cmr_share  {tot['assigned_subjects']} / {tot['cmr_share_pct']}%")
    v = t["verdict"]
    lines.append("VERDICT")
    lines.append(f"  tier_deviation              {v['tier_deviation']}")
    lines.append(f"  ops_track_deviation         {v['ops_track_deviation']} "
                 f"(expected {v['ops_expected_cmrs_in_window']} cmrs closed)")
    lines.append(f"  suppressed_reason           {v['verdict_suppressed_reason']}")
    flag_str = ", ".join(k for k, val in v["reliability_gates"].items() if val) or "none"
    lines.append(f"  reliability_gates failed    {flag_str}")
    lines.append("")
    q = p["quality"]
    lines.append("QUALITY")
    lines.append(f"  pr_count_in_window          {q['pr_count_in_window']}")
    lines.append(f"  matterai_quality_p50_pct    {q['pr_matterai_quality_p50_pct']} "
                 f"(n={q['pr_matterai_quality_samples_n']})")
    lines.append(f"  matterai_critical_flags     {q['pr_matterai_critical_flags']}")
    lines.append(f"  pr_revert_count             {q['pr_revert_count']}")
    lines.append("")
    n = p.get("narrative", {})
    lines.append("NARRATIVE (jira_metrics integration)")
    lines.append(f"  team_rank                   {n.get('team_rank')}/{n.get('team_sp_count')}  "
                 f"(sp_attributed={n.get('sp_attributed')}, team_median={n.get('team_median_sp')}, top={n.get('team_top_sp')})")
    lines.append(f"  attribution_chain           {n.get('attribution_chain')}")
    lines.append(f"  by_sprint                   {len(n.get('by_sprint', []))} sprints")
    for s in n.get("by_sprint", [])[:5]:
        lines.append(f"     {s['sprint_name']:30s} sp={s['sp']:>5} tickets={s['tickets']} state={s['state']}")
    lines.append(f"  domain_ownership            {len(n.get('domain_ownership', []))} domains")
    for d in n.get("domain_ownership", [])[:6]:
        lines.append(f"     {d.get('domain','?'):20s} [{d.get('label','?'):11s}] {d.get('share_pct')}% ({d.get('person_authored_merged')}/{d.get('team_merged')} PRs)")
    lines.append(f"  ops_tickets                 {len(n.get('ops_tickets', []))} ops/incident matches")
    for o in n.get("ops_tickets", [])[:4]:
        lines.append(f"     {o['subject']:14s} [{o.get('issue_type') or '?':5s}]  {(o['title'] or '')[:70]}")
    lines.append(f"  risk_flagged_prs            {len(n.get('risk_flagged_prs', []))}")
    lines.append("")
    f = p.get("fate", {})
    pfs = f.get("pr_fate_summary", {})
    tf = f.get("ticket_fate", {})
    lt = f.get("lookahead_throughput", {})
    cfg = f.get("config", {})
    lines.append(f"FATE (window + lookahead {cfg.get('lookahead_days')}d, fate_max {cfg.get('fate_max_days')}d)")
    lines.append(f"  PRs opened in window        shipped={pfs.get('shipped',0)} (in-window={pfs.get('shipped_in_window',0)}, in-lookahead={pfs.get('shipped_in_lookahead',0)})  "
                 f"abandoned={pfs.get('abandoned',0)}  in_flight={pfs.get('in_flight',0)}")
    lines.append(f"  PR cycle time               median={pfs.get('pr_cycle_median_days')}d  "
                 f"slow_>14d={pfs.get('slow_pr_count_over_14d',0)}  same_day={pfs.get('same_day_pr_count',0)}")
    for pr in (f.get("pr_fate") or [])[:6]:
        lines.append(f"     {pr['subject']:38s} {pr['status']:10s} opened={pr['opened_ts']} terminal={pr['terminal_ts']} days={pr['days_to_terminal']}")
    lines.append(f"  Tickets in_flight at until  {tf.get('in_flight_at_until_total',0)}  "
                 f"→ resolved-in-lookahead {len(tf.get('resolved_in_lookahead',[]))}  "
                 f"(→shipped={tf.get('shifted_to_shipped',0)}, →cancelled={tf.get('shifted_to_cancelled',0)})")
    lt_ft = lt.get("feature_track", {}) if lt else {}
    primary_sp = p["throughput"]["feature_track"]["sp_completion_rate_pct"]
    la_sp = lt_ft.get("sp_completion_rate_pct")
    lines.append(f"  sp_completion (primary)     {primary_sp}%  →  (lookahead) {la_sp}%  "
                 f"(extended_to={lt.get('window_extended_to')})")
    v = f.get("velocity", {}) or {}
    lines.append(f"  velocity                    median_ratio={v.get('median_ratio')}  "
                 f"slow={v.get('slow_count',0)}  fast={v.get('fast_count',0)}  "
                 f"shipped_w_sp={v.get('shipped_with_sp_count',0)}")
    for t in (v.get("per_ticket") or [])[:6]:
        lines.append(f"     {t['subject']:12s} sp={t['story_points']} lead={t['lead_days']}d exp={t['expected_days']}d ratio={t['ratio']}  [{t['flag']}]  created={t['created_ts']}→done={t['done_ts']}  {(t['title'] or '')[:50]}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="canonical substring (case-insensitive)")
    ap.add_argument("--since", required=True, help="ISO date / datetime — inclusive")
    ap.add_argument("--until", required=True, help="ISO date / datetime — exclusive")
    ap.add_argument("--format", choices=("json", "text"), default="json")
    args = ap.parse_args()
    profile = compute_profile(args.name, args.since, args.until)
    if args.format == "text":
        print(format_text(profile))
    else:
        print(json.dumps(profile, indent=2, default=str))


if __name__ == "__main__":
    main()

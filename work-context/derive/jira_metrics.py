"""Shared Jira-interpretation primitives for derive/* skills.

Single source of truth for ticket-credit attribution, ops-keyword detection,
PR-author ownership, and people resolution. **ALL** Jira-interpretation logic
that any skill needs lives HERE. Skills consume, do not reimplement.

Used by:
- `.claude/commands/narrative.md`
- `.claude/commands/retro.md`
- (future) `/sprint`, `/quarterly-retro`, `/boss-update`, `/dev-review`

Rules baked in (per 2026-05-12 feedback):
- **Dedup**: one credit per ticket (latest Done event), never per-transition.
- **Attribution chain**: assignment-changelog → events.assignee creation-fallback → unknown.
- **Ownership**: PR-author share ONLY. Status→Done transitions are CLERICAL, not ownership.
- **Ops detection**: title-regex against curated pattern list.

Usage from a skill's ctx_execute Python block:

    import sys; sys.path.insert(0, '$HOME/context/work-context')
    from derive.jira_metrics import (
        load_people_lookup, compute_done_credits,
        aggregate_velocity_by_actor, aggregate_velocity_by_sprint,
        compute_pr_author_ownership, detect_ops_tickets,
        get_aliases_for, OPS_PATTERNS,
    )
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
PEOPLE_YAML = ROOT / "config" / "people.yaml"


# ──────────────────────────────────────────────────────────────────────────
# People resolution
# ──────────────────────────────────────────────────────────────────────────

def load_people_lookup() -> dict[str, str]:
    """Reverse lookup: any handle/email/id/name → canonical (lowercased keys).

    Covers: canonical, github, email, jira_id, slack_id, slack_handle, name, git_name.
    """
    with open(PEOPLE_YAML) as f:
        data = yaml.safe_load(f).get("people", [])
    lookup: dict[str, str] = {}
    for p in data:
        canonical = p.get("canonical")
        if not canonical:
            continue
        for field_name in ("canonical", "github", "email", "jira_id", "slack_id", "slack_handle", "name", "git_name"):
            v = p.get(field_name)
            if v:
                lookup[str(v).lower().strip()] = canonical
    return lookup


def get_aliases_for(canonical: str) -> list[str]:
    """Return all alias strings (original-case) for a canonical handle.

    Used by skills that need the IN (...) clause for SQL queries. MUST include
    `slack_id` — slack events key on the U-id, not the slack_handle. Omitting
    it silently drops all slack signal from narrative/retro queries.
    """
    with open(PEOPLE_YAML) as f:
        data = yaml.safe_load(f).get("people", [])
    for p in data:
        if p.get("canonical") == canonical:
            return [str(p[k]) for k in
                    ("github", "canonical", "email", "jira_id", "slack_id", "slack_handle", "name", "git_name")
                    if p.get(k)]
    return []


def all_team_canonicals() -> list[str]:
    """All canonical handles in people.yaml."""
    with open(PEOPLE_YAML) as f:
        data = yaml.safe_load(f).get("people", [])
    return [p["canonical"] for p in data if p.get("canonical")]


# ──────────────────────────────────────────────────────────────────────────
# Ticket credit — assigned-only SP attribution
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class TicketCredit:
    subject: str
    canonical: Optional[str]  # None = unattributable
    story_points: float
    sprint_name: str
    sprint_state: str
    issue_type: str
    done_ts: str
    source: str  # 'changelog' | 'creation_fallback' | 'unknown'


_ASSIGN_RE = re.compile(r"assignee:\s*.*?→\s*(.+?)$")


def compute_done_credits(
    conn: sqlite3.Connection,
    start_ts: str,
    end_ts: str,
    people_lookup: Optional[dict[str, str]] = None,
) -> list[TicketCredit]:
    """Build credit list for all Done events in window.

    **Dedupes by subject** — exactly one credit per ticket (the latest Done event
    on that subject). Same ticket transitioning Done multiple times (e.g. reopen
    + re-close) credits once, not twice. Fixes the EX-2356 double-count bug.

    Resolution chain per ticket:
      1. Changelog replay — latest `assignment` event with `ts <= done_ts`,
         parse `assignee: ... → toString`, match toString → canonical via
         `people_lookup` (case-insensitive).
      2. Creation fallback — `events.assignee` field on the `issue_created`
         row (populated by `ingest/jira.py::normalize_issue_created` + the
         2026-05-12 `backfill-jira-assignees.py` script).
      3. Unknown — both above yielded nothing; `canonical=None`, `source='unknown'`.

    Returns flat list; callers aggregate. SP from `events.story_points` (0 if NULL).
    """
    if people_lookup is None:
        people_lookup = load_people_lookup()
    cur = conn.cursor()

    # Dedup: latest Done timestamp per subject.
    cur.execute("""
        SELECT subject, MAX(ts) FROM events
        WHERE source='jira' AND event_type='status_change'
          AND title LIKE '%→ Done%' AND ts BETWEEN ? AND ?
        GROUP BY subject
    """, (start_ts, end_ts))
    done_rows = cur.fetchall()

    credits: list[TicketCredit] = []
    for subject, done_ts in done_rows:
        canonical: Optional[str] = None
        source = "unknown"

        # Step 1: changelog replay
        cur.execute("""
            SELECT title FROM events
            WHERE source='jira' AND event_type='assignment'
              AND subject = ? AND ts <= ?
            ORDER BY ts DESC LIMIT 1
        """, (subject, done_ts))
        row = cur.fetchone()
        if row:
            m = _ASSIGN_RE.search(row[0] or "")
            if m:
                name = m.group(1).strip().lower()
                canonical = people_lookup.get(name)
                if canonical:
                    source = "changelog"

        # Step 2: creation fallback + fetch metadata in one shot
        cur.execute("""
            SELECT assignee, story_points, sprint_name, sprint_state,
                   COALESCE(issue_type, '')
            FROM events
            WHERE subject = ? AND event_type = 'issue_created'
            LIMIT 1
        """, (subject,))
        ic = cur.fetchone()
        if not canonical and ic and ic[0]:
            canonical = people_lookup.get(str(ic[0]).lower())
            if canonical:
                source = "creation_fallback"

        sp = float(ic[1]) if ic and ic[1] is not None else 0.0
        credits.append(TicketCredit(
            subject=subject,
            canonical=canonical,
            story_points=sp,
            sprint_name=(ic[2] if ic else "") or "",
            sprint_state=(ic[3] if ic else "") or "",
            issue_type=(ic[4] if ic else "") or "",
            done_ts=done_ts,
            source=source,
        ))
    return credits


def filter_credits_for(credits: list[TicketCredit], canonical: str) -> list[TicketCredit]:
    """Slice credits for one person."""
    return [c for c in credits if c.canonical == canonical]


# ──────────────────────────────────────────────────────────────────────────
# Aggregations
# ──────────────────────────────────────────────────────────────────────────

def aggregate_velocity_by_actor(credits: list[TicketCredit]) -> dict[str, dict]:
    """Returns `{canonical: {sp, tickets, by_source: {changelog,creation_fallback}}}`."""
    out: dict[str, dict] = {}
    for c in credits:
        if not c.canonical:
            continue
        a = out.setdefault(c.canonical, {"sp": 0.0, "tickets": 0, "by_source": {}})
        a["sp"] += c.story_points
        a["tickets"] += 1
        a["by_source"][c.source] = a["by_source"].get(c.source, 0) + 1
    for v in out.values():
        v["sp"] = round(v["sp"], 2)
    return out


def aggregate_velocity_by_sprint(credits: list[TicketCredit]) -> dict[str, dict]:
    """Returns `{sprint_name: {sp, tickets, state}}`. Filter credits beforehand if per-person."""
    out: dict[str, dict] = {}
    for c in credits:
        if not c.sprint_name:
            continue
        s = out.setdefault(c.sprint_name, {"sp": 0.0, "tickets": 0, "state": c.sprint_state})
        s["sp"] += c.story_points
        s["tickets"] += 1
    for v in out.values():
        v["sp"] = round(v["sp"], 2)
    return out


def attribution_source_summary(credits: list[TicketCredit]) -> dict[str, int]:
    """Count credits by source: changelog / creation_fallback / unknown."""
    out: dict[str, int] = {"changelog": 0, "creation_fallback": 0, "unknown": 0}
    for c in credits:
        out[c.source] = out.get(c.source, 0) + 1
    return out


# ──────────────────────────────────────────────────────────────────────────
# PR-author ownership (status→Done EXCLUDED)
# ──────────────────────────────────────────────────────────────────────────

def compute_pr_author_ownership(
    conn: sqlite3.Connection,
    aliases: list[str],
    start_ts: str,
    end_ts: str,
    domains_limit: int = 12,
) -> list[dict]:
    """Per-domain ownership by PR-author share only.

    **Status→Done transitions are EXCLUDED** — a dev running standup transitions
    tickets they didn't code. Pure code-author signal.

    Share = (PRs merged on domain-tagged subjects that they OPENED)
          / (all team PRs merged on those subjects)

    Labels:
      ≥40% → OWNED · ≥25% → DROVE · ≥1 PR <25% → CONTRIBUTED · 0 PRs → JIRA_ONLY
    """
    cur = conn.cursor()
    ph = ",".join("?" * len(aliases))

    # Domains the person touched (any event)
    cur.execute(f"""
        WITH ps AS (
          SELECT DISTINCT subject FROM events
          WHERE actor IN ({ph}) AND ts BETWEEN ? AND ? AND subject IS NOT NULL
        )
        SELECT dom.value, COUNT(*) FROM subject_summary ss, json_each(ss.domains) AS dom
        WHERE ss.subject IN (SELECT subject FROM ps)
        GROUP BY dom.value ORDER BY 2 DESC LIMIT ?
    """, (*aliases, start_ts, end_ts, domains_limit))
    domains = [r[0] for r in cur.fetchall()]

    out = []
    for domain in domains:
        cur.execute(f"""
            WITH ds AS (
              SELECT DISTINCT ss.subject FROM subject_summary ss, json_each(ss.domains) AS dom
              WHERE dom.value = ?
            )
            SELECT
              (SELECT COUNT(DISTINCT e.subject) FROM events e
                WHERE e.source='github' AND e.event_type='pr_merged'
                  AND e.subject IN (SELECT subject FROM ds)
                  AND e.subject IN (
                    SELECT subject FROM events
                    WHERE event_type='pr_opened' AND actor IN ({ph})
                  )
                  AND e.ts BETWEEN ? AND ?),
              (SELECT COUNT(DISTINCT subject) FROM events
                WHERE source='github' AND event_type='pr_merged'
                  AND subject IN (SELECT subject FROM ds) AND ts BETWEEN ? AND ?)
        """, (domain, *aliases, start_ts, end_ts, start_ts, end_ts))
        pam, tm = cur.fetchone()
        pam = pam or 0
        tm = tm or 0
        share = round(pam / tm * 100, 1) if tm else 0
        if share >= 40:
            label = "OWNED"
        elif share >= 25:
            label = "DROVE"
        elif pam >= 1:
            label = "CONTRIBUTED"
        else:
            label = "JIRA_ONLY"
        out.append({
            "domain": domain,
            "person_authored_merged": pam,
            "team_merged": tm,
            "share_pct": share,
            "label": label,
        })
    return out


# ──────────────────────────────────────────────────────────────────────────
# Ops-keyword detection
# ──────────────────────────────────────────────────────────────────────────

OPS_PATTERNS = [
    # Incidents / outages
    r'\bp0\b', r'\bp1\b', r'\boutage\b', r'\bincident\b', r'\bsev\d+\b',
    # RCA / postmortem
    r'\brca\b', r'post[- ]?mortem', r'\bicp[- ]?\d+\b',
    # Drills / failover
    r'\bdrill\b', r'\bfailover\b', r'\bfallback\b',
    r'disaster\s+recovery', r'\bdr\s+(drill|test|exercise)\b',
    r'\boncall\b', r'\bon[- ]call\b',
    # Correctness incidents
    r'double[- ]credit', r'double[- ]payout', r'balance[- ]change',
    r'balance\s+mismatch', r'missing\s+event', r'recon\s+(flow|issue|fix)',
    r'bad\s+query\s+plan',
    # Time-sensitive ops
    r'year[- ]?end', r'fiscal\s+year', r'fy[- ]?end',
    r'march\s+31', r'go[- ]live', r'production\s+(issue|bug|hotfix)',
    # Migration ops
    r'consolidate.*db', r'domain\s+migration',
]

_OPS_RE = re.compile("|".join(OPS_PATTERNS), re.IGNORECASE)


@dataclass
class OpsTicket:
    subject: str
    title: str
    event_type: str
    ts: str
    issue_type: str = ""
    sprint_name: str = ""
    story_points: float = 0.0


def detect_ops_tickets(
    conn: sqlite3.Connection,
    aliases: list[str],
    start_ts: str,
    end_ts: str,
) -> list[OpsTicket]:
    """Scan for ops-pattern hits across Jira tickets, Confluence pages,
    GitHub PRs, AND Slack thread parents authored / commented by aliases.

    Slack extension (2026-05-13, Phase E): if `thread_summary` has a row for
    the subject with `ops_pattern_match` set, use that. Otherwise fall back
    to scanning title + body of `thread_started` events. Enriched with
    issue_type + sprint + story_points from issue_created row (jira only;
    blank for non-jira).
    """
    cur = conn.cursor()
    ph = ",".join("?" * len(aliases))
    cur.execute(f"""
        SELECT subject, title, event_type, ts, source, body FROM events
        WHERE actor IN ({ph}) AND ts BETWEEN ? AND ?
          AND event_type IN ('issue_created', 'page_created', 'page_updated',
                             'pr_opened', 'comment', 'thread_started')
        ORDER BY ts
    """, (*aliases, start_ts, end_ts))
    out: list[OpsTicket] = []
    seen_subjects: set[str] = set()
    for sub, title, et, ts, source, body in cur.fetchall():
        # For slack rows, prefer materialised thread_summary signal if present.
        if source == "slack":
            ts_row = cur.execute(
                "SELECT ops_pattern_match FROM thread_summary WHERE subject = ?",
                (sub,),
            ).fetchone()
            if ts_row and ts_row[0]:
                pattern_hit = True  # thread_summary already detected ops_pattern
            else:
                # Slack title is body[:200]; scan both title + body for ops pattern.
                pattern_hit = bool(_OPS_RE.search((title or "") + " " + (body or "")))
        else:
            # Non-slack: title-only scan (existing behaviour).
            pattern_hit = bool(_OPS_RE.search(title or ""))
        if not pattern_hit:
            continue
        if sub in seen_subjects:
            continue
        seen_subjects.add(sub)
        # Jira issue enrichment (no-op for non-jira subjects).
        cur.execute("""
            SELECT issue_type, sprint_name, story_points FROM events
            WHERE subject = ? AND event_type = 'issue_created' LIMIT 1
        """, (sub,))
        r = cur.fetchone()
        out.append(OpsTicket(
            subject=sub,
            title=title or "",
            event_type=et,
            ts=ts,
            issue_type=(r[0] if r else "") or "",
            sprint_name=(r[1] if r else "") or "",
            story_points=(r[2] if r and r[2] is not None else 0.0),
        ))
    return out


# ──────────────────────────────────────────────────────────────────────────
# Convenience helpers
# ──────────────────────────────────────────────────────────────────────────

def team_velocity_baseline(
    conn: sqlite3.Connection,
    start_ts: str,
    end_ts: str,
    people_lookup: Optional[dict[str, str]] = None,
) -> dict[str, dict]:
    """One-shot: deduped assigned-only velocity for every actor in window."""
    credits = compute_done_credits(conn, start_ts, end_ts, people_lookup)
    return aggregate_velocity_by_actor(credits)


def strip_epic_prefix(title: str) -> str:
    """Strip leading `[Epic EX-N]` prefix from Jira ticket titles for rendering."""
    return re.sub(r'^\[Epic [A-Z]+-\d+\]\s*', '', title or "")

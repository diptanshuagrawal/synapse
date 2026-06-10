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
- **Dev-vs-reviewer** (2026-06-10): in-progress assignee = the dev who built it;
  in-review assignee that DIFFERS from the dev = the reviewer; same assignee in
  review = dev moved the board without reassigning (work awaiting a reviewer).
  See `infer_ticket_roles` / `member_review_role`.

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
# Dev-vs-reviewer inference (status × assignee replay)
# ──────────────────────────────────────────────────────────────────────────
#
# Owner rule (2026-06-10): the assignee while a ticket is *In Progress* is the
# person who actually built it (the dev). When the ticket moves to *In Review*
# one of two things happened:
#   - assignee stays the same → the dev flipped the board without reassigning;
#     the work sits in review and a reviewer still has to pick it up / close it.
#   - a new assignee appears → that new person is the reviewer, actively reviewing.
# When the reviewer is done they reassign back to the dev. We never treat the
# in-review assignee as "who did the work" — that's the in-progress assignee.

# Status buckets (lowercased, matched case-insensitively against `to_status`).
IN_PROGRESS_STATES = {
    "in progress", "work in progress", "in development", "in dev", "development",
}
REVIEW_STATES = {
    "in review", "code review", "in code review", "review", "review complete",
}


@dataclass
class TicketRoles:
    """Inferred roles for ONE ticket as of `as_of_ts` (or its full history)."""
    subject: str
    current_status: str               # latest to_status seen (raw)
    dev: Optional[str]                # canonical — effective builder: current holder
                                      # while In Progress, else the In-Progress snapshot
    dev_raw: Optional[str]            # raw display name / email behind `dev`
    current_assignee: Optional[str]   # canonical — who holds the ticket now
    current_assignee_raw: Optional[str]
    reviewer: Optional[str]           # canonical — set ONLY when in review AND assignee≠dev
    state: str                        # in_progress | in_review_active |
                                      # in_review_awaiting_reviewer | other
    note: str                         # human phrasing for a standup/Slack update


def _assignee_as_of(
    cur: sqlite3.Cursor, subject: str, as_of_ts: Optional[str]
) -> Optional[str]:
    """Raw assignee name in effect at `as_of_ts` via changelog replay.

    Latest `assignment` event with `ts <= as_of_ts` (or latest ever when
    `as_of_ts` is None), `toString` parsed from `assignee: ... → NAME`. Falls
    back to the `issue_created.assignee` field when no assignment event applies.
    Returns the raw string (display name or email), NOT a canonical handle.
    """
    if as_of_ts is None:
        cur.execute("""
            SELECT title FROM events
            WHERE source='jira' AND event_type='assignment' AND subject=?
            ORDER BY ts DESC LIMIT 1
        """, (subject,))
    else:
        cur.execute("""
            SELECT title FROM events
            WHERE source='jira' AND event_type='assignment' AND subject=? AND ts<=?
            ORDER BY ts DESC LIMIT 1
        """, (subject, as_of_ts))
    row = cur.fetchone()
    if row:
        m = _ASSIGN_RE.search(row[0] or "")
        if m:
            name = m.group(1).strip()
            # "∅" (or empty) means unassigned — fall through to creation field.
            if name and name != "∅":
                return name
    return _creation_assignee(cur, subject)


def _name_sig(s: Optional[str]) -> frozenset:
    """Token signature for fuzzy same-person matching across representations.

    Drops an email domain, splits on non-alphanumerics, lowercases. So
    "Alice Example", "alice.example@yourorg.com" and the canonical
    "alice-example" all collapse to {alice, example}.
    """
    if not s:
        return frozenset()
    local = s.split("@", 1)[0].lower()
    return frozenset(t for t in re.split(r"[^a-z0-9]+", local) if t)


def _same_person(
    canon_a: Optional[str], raw_a: Optional[str],
    canon_b: Optional[str], raw_b: Optional[str],
) -> bool:
    """True if A and B are the same person.

    Prefers canonical equality (when BOTH resolve). Otherwise compares token
    signatures of the best identity we have on each side. This survives the
    common identity gap where one side is an email and the other a display name,
    and only one of them resolves to a canonical handle in people.yaml.
    """  # e.g. "alice.example@yourorg.com" (resolved) vs "Alice Example" (raw)
    if canon_a and canon_b:
        return canon_a == canon_b
    sig_a = _name_sig(canon_a or raw_a)
    sig_b = _name_sig(canon_b or raw_b)
    return bool(sig_a) and sig_a == sig_b


def _build_sig_index(people_lookup: dict[str, str]) -> dict[frozenset, str]:
    """Token-signature → canonical, built from every people.yaml alias.

    Lets `resolve_canonical` rescue a raw name that isn't a literal alias (the
    changelog display name "Alice Example" when only the email is on file).
    Ambiguous signatures (two canonicals sharing a token set) are dropped — we
    never guess between two people.
    """
    idx: dict[frozenset, str] = {}
    ambiguous: set[frozenset] = set()
    for alias, canonical in people_lookup.items():
        sig = _name_sig(alias)
        if not sig:
            continue
        if sig in idx and idx[sig] != canonical:
            ambiguous.add(sig)
        idx[sig] = canonical
    for sig in ambiguous:
        idx.pop(sig, None)
    return idx


def resolve_canonical(
    raw: Optional[str],
    people_lookup: dict[str, str],
    sig_index: Optional[dict[frozenset, str]] = None,
) -> Optional[str]:
    """Canonical handle for a raw assignee — direct alias first, token-sig fallback.

    The fallback closes the email↔display-name gap: the changelog carries display
    names, `issue_created` carries emails, and people.yaml may know only one form.
    Pass `sig_index` (from `_build_sig_index`) when resolving in a loop to avoid
    rebuilding it per call.
    """
    if not raw:
        return None
    c = people_lookup.get(raw.strip().lower())
    if c:
        return c
    if sig_index is None:
        sig_index = _build_sig_index(people_lookup)
    return sig_index.get(_name_sig(raw))


def _decide_roles(
    subject: str,
    current_status: Optional[str],
    ip_dev: Optional[str], ip_dev_raw: Optional[str],
    current_assignee: Optional[str], current_assignee_raw: Optional[str],
) -> "TicketRoles":
    """Pure role decision shared by `infer_ticket_roles` and `infer_all_ticket_roles`.

    `ip_dev` = assignee snapshotted at the latest In-Progress transition (who built
    it before any reviewer took over). `current_assignee` = who holds it now. The
    EFFECTIVE dev depends on state:
      - in_progress → the current holder is the one actively building it (a ticket
        reassigned while still In Progress belongs to whoever has it now, not the
        original assignee).
      - in_review   → `ip_dev`, the builder before review; a current holder that
        differs is the reviewer; same holder = awaiting a reviewer.
      - other       → the current holder (latest assignee), for To-Do/CMR/Done.
    """
    cur_norm = (current_status or "").strip().lower()
    reviewer: Optional[str] = None
    if cur_norm in IN_PROGRESS_STATES:
        dev, dev_raw = current_assignee, current_assignee_raw
        state = "in_progress"
        note = f"in progress — {dev or dev_raw or 'unassigned'} building it"
    elif cur_norm in REVIEW_STATES:
        dev, dev_raw = ip_dev, ip_dev_raw
        holder_known = bool(current_assignee or current_assignee_raw)
        dev_known = bool(dev or dev_raw)
        if holder_known and dev_known and not _same_person(
            current_assignee, current_assignee_raw, dev, dev_raw
        ):
            reviewer = current_assignee or current_assignee_raw
            state = "in_review_active"
            note = (f"in review — {reviewer} reviewing "
                    f"(dev: {dev or dev_raw or 'unknown'})")
        else:
            state = "in_review_awaiting_reviewer"
            holder = dev or dev_raw or current_assignee or current_assignee_raw or "unassigned"
            note = (f"in review — awaiting reviewer; {holder} still holds it "
                    f"(moved to review without reassigning)")
    else:
        dev, dev_raw = current_assignee, current_assignee_raw
        state = "other"
        note = f"{current_status or 'unknown status'}"
    return TicketRoles(
        subject=subject,
        current_status=current_status or "",
        dev=dev,
        dev_raw=dev_raw,
        current_assignee=current_assignee,
        current_assignee_raw=current_assignee_raw,
        reviewer=reviewer,
        state=state,
        note=note,
    )


def _creation_assignee(cur: sqlite3.Cursor, subject: str) -> Optional[str]:
    cur.execute("""
        SELECT assignee FROM events
        WHERE subject=? AND event_type='issue_created' AND assignee IS NOT NULL AND assignee<>''
        LIMIT 1
    """, (subject,))
    row = cur.fetchone()
    return row[0] if row else None


def infer_ticket_roles(
    conn: sqlite3.Connection,
    subject: str,
    as_of_ts: Optional[str] = None,
    people_lookup: Optional[dict[str, str]] = None,
) -> TicketRoles:
    """Infer dev vs reviewer for one ticket by replaying status × assignee.

    `dev`  = assignee in effect at the LATEST In-Progress transition (`ts<=as_of_ts`),
             so reassignment-to-reviewer never overwrites who built it. When the
             ticket never hit In Progress, falls back to the creation assignee.
    `reviewer` = the current assignee, but ONLY when the ticket is currently in a
             review state AND that assignee differs from `dev`. Same assignee in
             review ⇒ no reviewer yet (dev moved the board without reassigning).

    `state` drives standup phrasing:
      - in_progress                  → dev is actively building it
      - in_review_active             → `reviewer` is reviewing; ball in reviewer's court
      - in_review_awaiting_reviewer  → work in review, `dev` still holds it; needs a reviewer
      - other                        → To-Do / Done / Cancelled / release states
    """
    if people_lookup is None:
        people_lookup = load_people_lookup()
    cur = conn.cursor()
    sig_index = _build_sig_index(people_lookup)

    def canon(raw: Optional[str]) -> Optional[str]:
        return resolve_canonical(raw, people_lookup, sig_index)

    # Current status: latest to_status <= as_of_ts (or ever).
    if as_of_ts is None:
        cur.execute("""
            SELECT to_status FROM events
            WHERE source='jira' AND event_type='status_change' AND subject=? AND to_status IS NOT NULL
            ORDER BY ts DESC LIMIT 1
        """, (subject,))
    else:
        cur.execute("""
            SELECT to_status FROM events
            WHERE source='jira' AND event_type='status_change' AND subject=?
              AND to_status IS NOT NULL AND ts<=?
            ORDER BY ts DESC LIMIT 1
        """, (subject, as_of_ts))
    row = cur.fetchone()
    current_status = (row[0] if row else "") or ""
    if not current_status:
        # No status_change rows — fall back to the issue_created status snapshot
        # (never-transitioned To-Do tickets). Matches infer_all_ticket_roles.
        cur.execute("""
            SELECT to_status FROM events
            WHERE source='jira' AND event_type='issue_created' AND subject=? AND to_status IS NOT NULL
            LIMIT 1
        """, (subject,))
        r2 = cur.fetchone()
        current_status = (r2[0] if r2 else "") or ""

    # dev = assignee at the latest In-Progress transition.
    placeholders = ",".join("?" * len(IN_PROGRESS_STATES))
    params: list = [subject, *IN_PROGRESS_STATES]
    ts_clause = ""
    if as_of_ts is not None:
        ts_clause = " AND ts<=?"
        params.append(as_of_ts)
    cur.execute(f"""
        SELECT ts FROM events
        WHERE source='jira' AND event_type='status_change' AND subject=?
          AND LOWER(to_status) IN ({placeholders}){ts_clause}
        ORDER BY ts DESC LIMIT 1
    """, params)
    ip_row = cur.fetchone()
    if ip_row:
        dev_raw = _assignee_as_of(cur, subject, ip_row[0])
    else:
        dev_raw = _creation_assignee(cur, subject)
    dev = canon(dev_raw)

    current_assignee_raw = _assignee_as_of(cur, subject, as_of_ts)
    current_assignee = canon(current_assignee_raw)

    return _decide_roles(
        subject, current_status,
        dev, dev_raw, current_assignee, current_assignee_raw,
    )


def member_review_role(roles: TicketRoles, member_canonical: str) -> str:
    """How `member_canonical` relates to this ticket, for standup bucketing.

    Returns one of:
      - 'dev_in_progress'      → member is building it (In Progress)
      - 'dev_awaiting_review'  → member's work is in review, no reviewer yet (their court / chase one)
      - 'reviewing'            → member is the active reviewer (ball in their court)
      - 'dev_under_review'     → member built it, someone else is reviewing (waiting on reviewer)
      - 'none'                 → member not a principal on this ticket in this state
    """
    if roles.state == "in_progress" and roles.dev == member_canonical:
        return "dev_in_progress"
    if roles.state == "in_review_awaiting_reviewer" and roles.dev == member_canonical:
        return "dev_awaiting_review"
    if roles.state == "in_review_active":
        if roles.reviewer == member_canonical:
            return "reviewing"
        if roles.dev == member_canonical:
            return "dev_under_review"
    return "none"


def infer_all_ticket_roles(
    conn: sqlite3.Connection,
    people_lookup: Optional[dict[str, str]] = None,
) -> dict[str, TicketRoles]:
    """Dev/reviewer roles for EVERY jira subject in ONE pass → `{subject: TicketRoles}`.

    Same rule as `infer_ticket_roles`, but reconstructs the whole board in a single
    ordered scan instead of N×4 point queries — the shape standup needs, where
    every roster member's board must be classified. Per-subject `infer_ticket_roles`
    stays the right tool for an as-of-timestamp query on one ticket.

    Replays the running assignee from `issue_created.assignee` + `assignment`-event
    titles (`assignee: X → Y`); snapshots the dev at each In-Progress transition;
    `current_status` = the last `status_change`. `issue_created.to_status` is
    deliberately ignored (it mirrors the live status, not a real transition) so this
    matches `infer_ticket_roles`, which only reads `status_change` rows.
    """
    if people_lookup is None:
        people_lookup = load_people_lookup()
    sig_index = _build_sig_index(people_lookup)

    timeline: dict[str, dict] = {}
    for sub, et, assignee, to_status, title in conn.execute(
        "SELECT subject, event_type, assignee, to_status, title FROM events "
        "WHERE source='jira' AND subject IS NOT NULL ORDER BY ts"
    ):
        d = timeline.setdefault(sub, {"creation": None, "running": None, "dev_raw": None, "status": ""})
        if et == "issue_created":
            if assignee:
                d["creation"] = assignee
                if d["running"] is None:
                    d["running"] = assignee
        elif et == "assignment":
            m = _ASSIGN_RE.search(title or "")
            if m:
                name = m.group(1).strip()
                d["running"] = None if name in ("", "∅") else name
        # Status from any to_status-bearing row: the `issue_created` snapshot is the
        # baseline (recovers never-transitioned To-Do tickets), `status_change` rows
        # override it in ts order so the latest transition wins.
        if to_status:
            d["status"] = to_status
            if to_status.strip().lower() in IN_PROGRESS_STATES:
                d["dev_raw"] = d["running"]

    out: dict[str, TicketRoles] = {}
    for sub, d in timeline.items():
        dev_raw = d["dev_raw"] or d["creation"]
        cur_raw = d["running"]
        out[sub] = _decide_roles(
            sub, d["status"],
            resolve_canonical(dev_raw, people_lookup, sig_index), dev_raw,
            resolve_canonical(cur_raw, people_lookup, sig_index), cur_raw,
        )
    return out


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


# ──────────────────────────────────────────────────────────────────────────
# Selftest — `python3 derive/jira_metrics.py`. Synthetic in-memory timelines,
# zero dependency on the live events.db or people.yaml. Plain asserts.
# ──────────────────────────────────────────────────────────────────────────

# People lookup used across role tests. Note `frank`/`grace` have ONLY an email
# alias (no display-name) — they exercise the token-signature fallback. `sam-a`
# and `sam-b` share a token signature → that signature is ambiguous and dropped.
_TEST_PL = {
    "alice@x.com": "alice", "alice example": "alice", "alice": "alice",
    "bob@x.com": "bob", "bob example": "bob", "bob": "bob",
    "carol@x.com": "carol", "carol kay": "carol", "carol": "carol",
    "frank.lee@x.com": "frank",          # email-only → tests sig fallback
    "grace.kim@x.com": "grace",          # email-only → tests sig fallback
    "sam.jones@a.com": "sam-a",          # shares {sam,jones} with…
    "jones.sam@b.com": "sam-b",          # …this → ambiguous, sig dropped
}


def _tc_conn(rows: list[tuple]):
    """Build an in-memory events DB. Each row: (event_type, ts, subject, assignee, to_status)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE events (source TEXT, event_type TEXT, ts TEXT, subject TEXT, "
        "title TEXT, assignee TEXT, to_status TEXT, issue_type TEXT)"
    )
    packed = []
    for et, ts, sub, asg, tost in rows:
        if et == "assignment":          # asg encoded as (from, to) tuple in `asg`
            frm, to = asg
            title = f"assignee: {frm} → {to}"
            asg_col = None
        else:
            title = f"{et} {sub}"
            asg_col = asg
        packed.append(("jira", et, ts, sub, title, asg_col, tost, "Task"))
    conn.executemany(
        "INSERT INTO events (source,event_type,ts,subject,title,assignee,to_status,issue_type) "
        "VALUES (?,?,?,?,?,?,?,?)", packed)
    conn.commit()
    return conn


def _selftest() -> None:
    """Run inline role-inference + resolver tests. Exits 0 on pass, raises on fail."""
    import sys

    # --- resolver: direct alias, token-sig fallback, ambiguity, miss ---
    sig = _build_sig_index(_TEST_PL)
    assert resolve_canonical("alice@x.com", _TEST_PL, sig) == "alice"      # direct
    assert resolve_canonical("Alice Example", _TEST_PL, sig) == "alice"    # direct (name)
    assert resolve_canonical("Frank Lee", _TEST_PL, sig) == "frank"        # sig: email↔display
    assert resolve_canonical("Grace Kim", _TEST_PL, sig) == "grace"        # sig fallback
    assert resolve_canonical("Sam Jones", _TEST_PL, sig) is None           # ambiguous → no guess
    assert resolve_canonical("Nobody Here", _TEST_PL, sig) is None         # unknown
    assert resolve_canonical(None, _TEST_PL, sig) is None
    print("  ✓ resolve_canonical — direct, token-sig, ambiguity, miss")

    # --- _same_person across representations ---
    assert _same_person("alice", "Alice Example", "alice", "alice@x.com")  # canonical match
    assert _same_person(None, "Frank Lee", None, "frank.lee@x.com")        # raw sig match
    assert not _same_person("alice", None, "bob", None)                    # different canon
    assert not _same_person(None, "Alice Example", None, "Bob Example")    # different raw
    print("  ✓ _same_person — canonical + cross-representation")

    # --- role scenarios: each is (subject, [events], expect_state, expect_dev, expect_reviewer) ---
    A, B, C, F, G = "Alice Example", "Bob Example", "Carol Kay", "Frank Lee", "Grace Kim"
    scenarios = [
        # 1. dev moves own work to review, no reassign → awaiting reviewer
        ("S1", [("issue_created", "01", "S1", "alice@x.com", "To Do"),
                ("assignment", "02", "S1", ("∅", A), None),
                ("status_change", "03", "S1", None, "In Progress"),
                ("status_change", "04", "S1", None, "In Review")],
         "in_review_awaiting_reviewer", "alice", None),
        # 2. reassigned to a reviewer while in review → active reviewer
        ("S2", [("issue_created", "01", "S2", "alice@x.com", "To Do"),
                ("status_change", "02", "S2", None, "In Progress"),
                ("status_change", "03", "S2", None, "In Review"),
                ("assignment", "04", "S2", (A, B), None)],
         "in_review_active", "alice", "bob"),
        # 3. reviewer bounces back to dev (In Review → In Progress) → dev's again
        ("S3", [("issue_created", "01", "S3", "alice@x.com", "To Do"),
                ("status_change", "02", "S3", None, "In Progress"),
                ("status_change", "03", "S3", None, "In Review"),
                ("assignment", "04", "S3", (A, B), None),
                ("status_change", "05", "S3", None, "In Progress"),
                ("assignment", "06", "S3", (B, A), None)],
         "in_progress", "alice", None),
        # 4. in-progress mid-flight reassignment → CURRENT holder is the dev
        ("S4", [("issue_created", "01", "S4", "alice@x.com", "To Do"),
                ("status_change", "02", "S4", None, "In Progress"),
                ("assignment", "03", "S4", (A, C), None)],
         "in_progress", "carol", None),
        # 5. never-transitioned To-Do → status recovered from issue_created snapshot
        ("S5", [("issue_created", "01", "S5", "alice@x.com", "To Do")],
         "other", "alice", None),
        # 6. straight to review (never In Progress) → dev = creation assignee, awaiting
        ("S6", [("issue_created", "01", "S6", "alice@x.com", "To Do"),
                ("status_change", "02", "S6", None, "In Review")],
         "in_review_awaiting_reviewer", "alice", None),
        # 7. identity-gap reviewer: dev (email) + reviewer (display) both email-only in lookup
        ("S7", [("issue_created", "01", "S7", "frank.lee@x.com", "To Do"),
                ("status_change", "02", "S7", None, "In Progress"),
                ("status_change", "03", "S7", None, "In Review"),
                ("assignment", "04", "S7", (F, G), None)],
         "in_review_active", "frank", "grace"),
        # 8. same person across representations (email vs display name) → NO false reviewer
        ("S8", [("issue_created", "01", "S8", "frank.lee@x.com", "To Do"),
                ("status_change", "02", "S8", None, "In Progress"),
                ("status_change", "03", "S8", None, "In Review"),
                ("assignment", "04", "S8", ("∅", F), None)],
         "in_review_awaiting_reviewer", "frank", None),
        # 9. status variants: "Work In Progress" → in_progress; "Code Review" → in_review
        ("S9", [("issue_created", "01", "S9", "alice@x.com", "To Do"),
                ("status_change", "02", "S9", None, "Work In Progress")],
         "in_progress", "alice", None),
        ("S9b", [("issue_created", "01", "S9b", "alice@x.com", "To Do"),
                 ("status_change", "02", "S9b", None, "In Progress"),
                 ("status_change", "03", "S9b", None, "Code Review"),
                 ("assignment", "04", "S9b", (A, B), None)],
         "in_review_active", "alice", "bob"),
        # 10. terminal → other
        ("S10", [("issue_created", "01", "S10", "alice@x.com", "To Do"),
                 ("status_change", "02", "S10", None, "In Progress"),
                 ("status_change", "03", "S10", None, "Done")],
         "other", "alice", None),
    ]

    all_rows: list[tuple] = []
    for _, evs, *_ in scenarios:
        all_rows.extend(evs)
    conn = _tc_conn(all_rows)

    for sub, _evs, exp_state, exp_dev, exp_rev in scenarios:
        r = infer_ticket_roles(conn, sub, people_lookup=_TEST_PL)
        assert r.state == exp_state, f"{sub}: state {r.state!r} != {exp_state!r}"
        assert r.dev == exp_dev, f"{sub}: dev {r.dev!r} != {exp_dev!r} (raw={r.dev_raw!r})"
        assert r.reviewer == exp_rev, f"{sub}: reviewer {r.reviewer!r} != {exp_rev!r}"
    print(f"  ✓ infer_ticket_roles — {len(scenarios)} scenarios (dev/reviewer/awaiting/bounce/variants)")

    # --- batch ≡ per-subject on the same DB ---
    allr = infer_all_ticket_roles(conn, _TEST_PL)
    assert set(allr) == {s for s, *_ in scenarios}, "batch subject set mismatch"
    for sub, _evs, *_ in scenarios:
        b, p = allr[sub], infer_ticket_roles(conn, sub, people_lookup=_TEST_PL)
        assert (b.state, b.dev, b.reviewer, b.current_status) == \
               (p.state, p.dev, p.reviewer, p.current_status), f"{sub}: batch != per-subject"
    print(f"  ✓ infer_all_ticket_roles — batch matches per-subject ({len(allr)} subjects)")

    # --- never-transitioned To-Do keeps its status; terminal carries through ---
    assert allr["S5"].current_status == "To Do", "S5 status snapshot lost"
    assert allr["S10"].current_status == "Done"
    print("  ✓ status: To-Do snapshot recovered, terminal carried through")

    # --- member_review_role bucketing ---
    assert member_review_role(allr["S4"], "carol") == "dev_in_progress"
    assert member_review_role(allr["S1"], "alice") == "dev_awaiting_review"
    assert member_review_role(allr["S2"], "bob") == "reviewing"
    assert member_review_role(allr["S2"], "alice") == "dev_under_review"
    assert member_review_role(allr["S2"], "carol") == "none"
    print("  ✓ member_review_role — dev/reviewer bucket mapping")

    print("ALL PASS")
    sys.exit(0)


if __name__ == "__main__":
    _selftest()

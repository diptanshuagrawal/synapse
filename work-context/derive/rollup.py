"""
Nightly rollup — regenerate derived/ markdown views from index/events.db.

Generates:
  derived/people/{handle}.md      per-person 30d profile
  derived/projects/{slug}.md      per-domain rollup (uses projects.yaml)
  derived/weekly/{YYYY-Wnn}.md    team weekly rollup
  derived/alerts.md               stale PRs + anti-patterns

Run: python derive/rollup.py [--days 30] [--week]
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import llm_classifier
import narrative
from llm_classifier import SubjectInput, SubjectVerdict

ROOT = Path(__file__).parent.parent
DB = ROOT / "index" / "events.db"
DERIVED = ROOT / "derived"
PEOPLE_YAML = ROOT / "config" / "people.yaml"
PROJECTS_YAML = ROOT / "config" / "projects.yaml"
LOG_FILE = ROOT / "logs" / "rollup.log"

BOT_LIKE = "%[bot]%"
MATTERAI_SUMMARY_RE = re.compile(r"^🧪 PR Review is completed:\s*(.+?)(?:\n|$)", re.MULTILINE)

import sources_config  # noqa: E402

# Claude Code Review bot (replaced MatterAI ~2026-05). Posts as github-actions[bot],
# identified by a body marker. Summary = the intro prose before the first findings
# section/table. Strip the boilerplate "## Claude Code Review" heading + the marker.
CLAUDE_REVIEW_MARKER = sources_config.claude_review_marker()
_CLAUDE_BOILERPLATE_RE = re.compile(r"^\s*##\s*Claude Code Review\s*$", re.MULTILINE)
_CLAUDE_STOP_RE = re.compile(r"^\s*(?:###\s|\|)", re.MULTILINE)  # first ### section or md table
# Explicit summary section, when the bot uses one (varies run to run).
_CLAUDE_SUMMARY_SEC_RE = re.compile(
    r"^\s*###\s*(?:Summary|What (?:this |the )?PR does|What changed|Overview)\b[^\n]*\n(.*?)(?=^\s*###\s|^\s*\||\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)


def extract_claude_summary(body: str | None) -> str | None:
    """Pull the human-readable summary out of a Claude Code Review comment.

    The bot's layout varies: sometimes an intro paragraph sits directly under the
    PR-title heading, sometimes the prose lives in a ``### Summary`` /
    ``### What this PR does`` section. Capture the PR-title heading as a lead, then
    that section if present, else the intro prose before the first ``###``/table.
    Returns None if the marker is absent."""
    if not body or CLAUDE_REVIEW_MARKER not in body:
        return None
    text = body.replace(CLAUDE_REVIEW_MARKER, "")
    text = _CLAUDE_BOILERPLATE_RE.sub("", text)

    # PR-title heading lead (first "## ..." line), e.g. "Code Review — PR #859".
    lead = ""
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("##"):
            lead = s.lstrip("# ").strip()
            break

    sec = _CLAUDE_SUMMARY_SEC_RE.search(text)
    if sec:
        prose = sec.group(1)
    else:
        m = _CLAUDE_STOP_RE.search(text)
        prose = text[: m.start()] if m else text

    lines = [ln.lstrip("# ").rstrip() for ln in prose.splitlines()]
    prose_clean = "\n".join(ln for ln in lines if ln).strip()
    # De-dup when lead already appears in prose (intro-paragraph layout).
    if lead and prose_clean.startswith(lead):
        summary = prose_clean
    else:
        summary = "\n".join(p for p in (lead, prose_clean) if p).strip()
    return summary or None


# ── helpers ───────────────────────────────────────────────────────────────────

def load_projects() -> list[dict]:
    if not PROJECTS_YAML.exists():
        return []
    return (yaml.safe_load(PROJECTS_YAML.read_text()) or {}).get("projects", [])


def load_people() -> tuple[dict[str, dict], dict[str, str]]:
    """Return (people_by_handle, alias_map).

    people_by_handle: github_handle → person dict
    alias_map:        any actor key (github/email/jira_id/slack_id/git_name) → canonical github handle
    """
    if not PEOPLE_YAML.exists():
        return {}, {}
    data = yaml.safe_load(PEOPLE_YAML.read_text()) or {}
    by_handle: dict[str, dict] = {}
    alias: dict[str, str] = {}
    for p in data.get("people", []):
        gh = p.get("github")
        if not gh:
            continue
        by_handle[gh] = p
        for key in ("github", "email", "jira_id", "slack_id", "git_name"):
            v = p.get(key)
            if v:
                alias[v] = gh
    return by_handle, alias


def person_aliases(person: dict) -> list[str]:
    """All actor keys that resolve to this person across sources."""
    out: list[str] = []
    for key in ("github", "email", "jira_id", "slack_id", "git_name"):
        v = person.get(key)
        if v:
            out.append(v)
    return out


EPIC_PREFIX_RE = re.compile(r"\[Epic ([A-Z]+-\d+)\]")


def detect_domains(text: str, projects: list[dict]) -> list[str]:
    if not text:
        return []
    low = text.lower()
    hits: list[str] = []

    # Tier-0: explicit Jira Epic match (highest signal). Title prefix written
    # by ingest/jira.py looks like "[Epic EX-2233] ...".
    m = EPIC_PREFIX_RE.search(text)
    epic_key = m.group(1) if m else ""

    for proj in projects:
        if epic_key and epic_key in (proj.get("jira_epics") or []):
            hits.append(proj["slug"])
            continue
        for kw in proj.get("keywords", []):
            if kw.lower() in low:
                hits.append(proj["slug"])
                break
    return hits


def extract_matterai_summary(body: str | None) -> str | None:
    if not body:
        return None
    m = MATTERAI_SUMMARY_RE.search(body)
    return m.group(1).strip() if m else None


def _subject_source(subject: str) -> str:
    """Classify a subject string by its prefix shape.

    Return one of: ``confluence``, ``github``, ``jira``, ``slack``, ``unknown``.
    Callers that build classifier inputs filter slack explicitly (slack
    threads aren't classified via the project-slug enum) and treat
    ``unknown`` as a data-integrity error worth logging.
    """
    if not subject:
        return "unknown"
    if subject.startswith("page:"):
        return "confluence"
    if subject.startswith("slack:"):
        return "slack"
    if "#" in subject and "/" in subject.split("#", 1)[0]:
        return "github"
    if re.match(r"^[A-Z]+-\d+$", subject):
        return "jira"
    return "unknown"


def collect_subjects(conn: sqlite3.Connection, since: str,
                     projects: list[dict],
                     team_handles: set[str] | None = None) -> list[SubjectInput]:
    """Build SubjectInput list for every distinct subject in window.

    Source-specific population:
      github     → title/body from latest pr_opened, MatterAI from latest review
      jira       → title/body from latest issue_created, epic_body if [Epic X-N] prefix
      confluence → title/body from latest page_created/page_updated

    team_handles: when provided, only include subjects where at least one team
    member appears as actor (author, reviewer, commenter) in the window.
    """
    cur = conn.cursor()

    # Subjects with team involvement (author/reviewer/commenter).
    team_subjects: set[str] | None = None
    if team_handles:
        ph = ",".join("?" * len(team_handles))
        cur.execute(f"""
            SELECT DISTINCT subject FROM events
            WHERE ts >= ? AND subject IS NOT NULL
              AND actor IN ({ph})
        """, (since, *team_handles))
        team_subjects = {r[0] for r in cur.fetchall()}

    # Latest event per subject (carrying title/body) for canonical PRs/issues/pages.
    # `thread_started` added 2026-05-28: surfaces slack threads to the LLM
    # ownership classifier. Slack threads weren't previously routed through the
    # project-slug pipeline, leaving 66% of topic clusters with no ownership
    # signal because they were slack-dominated.
    cur.execute("""
        SELECT e.subject, e.source, e.event_type, e.title, e.body, e.issue_type,
               e.story_points, e.sprint_id, e.sprint_name, e.sprint_state
        FROM events e
        WHERE e.subject IS NOT NULL AND e.ts >= ?
          AND e.event_type IN ('pr_opened','pr_merged','issue_created',
                               'page_created','page_updated','thread_started')
        ORDER BY e.ts ASC
    """, (since,))
    primary: dict[str, dict] = {}
    for sub, src, et, title, body, issue_type, sp, sid, sname, sstate in cur.fetchall():
        if team_subjects is not None and sub not in team_subjects:
            continue
        # later ts wins (loop sorted ascending) but pr_opened preferred over pr_merged for PR text
        existing = primary.get(sub)
        if existing and existing.get("event_type") == "pr_opened" and et == "pr_merged":
            continue
        primary[sub] = {
            "source": src, "event_type": et,
            "title": title or "", "body": body or "",
            "issue_type": issue_type or "",
            "story_points": sp,
            "sprint_id": sid,
            "sprint_name": sname or "",
            "sprint_state": sstate or "",
        }

    # Bot PR-review summary + severity per github subject.
    # MatterAI (legacy, review event) + Claude Code Review (current, comment event w/ marker).
    # Both coexist in long windows; MatterAI was replaced ~2026-05.
    matterai: dict[str, dict] = {}
    cur.execute("""
        SELECT subject, body FROM events
        WHERE actor LIKE '%matterai%' AND event_type = 'review' AND ts >= ?
    """, (since,))
    for sub, mbody in cur.fetchall():
        s = extract_matterai_summary(mbody)
        sev = severity_count(mbody)
        if sub:
            matterai[sub] = {"summary": s or "", "severity": sev}

    # Claude Code Review comments (github-actions[bot]; identified by body marker).
    cur.execute("""
        SELECT subject, body FROM events
        WHERE event_type = 'comment' AND instr(body, ?) > 0 AND ts >= ?
        ORDER BY ts ASC
    """, (CLAUDE_REVIEW_MARKER, since))
    for sub, cbody in cur.fetchall():
        if not sub:
            continue
        s = extract_claude_summary(cbody)
        sev = severity_count(cbody)
        # Prefer Claude when present (current bot); only fill if no real matterai summary.
        if s and not (matterai.get(sub, {}).get("summary")):
            matterai[sub] = {"summary": s, "severity": sev}

    # Epic body lookup: any subject whose title carries [Epic X-N] → fetch X-N body.
    epic_keys: set[str] = set()
    for sub, meta in primary.items():
        ek = llm_classifier.extract_epic_key(meta["title"])
        if ek:
            epic_keys.add(ek)
    epic_body: dict[str, str] = {}
    if epic_keys:
        ph = ",".join("?" * len(epic_keys))
        cur.execute(f"""
            SELECT subject, title, body FROM events
            WHERE subject IN ({ph}) AND event_type = 'issue_created'
        """, tuple(epic_keys))
        for sub, title, body in cur.fetchall():
            epic_body[sub] = f"{title}\n{body or ''}".strip()

    # Slack-specific enrichment: participants + early reply bodies for ownership
    # context. The classifier reads these from the `body` field of the slack
    # SubjectInput; no schema change to SubjectInput.
    slack_subjects = [s for s, m in primary.items() if _subject_source(s) == "slack"]
    slack_participants: dict[str, list[tuple[str, int]]] = {}
    slack_replies: dict[str, list[tuple[str, str]]] = {}
    if slack_subjects:
        ph = ",".join("?" * len(slack_subjects))
        cur.execute(f"""
            SELECT subject, actor, COUNT(*) AS n FROM events
            WHERE subject IN ({ph}) AND actor IS NOT NULL
            GROUP BY subject, actor ORDER BY n DESC
        """, tuple(slack_subjects))
        for sub, actor, n in cur.fetchall():
            slack_participants.setdefault(sub, []).append((actor, n))
        cur.execute(f"""
            SELECT subject, actor, body, ts FROM events
            WHERE subject IN ({ph}) AND event_type = 'thread_reply'
            ORDER BY subject, ts ASC
        """, tuple(slack_subjects))
        for sub, actor, body, ts in cur.fetchall():
            if len(slack_replies.get(sub, [])) >= 3:
                continue
            slack_replies.setdefault(sub, []).append((actor or "", (body or "")[:300]))

    def _build_slack_body(sub: str, root_actor: str, root_body: str) -> str:
        parts: list[str] = []
        ps = slack_participants.get(sub, [])[:8]
        if ps:
            parts.append("PARTICIPANTS: " + ", ".join(f"{a}({n})" for a, n in ps))
        parts.append(f"ROOT (by {root_actor}):\n{root_body[:600]}")
        for i, (actor, body) in enumerate(slack_replies.get(sub, []), 1):
            parts.append(f"REPLY {i} (by {actor}):\n{body}")
        return "\n\n".join(parts)

    out: list[SubjectInput] = []
    unknown_examples: list[str] = []
    for sub, meta in primary.items():
        src = _subject_source(sub)
        if src == "slack":
            root_actor = ""
            row = conn.execute(
                "SELECT actor FROM events WHERE subject=? AND event_type='thread_started' LIMIT 1",
                (sub,),
            ).fetchone()
            if row:
                root_actor = row[0] or ""
            slack_body = _build_slack_body(sub, root_actor, meta["body"])
            out.append(SubjectInput(
                subject=sub,
                source="slack",
                title=(meta["title"] or "")[:200],
                body=slack_body,
            ))
            continue
        if src == "unknown":
            # Data integrity signal — every primary subject from this DB
            # should fit a known shape. Capture a small sample for the
            # operator without flooding logs on a corrupted batch.
            if len(unknown_examples) < 5:
                unknown_examples.append(sub)
            continue
        title = meta["title"]
        ek = llm_classifier.extract_epic_key(title)
        ai = matterai.get(sub, {})
        out.append(SubjectInput(
            subject=sub,
            source=src,
            title=title,
            body=meta["body"],
            matterai_summary=ai.get("summary", ""),
            matterai_severity=ai.get("severity", {}),
            epic_key=ek,
            epic_body=epic_body.get(ek, ""),
            confluence_body=meta["body"] if src == "confluence" else "",
            issue_type=meta.get("issue_type", "") if src == "jira" else "",
            story_points=meta.get("story_points") if src == "jira" else None,
            sprint_id=meta.get("sprint_id") if src == "jira" else None,
            sprint_name=meta.get("sprint_name", "") if src == "jira" else "",
            sprint_state=meta.get("sprint_state", "") if src == "jira" else "",
        ))
    if unknown_examples:
        # Fail-loud: a malformed subject would otherwise vanish from the
        # classification pipeline with no signal to the operator.
        logging.getLogger("rollup").warning(
            "collect_subjects: %d subject(s) had unrecognized source "
            "(examples: %s) — check ingest schema for drift",
            len(unknown_examples), unknown_examples,
        )
    return out


def severity_count(body: str | None) -> dict[str, int]:
    if not body:
        return {"red": 0, "orange": 0, "yellow": 0}
    return {
        "red":    body.count("🔴"),
        "orange": body.count("🟠"),
        "yellow": body.count("🟡"),
    }


def fmt_iso(ts: str) -> str:
    return ts[:16].replace("T", " ") if ts else ""


def hours_between(t1: str, t2: str) -> float:
    try:
        d1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
        d2 = datetime.fromisoformat(t2.replace("Z", "+00:00"))
        return (d2 - d1).total_seconds() / 3600
    except (ValueError, AttributeError):
        return 0.0


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(int(len(s) * p), len(s) - 1)]


# ── per-person ────────────────────────────────────────────────────────────────

def build_person_profile(conn: sqlite3.Connection, actor: str, since: str,
                         projects: list[dict], people: dict,
                         alias_map: dict[str, str],
                         verdicts: dict[str, SubjectVerdict],
                         narrative_md: str = "") -> str:
    cur = conn.cursor()

    person = people.get(actor, {"github": actor})
    aliases = person_aliases(person) or [actor]
    aliases_ph = ",".join("?" * len(aliases))

    cur.execute(f"""
        SELECT event_type, count(*) FROM events
        WHERE actor IN ({aliases_ph}) AND ts >= ?
        GROUP BY event_type
    """, (*aliases, since))
    counts = dict(cur.fetchall())

    cur.execute(f"""
        SELECT source, event_type, count(*) FROM events
        WHERE actor IN ({aliases_ph}) AND ts >= ?
        GROUP BY source, event_type
    """, (*aliases, since))
    counts_by_src: dict[tuple, int] = {(r[0], r[1]): r[2] for r in cur.fetchall()}

    cur.execute(f"""
        SELECT o.ts, m.ts FROM events o
        JOIN events m ON o.subject = m.subject
        WHERE o.actor IN ({aliases_ph}) AND o.event_type = 'pr_opened'
          AND m.event_type = 'pr_merged' AND m.ts >= ?
    """, (*aliases, since))
    cycle_hours = [hours_between(r[0], r[1]) for r in cur.fetchall() if r[0] and r[1]]

    cur.execute(f"""
        SELECT subject, title, url, ts FROM events
        WHERE actor IN ({aliases_ph}) AND event_type = 'pr_opened' AND ts >= ?
        ORDER BY ts DESC LIMIT 10
    """, (*aliases, since))
    recent_prs = cur.fetchall()

    pr_summaries = []
    for subject, title, url, ts in recent_prs:
        cur.execute("""
            SELECT body FROM events
            WHERE subject = ? AND actor LIKE '%matterai%' AND event_type = 'review'
            ORDER BY ts DESC LIMIT 1
        """, (subject,))
        row = cur.fetchone()
        body = row[0] if row else None
        verdict = verdicts.get(subject)
        summary = (verdict.summary if verdict and verdict.summary
                   else extract_matterai_summary(body))
        pr_summaries.append({
            "subject":    subject,
            "title":      title,
            "url":        url,
            "ts":         ts,
            "summary":    summary,
            "severity":   severity_count(body),
            "risk_flags": (verdict.risk_flags if verdict else []),
        })

    # Domain attribution split into contributor / owner / reviewer buckets,
    # de-duped by subject within each bucket. Domain mapping comes from
    # Claude verdicts (or keyword fallback for subjects Claude couldn't reach).
    #   contributor  — code/issue/page authored (pr_opened, commit_in_pr, issue_created, …)
    #   owner        — merged others' PRs as tech lead (pr_merged_by)
    #   reviewer     — reviewed/commented but didn't author
    AUTHOR_EVENTS = {'pr_opened', 'commit_pushed', 'commit_in_pr', 'issue_created',
                     'page_created', 'page_updated'}
    OWNER_EVENTS  = {'pr_merged_by'}

    cur.execute(f"""
        SELECT subject, title, body, event_type, ts, url FROM events
        WHERE actor IN ({aliases_ph}) AND ts >= ?
          AND event_type IN ('pr_opened','pr_merged','pr_merged_by','review','comment',
                             'commit_pushed','commit_in_pr','issue_created',
                             'status_change','assignment',
                             'page_updated','page_created')
        ORDER BY ts DESC
    """, (*aliases, since))
    domain_contrib: dict[str, set[str]] = {}
    domain_owner:   dict[str, set[str]] = {}
    domain_review:  dict[str, set[str]] = {}
    subject_meta:   dict[str, dict] = {}
    for subject, title, body, et, ts, url in cur.fetchall():
        if subject and subject not in subject_meta:
            subject_meta[subject] = {"title": title, "ts": ts, "url": url, "event_type": et}
        verdict = verdicts.get(subject or "")
        if not verdict:
            continue
        if et in AUTHOR_EVENTS:
            bucket = domain_contrib
        elif et in OWNER_EVENTS:
            bucket = domain_owner
        else:
            bucket = domain_review
        for slug in verdict.domains:
            bucket.setdefault(slug, set()).add(subject or "")
    contrib_subjects = set().union(*domain_contrib.values()) if domain_contrib else set()
    owner_subjects   = set().union(*domain_owner.values())   if domain_owner   else set()
    for slug, subs in list(domain_review.items()):
        domain_review[slug] = subs - contrib_subjects - owner_subjects
    contrib_counter = Counter({s: len(v) for s, v in domain_contrib.items() if v})
    owner_counter   = Counter({s: len(v) for s, v in domain_owner.items()   if v})
    review_counter  = Counter({s: len(v) for s, v in domain_review.items()  if v})

    cur.execute(f"""
        SELECT r.actor, count(*) FROM events o
        JOIN events r ON o.subject = r.subject
        WHERE o.actor IN ({aliases_ph}) AND o.event_type = 'pr_opened'
          AND r.event_type = 'review' AND r.actor NOT IN ({aliases_ph})
          AND r.actor NOT LIKE ? AND r.ts >= ?
        GROUP BY r.actor
    """, (*aliases, *aliases, BOT_LIKE, since))
    reviewer_rows = cur.fetchall()
    reviewer_collapsed: Counter = Counter()
    for r_actor, n in reviewer_rows:
        reviewer_collapsed[alias_map.get(r_actor, r_actor)] += n
    top_reviewers = reviewer_collapsed.most_common(5)

    cur.execute(f"""
        SELECT o.actor, count(*) FROM events r
        JOIN events o ON r.subject = o.subject
        WHERE r.actor IN ({aliases_ph}) AND r.event_type = 'review'
          AND o.event_type = 'pr_opened' AND o.actor NOT IN ({aliases_ph})
          AND o.actor NOT LIKE ? AND r.ts >= ?
        GROUP BY o.actor
    """, (*aliases, *aliases, BOT_LIKE, since))
    author_rows = cur.fetchall()
    author_collapsed: Counter = Counter()
    for a_actor, n in author_rows:
        author_collapsed[alias_map.get(a_actor, a_actor)] += n
    authors_reviewed = author_collapsed.most_common(5)

    name = people.get(actor, {}).get("name", actor)
    out: list[str] = [
        f"# {name}",
        f"\n_GitHub handle: `{actor}` · window: last {(datetime.now(timezone.utc) - datetime.fromisoformat(since.replace('Z', '+00:00'))).days}d_\n",
    ]
    if narrative_md and narrative_md.strip():
        out.append("## Engineering summary\n")
        out.append(narrative_md.strip())
        out.append("")
    gh_comments   = (counts_by_src.get(("github", "comment"), 0)
                     + counts_by_src.get(("github", "issue_comment"), 0))
    jira_comments = counts_by_src.get(("jira",      "comment"), 0)
    conf_comments = counts_by_src.get(("confluence", "comment"), 0)
    commits_total = counts.get("commit_pushed", 0) + counts.get("commit_in_pr", 0)
    out += [
        "## Activity\n",
        "| Metric | Count |",
        "|---|---|",
        f"| PRs authored       | {counts.get('pr_opened', 0)} |",
        f"| PRs owned (merged) | {counts.get('pr_merged_by', 0)} |",
        f"| Reviews given      | {counts.get('review', 0)} |",
        f"| PR comments        | {gh_comments} |",
        f"| Commits            | {commits_total} |",
        f"| Jira issues created | {counts.get('issue_created', 0)} |",
        f"| Jira transitions   | {counts.get('status_change', 0)} |",
        f"| Jira comments      | {jira_comments} |",
        f"| Confluence edits   | {counts.get('page_created', 0) + counts.get('page_updated', 0)} |",
        f"| Confluence comments | {conf_comments} |",
        "",
    ]

    if cycle_hours:
        out += [
            "## Cycle time (open → merge)\n",
            f"- p50: **{percentile(cycle_hours, 0.5):.1f}h**",
            f"- p90: **{percentile(cycle_hours, 0.9):.1f}h**",
            f"- merged sample: {len(cycle_hours)}",
            "",
        ]

    if contrib_counter:
        out.append("## Domains as contributor (authored)\n")
        for slug, n in contrib_counter.most_common():
            proj = next((p for p in projects if p["slug"] == slug), {})
            out.append(f"- **{proj.get('name', slug)}** — {n} item(s)")
        out.append("")

    if review_counter:
        out.append("## Domains as reviewer / commenter\n")
        for slug, n in review_counter.most_common():
            proj = next((p for p in projects if p["slug"] == slug), {})
            out.append(f"- **{proj.get('name', slug)}** — {n} item(s)")
        out.append("")

    if owner_counter:
        out.append("## Domains as owner (merger)\n")
        for slug, n in owner_counter.most_common():
            proj = next((p for p in projects if p["slug"] == slug), {})
            out.append(f"- **{proj.get('name', slug)}** — {n} PR(s) merged")
        out.append("")

    # Per-domain work breakdown — top 5 most recent items per domain
    if contrib_counter or review_counter or owner_counter:
        out.append("## Work per domain\n")
        total_per: Counter = Counter()
        for s in domain_contrib: total_per[s] += len(domain_contrib[s])
        for s in domain_review:  total_per[s] += len(domain_review.get(s, set()))
        for s in domain_owner:   total_per[s] += len(domain_owner.get(s, set()))
        for slug, _ in total_per.most_common():
            proj = next((p for p in projects if p["slug"] == slug), {})
            cn = len(domain_contrib.get(slug, set()))
            rn = len(domain_review.get(slug, set()))
            on = len(domain_owner.get(slug, set()))
            header_bits = []
            if cn: header_bits.append(f"contrib: {cn}")
            if rn: header_bits.append(f"review: {rn}")
            if on: header_bits.append(f"owner: {on}")
            out.append(f"### {proj.get('name', slug)}  _( {', '.join(header_bits)} )_")

            items: list[dict] = []
            for sub in domain_contrib.get(slug, set()):
                m = subject_meta.get(sub, {})
                items.append({"role": "author",   "subject": sub, **m})
            for sub in domain_review.get(slug, set()):
                m = subject_meta.get(sub, {})
                items.append({"role": "reviewer", "subject": sub, **m})
            for sub in domain_owner.get(slug, set()):
                m = subject_meta.get(sub, {})
                items.append({"role": "owner",    "subject": sub, **m})
            items.sort(key=lambda x: x.get("ts") or "", reverse=True)

            for it in items[:5]:
                ts_short = (it.get("ts") or "")[:10]
                sub      = it.get("subject") or ""
                v        = verdicts.get(sub)
                line     = (v.summary.strip() if v and v.summary else
                            (it.get("title") or "").strip().replace("\n", " "))
                if len(line) > 100:
                    line = line[:97] + "..."
                url      = it.get("url") or ""
                link     = f"[{sub}]({url})" if url and sub else (sub or "")
                role_tag = f"`{it['role']}`"
                if link:
                    out.append(f"- {ts_short} {role_tag} {link} — {line}")
                else:
                    out.append(f"- {ts_short} {role_tag} — {line}")
            if len(items) > 5:
                out.append(f"- _… +{len(items) - 5} more_")
            out.append("")

    if top_reviewers:
        out.append("## Top reviewers of their PRs\n")
        for reviewer, n in top_reviewers:
            rname = people.get(reviewer, {}).get("name", reviewer)
            out.append(f"- {rname} (`{reviewer}`) — {n}")
        out.append("")

    if authors_reviewed:
        out.append("## Authors they review most\n")
        for author, n in authors_reviewed:
            aname = people.get(author, {}).get("name", author)
            out.append(f"- {aname} (`{author}`) — {n}")
        out.append("")

    if pr_summaries:
        out.append("## Recent PRs\n")
        for pr in pr_summaries[:8]:
            sev = pr["severity"]
            sev_str = f" [🔴{sev['red']} 🟠{sev['orange']} 🟡{sev['yellow']}]" if (sev["red"] or sev["orange"]) else ""
            risk_str = ""
            if pr.get("risk_flags"):
                risk_str = f" `{' '.join(pr['risk_flags'])}`"
            out.append(f"### [{pr['subject']}]({pr['url']}) — {fmt_iso(pr['ts'])}{sev_str}{risk_str}")
            out.append(f"**{pr['title']}**")
            if pr["summary"]:
                out.append(f"\n> {pr['summary']}")
            out.append("")

    out.append(f"\n_generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}_")
    return "\n".join(out)


# ── per-project ───────────────────────────────────────────────────────────────

def build_project_rollup(conn: sqlite3.Connection, proj: dict, since: str,
                         people: dict, alias_map: dict[str, str],
                         verdicts: dict[str, SubjectVerdict]) -> str:
    cur = conn.cursor()
    slug = proj["slug"]

    # Subjects this project owns (per Claude verdict).
    project_subjects = {sub for sub, v in verdicts.items() if slug in v.domains}
    days_n = (datetime.now(timezone.utc) - datetime.fromisoformat(since.replace("Z", "+00:00"))).days
    if not project_subjects:
        return (
            f"# {proj['name']}\n\n"
            f"_slug: `{slug}` · window: last {days_n}d · subjects: 0_\n\n"
            f"_No activity in this window._\n\n"
            f"_generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}_"
        )

    ph = ",".join("?" * len(project_subjects))
    cur.execute(f"""
        SELECT actor, event_type, subject, title, ts, url FROM events
        WHERE subject IN ({ph}) AND ts >= ? AND actor NOT LIKE ?
        ORDER BY ts DESC
    """, (*project_subjects, since, BOT_LIKE))
    rows = cur.fetchall()

    by_actor: Counter = Counter()
    for r in rows:
        by_actor[alias_map.get(r[0], r[0])] += 1
    by_type = Counter(r[1] for r in rows)
    # one row per PR subject for "recent PRs" — pick latest pr_opened
    seen: set[str] = set()
    recent_prs: list[tuple] = []
    for actor, et, subject, title, ts, url in rows:
        if et != "pr_opened" or subject in seen:
            continue
        seen.add(subject)
        recent_prs.append((actor, et, subject, title, ts, url))
        if len(recent_prs) >= 10:
            break

    out: list[str] = [
        f"# {proj['name']}",
        f"\n_slug: `{slug}` · window: last {days_n}d · subjects: {len(project_subjects)}_\n",
        "## Activity by type\n",
    ]
    for t, n in by_type.most_common():
        out.append(f"- {t}: {n}")
    out.append("")

    if by_actor:
        out.append("## Top contributors\n")
        for actor, n in by_actor.most_common(8):
            name = people.get(actor, {}).get("name", actor)
            out.append(f"- {name} (`{actor}`) — {n} event(s)")
        out.append("")

    if recent_prs:
        out.append("## Recent PRs\n")
        for actor, _, subject, title, ts, url in recent_prs:
            canon = alias_map.get(actor, actor)
            v = verdicts.get(subject)
            line = (v.summary.strip() if v and v.summary else (title or ""))
            risk = ""
            if v and v.risk_flags:
                risk = f"  `{' '.join(v.risk_flags)}`"
            out.append(f"- [{subject}]({url}) — {line} _by `{canon}` ({fmt_iso(ts)})_{risk}")
        out.append("")

    out.append(f"\n_generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}_")
    return "\n".join(out)


# ── weekly ────────────────────────────────────────────────────────────────────

def build_weekly(conn: sqlite3.Connection, ws: str, we: str,
                 people: dict) -> tuple[str, str]:
    cur = conn.cursor()

    cur.execute("""
        SELECT event_type, count(*) FROM events
        WHERE ts >= ? AND ts < ? AND actor NOT LIKE ?
        GROUP BY event_type
    """, (ws, we, BOT_LIKE))
    counts = dict(cur.fetchall())

    cur.execute("""
        SELECT o.ts, m.ts FROM events o
        JOIN events m ON o.subject = m.subject
        WHERE o.event_type = 'pr_opened' AND m.event_type = 'pr_merged'
          AND m.ts >= ? AND m.ts < ?
    """, (ws, we))
    cycle_hours = [hours_between(r[0], r[1]) for r in cur.fetchall() if r[0] and r[1]]

    cur.execute("""
        SELECT m.subject FROM events m
        WHERE m.event_type = 'pr_merged' AND m.ts >= ? AND m.ts < ?
    """, (ws, we))
    merged_subjects = [r[0] for r in cur.fetchall()]

    reviewed_subjects: set[str] = set()
    if merged_subjects:
        placeholders = ",".join("?" * len(merged_subjects))
        cur.execute(f"""
            SELECT DISTINCT subject FROM events
            WHERE subject IN ({placeholders})
              AND event_type = 'review' AND actor NOT LIKE ?
        """, (*merged_subjects, BOT_LIKE))
        reviewed_subjects = {r[0] for r in cur.fetchall()}

    coverage = (len(reviewed_subjects) / len(merged_subjects) * 100) if merged_subjects else 0

    cur.execute("""
        SELECT actor, count(*) FROM events
        WHERE event_type = 'pr_merged' AND ts >= ? AND ts < ?
          AND actor NOT LIKE ?
        GROUP BY actor ORDER BY count(*) DESC LIMIT 10
    """, (ws, we, BOT_LIKE))
    top_authors = cur.fetchall()

    yr, wk, _ = datetime.fromisoformat(ws.replace("Z", "+00:00")).isocalendar()
    fname = f"{yr}-W{wk:02d}.md"

    out: list[str] = [
        f"# Week {yr}-W{wk:02d}",
        f"\n_{ws[:10]} → {we[:10]}_\n",
        "## Volume\n",
        "| Metric | Count |",
        "|---|---|",
        f"| PRs opened | {counts.get('pr_opened', 0)} |",
        f"| PRs merged | {counts.get('pr_merged', 0)} |",
        f"| Reviews    | {counts.get('review', 0)} |",
        f"| Comments   | {counts.get('comment', 0)} |",
        "",
    ]

    if cycle_hours:
        out += [
            "## Cycle time\n",
            f"- p50: **{percentile(cycle_hours, 0.5):.1f}h**",
            f"- p90: **{percentile(cycle_hours, 0.9):.1f}h**",
            "",
        ]

    out.append(f"## Review coverage\n\n**{coverage:.0f}%** of merged PRs had ≥1 human review ({len(reviewed_subjects)}/{len(merged_subjects)})\n")

    if top_authors:
        out.append("## Top contributors (by PRs merged)\n")
        for actor, n in top_authors:
            name = people.get(actor, {}).get("name", actor)
            out.append(f"- {name} (`{actor}`) — {n}")
        out.append("")

    out.append(f"\n_generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}_")
    return fname, "\n".join(out)


# ── alerts ────────────────────────────────────────────────────────────────────

def build_alerts(conn: sqlite3.Connection, people: dict,
                 classifier_stats: "llm_classifier._Stats | None" = None) -> str:
    cur = conn.cursor()

    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    cur.execute("""
        WITH last_activity AS (
            SELECT subject, MAX(ts) AS last_ts FROM events
            WHERE event_type IN ('pr_opened','review','comment','commit_pushed')
            GROUP BY subject
        ),
        terminated AS (
            SELECT DISTINCT subject FROM events
            WHERE event_type IN ('pr_merged','pr_closed')
        )
        SELECT o.subject, o.actor, o.title, o.url, o.ts, la.last_ts
        FROM events o
        JOIN last_activity la ON o.subject = la.subject
        WHERE o.event_type = 'pr_opened'
          AND o.subject NOT IN (SELECT subject FROM terminated)
          AND la.last_ts < ?
        ORDER BY la.last_ts ASC LIMIT 20
    """, (seven_days_ago,))
    stale = cur.fetchall()

    thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    cur.execute("""
        SELECT m.subject, m.actor, m.title, m.url, m.ts FROM events m
        WHERE m.event_type = 'pr_merged' AND m.ts >= ?
          AND NOT EXISTS (
              SELECT 1 FROM events r
              WHERE r.subject = m.subject AND r.event_type = 'review'
                AND r.actor NOT LIKE ?
          )
        ORDER BY m.ts DESC LIMIT 20
    """, (thirty_days_ago, BOT_LIKE))
    drive_by = cur.fetchall()

    out: list[str] = ["# Alerts\n"]

    if classifier_stats:
        s = classifier_stats
        total_classified = s.cache_hits + s.claude_pass1 + s.claude_pass2 + s.fallback
        if s.fallback > 0 and total_classified > 0:
            pct = (s.fallback / total_classified) * 100
            out.append(
                f"> ⚠ {s.fallback} of {total_classified} subjects ({pct:.0f}%) "
                f"classified by keyword fallback this run — Claude unavailable or rate-limited.\n"
            )
        if s.unmapped_epic_ctx:
            lines = [
                f"> ℹ {len(s.unmapped_epic_ctx)} epic(s) need slug creation — "
                f"run `/slug-epics` in chat to synthesise human-readable slugs:\n"
            ]
            for ec in s.unmapped_epic_ctx:
                ek = ec["epic_key"]
                title = ec.get("epic_title") or "(no title in DB)"
                lines.append(f">   - `{ek}` — {title} ({ec['child_count']} children)")
            lines.append("")
            out.append("\n".join(lines))

    out.append(f"## Stale PRs ({len(stale)})\n")
    out.append("Open PRs with no activity for ≥7 days.\n")
    if stale:
        for subject, actor, title, url, _opened, last in stale:
            name = people.get(actor, {}).get("name", actor)
            out.append(f"- [{subject}]({url}) — **{title}** by {name} (last: {fmt_iso(last)})")
    else:
        out.append("_None._")
    out.append("")

    out.append(f"## Drive-by merges last 30d ({len(drive_by)})\n")
    out.append("PRs merged with no human review.\n")
    if drive_by:
        for subject, actor, title, url, ts in drive_by:
            name = people.get(actor, {}).get("name", actor)
            out.append(f"- [{subject}]({url}) — **{title}** merged by {name} ({fmt_iso(ts)})")
    else:
        out.append("_None._")
    out.append("")

    out.append(f"\n_generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}_")
    return "\n".join(out)


# ── unmapped-epic emission ────────────────────────────────────────────────────

PENDING_SLUG_PATH = ROOT / "state/pending_slug_creation.json"

_SLUG_RULES_MD = """# Epic-slug creation rules

For each epic in `pending_slug_creation.json`, propose a NEW project entry for
`config/projects.yaml`. The chat reads child-ticket titles to synthesise a
human-readable kebab-case slug, descriptive name, and high-signal keywords.

## Verdict schema (write to `state/verdicts.epic_slugs.json`)

```json
[
  {
    "epic_key": "EX-XXXX",
    "slug": "human-readable-kebab",
    "name": "Short title (~60 chars)",
    "keywords": ["multi-word bigram", "another bigram"],
    "merge_into": null
  }
]
```

## Rules

- `slug`: kebab-case, 2-5 tokens, derived from the **dominant theme of child
  tickets** (not the epic title in isolation). NEVER use `epic-<key>` form.
- `name`: human-readable, ~60 chars max.
- `keywords`: 3-6 **bigrams** (multi-word). Unigrams are too generic for
  banking PR matching and cause false positives.
- `merge_into`: when an existing slug in projects.yaml already covers this
  domain, set `merge_into: "<existing-slug>"`. Then `slug` is ignored; the
  epic_key is appended to the existing slug's `jira_epics`. Use sparingly —
  only for clear semantic overlap.
- If an epic has fewer than 2 children AND a generic name (e.g. "Bug fixes",
  "Onboarding"), prefix the slug with the jira prefix (e.g. `ex-onboarding`).

## Apply

```bash
derive/manual-rollup.sh apply-slugs
```

This rewrites projects.yaml, invalidates the `subject_summary` cache for
child subjects of each affected epic, and triggers a re-dump on the next
rollup so children re-classify with the new epic anchor.
"""


def _emit_pending_slug_creation(unmapped_ctx: list[dict]) -> None:
    """Write unmapped-epic context to `pending_slug_creation.json` + rules.md.

    The chat-driven `/slug-epics` flow picks this up, proposes
    `verdicts.epic_slugs.json`, and `apply_epic_slugs.py` folds them into
    projects.yaml.
    """
    if not unmapped_ctx:
        # Clear stale file if no work remaining.
        if PENDING_SLUG_PATH.exists():
            PENDING_SLUG_PATH.unlink()
            rules = PENDING_SLUG_PATH.with_suffix(PENDING_SLUG_PATH.suffix + ".rules.md")
            if rules.exists():
                rules.unlink()
        return
    PENDING_SLUG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_SLUG_PATH.write_text(json.dumps(unmapped_ctx, indent=2, ensure_ascii=False))
    rules = PENDING_SLUG_PATH.with_suffix(PENDING_SLUG_PATH.suffix + ".rules.md")
    rules.write_text(_SLUG_RULES_MD)
    logging.getLogger("rollup").info(
        "pending_slug_creation: %d epic(s) need slug — %s",
        len(unmapped_ctx),
        ", ".join(c["epic_key"] for c in unmapped_ctx[:8]),
    )


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="Lookback in days")
    parser.add_argument("--week", action="store_true", help="Also build current-ISO-week rollup")
    parser.add_argument("--detail-summary", action="store_true",
                        help="Ask Claude for richer 3-5 sentence per-PR detail (stored in subject_summary.detail, ~3x output tokens)")
    parser.add_argument("--skip-narrative", action="store_true",
                        help="Skip per-person Claude narrative generation (saves API quota; uses cached narratives if present)")
    args = parser.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, mode="a"),
            logging.StreamHandler(),
        ],
    )
    log = logging.getLogger(__name__)
    log.info("Rollup starting (lookback=%dd, week=%s)", args.days, args.week)

    if not DB.exists():
        log.error("DB not found at %s", DB)
        sys.exit(1)

    DERIVED.mkdir(exist_ok=True)
    (DERIVED / "people").mkdir(exist_ok=True)
    (DERIVED / "projects").mkdir(exist_ok=True)
    (DERIVED / "weekly").mkdir(exist_ok=True)

    projects = load_projects()
    people, alias_map = load_people()

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat().replace("+00:00", "Z")

    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()

    llm_classifier.ensure_schema(conn)
    team_handles: set[str] = set()
    for p in people.values():
        for key in ("github", "email", "jira_id", "slack_id", "git_name"):
            if p.get(key):
                team_handles.add(p[key])
    subjects = collect_subjects(conn, since, projects, team_handles=team_handles)
    log.info("classifier: %d subjects in window", len(subjects))
    verdicts, classifier_stats = llm_classifier.classify_subjects(
        conn, subjects, projects, detail_summary=args.detail_summary,
    )
    log.info(
        "classifier: cache=%d claude_p1=%d claude_p2=%d fallback=%d "
        "diff_fetched=%d in_tok=%d out_tok=%d",
        classifier_stats.cache_hits, classifier_stats.claude_pass1,
        classifier_stats.claude_pass2, classifier_stats.fallback,
        classifier_stats.diff_fetched, classifier_stats.input_tokens,
        classifier_stats.output_tokens,
    )

    _emit_pending_slug_creation(classifier_stats.unmapped_epic_ctx)

    if not team_handles:
        log.warning("no people.yaml entries with github field — no profiles will be built")

    cur.execute("""
        SELECT actor, count(*) FROM events
        WHERE source = 'github'
          AND actor NOT LIKE ? AND actor IS NOT NULL AND ts >= ?
        GROUP BY actor HAVING count(*) >= 3
        ORDER BY count(*) DESC
    """, (BOT_LIKE, since))
    actors_active = [r[0] for r in cur.fetchall() if r[0] in team_handles]

    log.info("People: %d active / %d total in last %dd", len(actors_active), len(team_handles), args.days)
    if args.skip_narrative:
        log.info("narrative: --skip-narrative set, omitting narrative section")
        narratives = {}
    else:
        # Build verdicts from direct DB lookup (no LLM upgrade) so narrative
        # content_hash matches what dump_pending_narrative._verdicts_for sees.
        # classify_subjects verdicts (above) may have upgraded fallbacks via LLM,
        # changing summaries → different hash → cached narratives not found.
        import json as _json
        verdicts_for_narrative: dict[str, llm_classifier.SubjectVerdict] = {}
        for s in subjects:
            _h = llm_classifier._content_hash(s)
            _row = conn.execute(
                "SELECT domains, summary, risk_flags, confidence, source "
                "FROM subject_summary WHERE subject=? AND content_hash=?",
                (s.subject, _h),
            ).fetchone()
            if _row:
                verdicts_for_narrative[s.subject] = llm_classifier.SubjectVerdict(
                    domains=_json.loads(_row[0] or "[]"),
                    summary=_row[1] or "",
                    risk_flags=_json.loads(_row[2] or "[]"),
                    confidence=_row[3] or 0.0,
                    source=_row[4] or "",
                )
        narratives = narrative.narrate_people(
            conn, actors_active, since, args.days, verdicts_for_narrative, projects, people, alias_map,
        )
    # Emit one profile per canonical person (keyed by github handle in load_people).
    # Iterating raw team_handles produced duplicate files keyed by jira_id /
    # slack_id / email — each person showed up as 3-4 stub files with 0 metrics.
    for actor in sorted(people.keys()):
        md = build_person_profile(
            conn, actor, since, projects, people, alias_map, verdicts,
            narrative_md=narratives.get(actor, ""),
        )
        safe = re.sub(r"[^\w-]", "_", actor)
        (DERIVED / "people" / f"{safe}.md").write_text(md)

    log.info("Projects: %d configured", len(projects))
    for proj in projects:
        md = build_project_rollup(conn, proj, since, people, alias_map, verdicts)
        (DERIVED / "projects" / f"{proj['slug']}.md").write_text(md)

    if args.week:
        now = datetime.now(timezone.utc)
        ws_dt = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        we_dt = ws_dt + timedelta(days=7)
        ws = ws_dt.isoformat().replace("+00:00", "Z")
        we = we_dt.isoformat().replace("+00:00", "Z")
        fname, md = build_weekly(conn, ws, we, people)
        (DERIVED / "weekly" / fname).write_text(md)
        log.info("Weekly: %s", fname)

    md = build_alerts(conn, people, classifier_stats)
    (DERIVED / "alerts.md").write_text(md)
    log.info("Alerts: written")

    conn.close()
    log.info("Rollup done. Output → %s/", DERIVED)


if __name__ == "__main__":
    main()

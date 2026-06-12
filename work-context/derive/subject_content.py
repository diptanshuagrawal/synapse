"""
subject_content.py — pulls the embeddable content for any subject.

A "subject" in events.db is a stable cross-source identifier:
  slack:CH:ts          → slack thread parent
  EX-NNNN              → jira ticket
  page:NNNN            → confluence page
  owner/repo#N         → github PR

This module returns a (subject, source, content) tuple for each. Content is
the text we hand to the embedding model — designed to capture the semantics
of the subject without being so long the model truncates aggressively.

Caps (rough token budgets at ~4 chars/token):
  slack:    parent body + first 5 reply previews + last 2 ~  3000 chars
  jira:     title + description + matterai-summary             ~ 2500 chars
  conf:     title + first ~2000 chars of body                  ~ 2000 chars
  github:   title + body + matterai-summary                    ~ 1500 chars

These caps keep the average input cost low (~500 tokens/subject) and prevent
one chatty thread from dominating cluster geometry.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Optional

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


# Caps in chars (approx 4 chars per token).
_MAX_SLACK_PARENT = 1500
_MAX_SLACK_REPLY = 300
_MAX_SLACK_REPLIES_FIRST = 5
_MAX_SLACK_REPLIES_LAST = 2
_MAX_JIRA = 2500
_MAX_CONF = 2000
_MAX_GH = 1500


def _truncate(s: Optional[str], cap: int) -> str:
    if not s:
        return ""
    s = s.strip()
    if len(s) <= cap:
        return s
    return s[:cap].rstrip() + " …"


_MIN_USEFUL_CONTENT = 30  # chars of real content (after stripping title) needed to bother embedding

# Structural-noise slack roots — channel housekeeping + availability notices.
# These embed into tight junk clusters (near-identical text; ~2.3k threads as
# of 2026-06) that pollute HDBSCAN geometry, waste chat-labeling effort, and
# surface in /ask retrieval. Returning "" turns them into no_content: detect
# skips them and they are never (re-)embedded.
# Keep loosely in sync with ownership_corrections HR_OOO_PHRASES (that list
# drives ownership noise→external; this one drives embeddability).
_NOISE_ALWAYS = (
    "has joined the channel", "has left the channel",
    # Group-DM membership events use "conversation", not "channel" — missed in
    # the first pass; they formed several labeled junk clusters (2026-06-11).
    "has joined the conversation", "has left the conversation",
    "has been added to the conversation",
)
_NOISE_OOO = (
    "out of office", "on leave", "on a leave", "planned leave", "annual leave",
    "sick leave", "leave today", "half day", "day off", "on vacation",
    "wfh", "work from home", "working from home",
    "login late", "logging in late", "will login", "login by", "log in by",
    "feeling unwell",
    # Availability variants observed in surviving junk clusters (2026-06-11).
    # Phrasings kept specific (e.g. "running late by/for", not bare "running
    # late") so short real ops notes ("EOD job running late") survive.
    "won't be able to", "wont be able to", "won;t be able to",
    "won't be available", "not be available",
    "not feeling well", "under the weather", "feeling under the weather",
    "out sick", "taking rest", "taking a leave", "taking leave",
    "taking off today", "taking the day",
    "running late by", "running late for", "logging off early",
    "will be away", "join office", "will join tomorrow",
)
# Availability notices are short. Long threads that merely *mention* leave
# (incident threads, handover discussions) must survive — guard on combined
# length so only short notes are dropped.
_NOISE_OOO_MAX_LEN = 400


def _slack_content(conn: sqlite3.Connection, subject: str) -> str:
    """slack:CH:ts → parent + replies preview.

    NO source-prefix (e.g. `[Slack thread]`) — those bias embeddings toward
    format-clustering rather than topic. Returns empty string when there's
    not enough real content to embed (caller skips)."""
    parent = conn.execute(
        "SELECT title, body FROM events WHERE subject = ? AND event_type = 'thread_started' LIMIT 1",
        (subject,),
    ).fetchone()
    if not parent:
        return ""
    title, body = parent
    parent_text = _truncate(body, _MAX_SLACK_PARENT) if body else ""
    title_text = _truncate(title, 200) if title else ""

    replies = conn.execute(
        """SELECT body FROM events
            WHERE subject = ? AND event_type = 'thread_reply'
            ORDER BY ts ASC""",
        (subject,),
    ).fetchall()

    reply_text = ""
    if replies:
        first = replies[:_MAX_SLACK_REPLIES_FIRST]
        last = replies[-_MAX_SLACK_REPLIES_LAST:] if len(replies) > _MAX_SLACK_REPLIES_FIRST else []
        seen = set()
        chosen = []
        for r in first + last:
            key = r[0]
            if key in seen:
                continue
            seen.add(key)
            chosen.append(r)
        reply_text = "\n".join(_truncate(rb[0], _MAX_SLACK_REPLY) for rb in chosen if rb[0])

    combined = "\n".join(p for p in (title_text, parent_text, reply_text) if p).strip()
    if len(combined) < _MIN_USEFUL_CONTENT:
        return ""  # empty bot ping / channel-join / pure-emoji message — not worth embedding
    # Structural noise — judge on the ROOT message only (title + parent), so a
    # junk-looking root can't be rescued by reply volume, but a long real
    # thread that mentions leave keeps its content via the length guard.
    root_low = f"{title_text} {parent_text}".lower()
    if any(p in root_low for p in _NOISE_ALWAYS):
        return ""
    if len(combined) < _NOISE_OOO_MAX_LEN and any(p in root_low for p in _NOISE_OOO):
        return ""
    return combined


def _jira_content(conn: sqlite3.Connection, subject: str) -> str:
    """EX-NNNN → title + body. No `[Jira <type>]` prefix — biases embeddings."""
    row = conn.execute(
        """SELECT title, body FROM events
            WHERE subject = ? AND source = 'jira'
              AND event_type = 'issue_created'
            ORDER BY ts DESC LIMIT 1""",
        (subject,),
    ).fetchone()
    if not row:
        # Fall back to ANY event for the subject (status-change events also have title).
        row = conn.execute(
            """SELECT title, body FROM events
                WHERE subject = ? AND source = 'jira'
                ORDER BY length(body) DESC LIMIT 1""",
            (subject,),
        ).fetchone()
        if not row:
            return ""
    title, body = row
    title_text = _truncate(title, 250) if title else ""
    body_text = _truncate(body, _MAX_JIRA) if body else ""
    combined = "\n".join(p for p in (title_text, body_text) if p).strip()
    if len(combined) < _MIN_USEFUL_CONTENT:
        return ""
    return combined


def _confluence_content(conn: sqlite3.Connection, subject: str) -> str:
    """page:NNNN → title + body. Prefers the longest-body event for this page
    (page_updated typically wins over comments)."""
    row = conn.execute(
        """SELECT title, body FROM events
            WHERE subject = ? AND source = 'confluence'
              AND event_type IN ('page_created','page_updated')
            ORDER BY length(body) DESC LIMIT 1""",
        (subject,),
    ).fetchone()
    if not row:
        # Fall back to any confluence event for this page.
        row = conn.execute(
            """SELECT title, body FROM events
                WHERE subject = ? AND source = 'confluence'
                ORDER BY length(body) DESC LIMIT 1""",
            (subject,),
        ).fetchone()
        if not row:
            return ""
    title, body = row
    title_text = _truncate(title, 250) if title else ""
    body_text = _truncate(body, _MAX_CONF) if body else ""
    combined = "\n".join(p for p in (title_text, body_text) if p).strip()
    if len(combined) < _MIN_USEFUL_CONTENT:
        return ""
    return combined


def _github_content(conn: sqlite3.Connection, subject: str) -> str:
    """owner/repo#N → PR title + body, with fallbacks for PRs that lack a
    `pr_opened` row (many ingested via `pr_merged` / `commit_in_pr` only).

    Priority:
      1. pr_opened title + body  (best — author's own framing)
      2. matterai-bot comment    (rich auto-summary of the PR diff)
      3. commit titles + top comment  (commit messages + first human comment)

    No `[GitHub PR]` prefix — biases embeddings."""
    # 1. pr_opened
    row = conn.execute(
        """SELECT title, body FROM events
            WHERE subject = ? AND source = 'github' AND event_type = 'pr_opened'
            ORDER BY ts DESC LIMIT 1""",
        (subject,),
    ).fetchone()
    title, body = (row or (None, None))
    title_text = _truncate(title, 250) if title else ""
    body_text = _truncate(body, _MAX_GH) if body else ""
    combined = "\n".join(p for p in (title_text, body_text) if p).strip()
    if len(combined) >= _MIN_USEFUL_CONTENT:
        return combined

    # 2. matterai-bot summary comment (richest auto-generated PR description)
    row = conn.execute(
        """SELECT body FROM events
            WHERE subject = ? AND source = 'github' AND event_type = 'comment'
              AND (actor LIKE 'matterai%' OR actor LIKE '%matterai%')
              AND body IS NOT NULL AND length(body) > 80
            ORDER BY length(body) DESC LIMIT 1""",
        (subject,),
    ).fetchone()
    if row and row[0]:
        mai = _truncate(row[0], _MAX_GH)
        if len(mai) >= _MIN_USEFUL_CONTENT:
            # Preserve PR-shape header if title_text exists (rare but possible).
            return "\n".join(p for p in (title_text, mai) if p).strip()

    # 3. commit titles + top human comment
    commit_titles = conn.execute(
        """SELECT title FROM events
            WHERE subject = ? AND source = 'github' AND event_type = 'commit_in_pr'
              AND title IS NOT NULL AND title != ''
            ORDER BY ts ASC""",
        (subject,),
    ).fetchall()
    top_comment = conn.execute(
        """SELECT body FROM events
            WHERE subject = ? AND source = 'github' AND event_type = 'comment'
              AND actor NOT LIKE '%[bot]%'
              AND body IS NOT NULL AND length(body) > 40
            ORDER BY length(body) DESC LIMIT 1""",
        (subject,),
    ).fetchone()
    parts: list[str] = []
    if commit_titles:
        joined = " | ".join({t for (t,) in commit_titles if t and t != ".."})
        if joined:
            parts.append(_truncate(joined, 700))
    if top_comment and top_comment[0]:
        parts.append(_truncate(top_comment[0], 700))
    combined = "\n".join(parts).strip()
    if len(combined) < _MIN_USEFUL_CONTENT:
        return ""
    return combined


def _service_content(conn: sqlite3.Connection, subject: str) -> str:
    """Service-brief chunk body, ingested by ingest_briefs.py (source='service')."""
    row = conn.execute(
        "SELECT body FROM events WHERE subject = ? AND source = 'service' LIMIT 1",
        (subject,),
    ).fetchone()
    return (row[0] or "") if row else ""


_DISPATCH = {
    "slack": _slack_content,
    "jira": _jira_content,
    "confluence": _confluence_content,
    "github": _github_content,
    "service": _service_content,
}


def detect_source(subject: str) -> str:
    """Best-effort source inference from subject form."""
    # service:<svc>#<section> — checked FIRST: it contains '#' and '/', which
    # would otherwise match the github rule below.
    if subject.startswith("service:"):
        return "service"
    if subject.startswith("slack:"):
        return "slack"
    if subject.startswith("page:"):
        return "confluence"
    if "#" in subject and "/" in subject:
        return "github"
    # EX-NNNN, service-a-NNNN etc — jira ticket key shape.
    if "-" in subject and subject.split("-", 1)[0].isalpha():
        return "jira"
    return "unknown"


def get_content(conn: sqlite3.Connection, subject: str) -> tuple[str, str]:
    """Return (source, content) for a subject. Content empty when no row found
    or source unknown; callers should skip such subjects."""
    source = detect_source(subject)
    fetcher = _DISPATCH.get(source)
    if fetcher is None:
        return "unknown", ""
    return source, fetcher(conn, subject)


def content_sha(content: str) -> str:
    """Stable hash so callers can detect when a subject's embeddable content
    has changed (replies added, body edited) and trigger re-embed."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


__all__ = ["get_content", "detect_source", "content_sha"]

"""
Per-person engineering narrative generator.

Aggregates signals across PRs (authored + reviewed), Jira (issues, transitions,
comments), and Confluence (pages + comments) within the rollup window. Sends
to Claude → returns a structured markdown narrative. Cached in person_narrative
table keyed by (actor, window_days, content_hash) — re-generated only when the
person's activity shape changes.

Auth resolution mirrors llm_classifier:
  ANTHROPIC_API_KEY > ANTHROPIC_AUTH_TOKEN > fallback (no narrative).

Public API:
  ensure_schema(conn)
  narrate_people(conn, actors, since, window_days, verdicts, projects,
                 people, alias_map) -> dict[actor, str]
  load_cached(conn, actor, window_days) -> str | None
  persist(conn, actor, window_days, content_hash, body, source, ...)

Session-mode helper (used when I generate narratives directly in a Claude Code
session, not via API):
  build_signals(conn, actor, since, verdicts, alias_map) -> PersonSignals
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("narrative")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MODEL = "claude-sonnet-4-6"
MAX_OUTPUT_TOKENS = 2000

BOT_LIKE = "%[bot]%"


# ── data ─────────────────────────────────────────────────────────────────────

@dataclass
class AuthoredPR:
    subject: str
    title: str
    domains: list[str]
    summary: str
    risk_flags: list[str]
    ts: str
    state: str            # 'opened' | 'merged'

@dataclass
class GivenReview:
    subject: str
    target_title: str
    target_author: str
    target_domains: list[str]
    target_summary: str
    target_risk_flags: list[str]
    review_count: int     # how many review submissions on this PR
    ts: str

@dataclass
class JiraOwned:
    key: str
    title: str
    epic: str
    domains: list[str]
    summary: str
    transitions: list[str]   # status names in order
    comment_count: int

@dataclass
class JiraTransitioned:
    key: str
    title: str
    transitions: list[str]   # transitions THIS actor performed

@dataclass
class JiraCommented:
    key: str
    title: str
    comment_count: int

@dataclass
class ConfluencePage:
    page_id: str
    title: str
    domains: list[str]
    summary: str
    event: str               # 'page_created' | 'page_updated'
    ts: str

@dataclass
class PersonSignals:
    actor: str
    name: str
    window_days: int
    authored_prs: list[AuthoredPR] = field(default_factory=list)
    reviews_given: list[GivenReview] = field(default_factory=list)
    pr_comments_count: int = 0
    jira_owned: list[JiraOwned] = field(default_factory=list)
    jira_transitioned: list[JiraTransitioned] = field(default_factory=list)
    jira_commented: list[JiraCommented] = field(default_factory=list)
    confluence_pages: list[ConfluencePage] = field(default_factory=list)
    confluence_comments_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ── schema + cache ───────────────────────────────────────────────────────────

def ensure_schema(conn: sqlite3.Connection) -> None:
    sql_path = MIGRATIONS_DIR / "002_person_narrative.sql"
    if sql_path.exists():
        conn.executescript(sql_path.read_text())
        conn.commit()


def _content_hash(signals: PersonSignals) -> str:
    """Stable hash over the signal contents — narrative regenerates only on change."""
    payload = {
        "prs":   [(p.subject, p.state, p.summary[:80]) for p in signals.authored_prs],
        "revs":  [(r.subject, r.review_count, r.target_summary[:80]) for r in signals.reviews_given],
        "jowned": [(j.key, j.transitions, j.comment_count, j.summary[:80]) for j in signals.jira_owned],
        "jtrans": [(j.key, j.transitions) for j in signals.jira_transitioned],
        "jcmt":  [(j.key, j.comment_count) for j in signals.jira_commented],
        "conf":  [(c.page_id, c.event, c.title[:80]) for c in signals.confluence_pages],
        "cnts":  [signals.pr_comments_count, signals.confluence_comments_count],
    }
    h = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return h[:32]


def load_cached(conn: sqlite3.Connection, actor: str, window_days: int,
                content_hash: str) -> Optional[str]:
    cur = conn.execute(
        "SELECT body FROM person_narrative WHERE actor=? AND window_days=? AND content_hash=?",
        (actor, window_days, content_hash),
    )
    row = cur.fetchone()
    return row[0] if row else None


def persist(conn: sqlite3.Connection, actor: str, window_days: int,
            content_hash: str, body: str, source: str,
            model: Optional[str] = None,
            input_tokens: Optional[int] = None,
            output_tokens: Optional[int] = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO person_narrative "
        "(actor, window_days, content_hash, body, source, model, "
        " generated_at, input_tokens, output_tokens) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            actor, window_days, content_hash, body, source, model,
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            input_tokens, output_tokens,
        ),
    )
    conn.commit()


# ── signal aggregation ───────────────────────────────────────────────────────

def _person_aliases(person: dict) -> list[str]:
    out: list[str] = []
    for key in ("github", "email", "jira_id", "slack_id"):
        v = person.get(key)
        if v:
            out.append(v)
    return out


def build_signals(conn: sqlite3.Connection, actor: str, since: str,
                  window_days: int, verdicts: dict,
                  people: dict, alias_map: dict) -> PersonSignals:
    cur = conn.cursor()
    person = people.get(actor, {"github": actor})
    aliases = _person_aliases(person) or [actor]
    aliases_ph = ",".join("?" * len(aliases))
    name = person.get("name", actor)

    sig = PersonSignals(actor=actor, name=name, window_days=window_days)

    # ── PRs authored ─────────────────────────────────────────────────────────
    cur.execute(f"""
        SELECT subject, title, ts, event_type FROM events
        WHERE actor IN ({aliases_ph}) AND event_type IN ('pr_opened','pr_merged')
          AND ts >= ?
        ORDER BY ts ASC
    """, (*aliases, since))
    by_subj: dict[str, dict] = {}
    for sub, title, ts, et in cur.fetchall():
        rec = by_subj.setdefault(sub, {"subject": sub, "title": title, "ts": ts, "state": "opened"})
        if et == "pr_merged":
            rec["state"] = "merged"
            rec["ts"] = ts
        if not rec.get("title"):
            rec["title"] = title
    for r in by_subj.values():
        v = verdicts.get(r["subject"])
        sig.authored_prs.append(AuthoredPR(
            subject=r["subject"], title=r.get("title") or "",
            domains=(v.domains if v else []),
            summary=(v.summary if v else ""),
            risk_flags=(v.risk_flags if v else []),
            ts=r["ts"], state=r["state"],
        ))

    # ── Reviews given (target PR not authored by them) ───────────────────────
    cur.execute(f"""
        SELECT r.subject, r.ts, count(*) FROM events r
        WHERE r.actor IN ({aliases_ph}) AND r.event_type = 'review' AND r.ts >= ?
        GROUP BY r.subject
    """, (*aliases, since))
    review_subjects = cur.fetchall()
    for sub, ts, n in review_subjects:
        cur.execute("""
            SELECT actor, title FROM events
            WHERE subject = ? AND event_type = 'pr_opened' LIMIT 1
        """, (sub,))
        row = cur.fetchone()
        if not row:
            continue
        target_actor, target_title = row
        canon = alias_map.get(target_actor, target_actor)
        if canon == actor:
            continue   # skip self-reviews
        v = verdicts.get(sub)
        sig.reviews_given.append(GivenReview(
            subject=sub, target_title=target_title or "",
            target_author=canon,
            target_domains=(v.domains if v else []),
            target_summary=(v.summary if v else ""),
            target_risk_flags=(v.risk_flags if v else []),
            review_count=n, ts=ts,
        ))

    # ── PR comments (count only) ─────────────────────────────────────────────
    cur.execute(f"""
        SELECT count(*) FROM events
        WHERE actor IN ({aliases_ph}) AND event_type = 'comment' AND source = 'github'
          AND ts >= ?
    """, (*aliases, since))
    sig.pr_comments_count = cur.fetchone()[0] or 0

    # ── Jira owned (issue_created) ───────────────────────────────────────────
    cur.execute(f"""
        SELECT subject, title FROM events
        WHERE actor IN ({aliases_ph}) AND event_type = 'issue_created' AND ts >= ?
    """, (*aliases, since))
    owned_keys = cur.fetchall()
    for key, title in owned_keys:
        # transitions on this issue, in chronological order
        cur.execute("""
            SELECT title FROM events
            WHERE subject = ? AND event_type = 'status_change' ORDER BY ts ASC
        """, (key,))
        transitions = [r[0] for r in cur.fetchall() if r[0]]
        cur.execute("""
            SELECT count(*) FROM events WHERE subject = ? AND event_type = 'comment'
        """, (key,))
        cmt = cur.fetchone()[0] or 0
        v = verdicts.get(key)
        sig.jira_owned.append(JiraOwned(
            key=key, title=title or "",
            epic=(v.domains[0] if v and v.domains else ""),
            domains=(v.domains if v else []),
            summary=(v.summary if v else ""),
            transitions=transitions, comment_count=cmt,
        ))

    # ── Jira transitions actor performed (on issues NOT owned) ───────────────
    owned_set = {k for k, _ in owned_keys}
    cur.execute(f"""
        SELECT subject, title FROM events
        WHERE actor IN ({aliases_ph}) AND event_type = 'status_change' AND ts >= ?
        ORDER BY ts ASC
    """, (*aliases, since))
    by_key: dict[str, dict] = {}
    for sub, title in cur.fetchall():
        if not sub or sub in owned_set:
            continue
        rec = by_key.setdefault(sub, {"key": sub, "title": "", "transitions": []})
        if title:
            rec["transitions"].append(title)
        # title comes from issue_created — fetch once
    for key in list(by_key.keys()):
        cur.execute("SELECT title FROM events WHERE subject=? AND event_type='issue_created' LIMIT 1", (key,))
        row = cur.fetchone()
        by_key[key]["title"] = row[0] if row else ""
    for r in by_key.values():
        sig.jira_transitioned.append(JiraTransitioned(**r))

    # ── Jira comments (issues not owned) ─────────────────────────────────────
    cur.execute(f"""
        SELECT subject, count(*) FROM events
        WHERE actor IN ({aliases_ph}) AND event_type = 'comment' AND source = 'jira' AND ts >= ?
        GROUP BY subject
    """, (*aliases, since))
    for sub, n in cur.fetchall():
        if not sub or sub in owned_set:
            continue
        cur.execute("SELECT title FROM events WHERE subject=? AND event_type='issue_created' LIMIT 1", (sub,))
        row = cur.fetchone()
        sig.jira_commented.append(JiraCommented(
            key=sub, title=(row[0] if row else ""), comment_count=n,
        ))

    # ── Confluence pages ─────────────────────────────────────────────────────
    cur.execute(f"""
        SELECT subject, title, event_type, ts FROM events
        WHERE actor IN ({aliases_ph}) AND event_type IN ('page_created','page_updated')
          AND ts >= ?
    """, (*aliases, since))
    for sub, title, et, ts in cur.fetchall():
        v = verdicts.get(sub)
        sig.confluence_pages.append(ConfluencePage(
            page_id=sub, title=title or "",
            domains=(v.domains if v else []),
            summary=(v.summary if v else ""),
            event=et, ts=ts,
        ))
    cur.execute(f"""
        SELECT count(*) FROM events
        WHERE actor IN ({aliases_ph}) AND event_type = 'comment' AND source = 'confluence'
          AND ts >= ?
    """, (*aliases, since))
    sig.confluence_comments_count = cur.fetchone()[0] or 0

    return sig


# ── prompt rendering ─────────────────────────────────────────────────────────

def _render_signals_block(sig: PersonSignals) -> str:
    lines: list[str] = [
        f"# {sig.name} ({sig.actor}) — last {sig.window_days}d",
        "",
        f"## PRs authored ({len(sig.authored_prs)})",
    ]
    for p in sig.authored_prs[:30]:
        risk = f" risks={p.risk_flags}" if p.risk_flags else ""
        lines.append(f"- [{p.state}] {p.subject} domains={p.domains}{risk}")
        if p.summary:
            lines.append(f"  · {p.summary}")
    if len(sig.authored_prs) > 30:
        lines.append(f"… +{len(sig.authored_prs) - 30} more")

    lines += ["", f"## Reviews given ({len(sig.reviews_given)} PRs reviewed)"]
    for r in sig.reviews_given[:30]:
        risk = f" risks={r.target_risk_flags}" if r.target_risk_flags else ""
        lines.append(f"- {r.subject} (author: {r.target_author}, {r.review_count} review submissions) domains={r.target_domains}{risk}")
        if r.target_summary:
            lines.append(f"  · {r.target_summary}")
    if len(sig.reviews_given) > 30:
        lines.append(f"… +{len(sig.reviews_given) - 30} more")
    lines.append(f"PR comments authored: {sig.pr_comments_count}")

    lines += ["", f"## Jira issues owned ({len(sig.jira_owned)})"]
    for j in sig.jira_owned[:30]:
        lines.append(f"- {j.key} domains={j.domains} transitions={j.transitions} comments={j.comment_count}")
        if j.summary:
            lines.append(f"  · {j.summary}")
    if len(sig.jira_owned) > 30:
        lines.append(f"… +{len(sig.jira_owned) - 30} more")

    if sig.jira_transitioned:
        lines += ["", f"## Jira transitions performed on others' issues ({len(sig.jira_transitioned)})"]
        for j in sig.jira_transitioned[:20]:
            lines.append(f"- {j.key} transitions={j.transitions} title={j.title[:80]}")

    if sig.jira_commented:
        lines += ["", f"## Jira issues commented ({len(sig.jira_commented)})"]
        for j in sig.jira_commented[:20]:
            lines.append(f"- {j.key} ({j.comment_count}) {j.title[:80]}")

    lines += ["", f"## Confluence pages ({len(sig.confluence_pages)})"]
    for c in sig.confluence_pages[:20]:
        lines.append(f"- [{c.event}] {c.page_id} domains={c.domains} title={c.title[:80]}")
        if c.summary:
            lines.append(f"  · {c.summary}")
    lines.append(f"Confluence comments: {sig.confluence_comments_count}")
    return "\n".join(lines)


SYSTEM_PROMPT = """You write engineering performance narratives for software engineers based on activity signals across GitHub, Jira, and Confluence.

Output format (markdown):
1. **TL;DR** — three short bullets capturing the dominant story.
2. **## Engineering focus** — what they shipped, where (domains), with risk/quality observations from MatterAI/risk_flags. Cite concrete PRs.
3. **## Review behavior** — depth (review counts vs surface skim), domain coverage, mentorship signal (who they review most + what topics).
4. **## Jira ownership** — issues owned, transition velocity (created→done?), epics driven, blocking patterns.
5. **## Documentation** — Confluence pages written/updated and what they cover. Skip section if empty.
6. **## Risks / signals to watch** — concrete patterns: stale PRs, drive-by merges, security/race flags, abandoned issues. Skip if none.

Style:
- Short, dense, factual. No fluff. No "great work" platitudes.
- Cite identifiers (PR#, JIRA-KEY, page-id) when making a claim.
- Avoid restating raw counts already in the activity table — synthesize meaning.
- 200–400 words total. Trim if signals are sparse.
- If a section has no signal, omit it (don't write "no activity here")."""


def _render_user_msg(sig: PersonSignals, project_index: dict) -> str:
    proj_lines = [f"- {p['slug']}: {p['name']}" for p in project_index.values()]
    return (
        "Project slugs that may appear in domain tags:\n"
        + "\n".join(proj_lines)
        + "\n\nActivity signals:\n\n"
        + _render_signals_block(sig)
    )


# ── Claude call ──────────────────────────────────────────────────────────────

def _call_claude(client, system_blocks: list[dict], user_text: str,
                 max_tokens: int = MAX_OUTPUT_TOKENS) -> Optional[dict]:
    import anthropic
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=[{"role": "user", "content": user_text}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            return {
                "text": text,
                "input_tokens":  getattr(resp.usage, "input_tokens", 0),
                "output_tokens": getattr(resp.usage, "output_tokens", 0),
            }
        except anthropic.APIStatusError as e:
            if 500 <= e.status_code < 600 or e.status_code == 429:
                time.sleep(min(60, 2 ** attempt * 5))
                continue
            log.error("Claude APIStatusError %d: %s", e.status_code, e)
            return None
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            log.warning("Claude connectivity %s, retry", e)
            time.sleep(2 ** attempt * 2)
            continue
        except Exception as e:
            log.error("Claude unexpected error: %s", e)
            return None
    return None


# ── public entrypoint ────────────────────────────────────────────────────────

def narrate_people(conn: sqlite3.Connection, actors: list[str], since: str,
                   window_days: int, verdicts: dict,
                   projects: list[dict], people: dict,
                   alias_map: dict) -> dict[str, str]:
    ensure_schema(conn)

    project_index = {p["slug"]: p for p in projects}

    # Build signals + content hashes once.
    signals: dict[str, PersonSignals] = {}
    hashes:  dict[str, str] = {}
    for actor in actors:
        sig = build_signals(conn, actor, since, window_days, verdicts, people, alias_map)
        signals[actor] = sig
        hashes[actor]  = _content_hash(sig)

    # Cache lookup.
    out: dict[str, str] = {}
    misses: list[str] = []
    for actor in actors:
        cached = load_cached(conn, actor, window_days, hashes[actor])
        if cached:
            out[actor] = cached
        else:
            misses.append(actor)

    if not misses:
        log.info("narrative: all %d people cache-hit", len(actors))
        return out

    api_key    = os.environ.get("ANTHROPIC_API_KEY")
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not (api_key or auth_token):
        log.warning("narrative: no Claude credentials → skipping narrative for %d people", len(misses))
        return out

    try:
        import anthropic
    except ImportError:
        log.error("anthropic SDK not installed — narrative skipped")
        return out

    if api_key:
        client = anthropic.Anthropic(api_key=api_key)
        source = "claude-api"
    else:
        client = anthropic.Anthropic(
            auth_token=auth_token,
            default_headers={"anthropic-beta": "oauth-2025-04-20"},
        )
        source = "claude-api"   # OAuth still hits same API

    system_blocks = [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
    ]

    for actor in misses:
        sig = signals[actor]
        if not (sig.authored_prs or sig.reviews_given or sig.jira_owned
                or sig.jira_transitioned or sig.confluence_pages):
            continue   # no signal → no narrative
        user_text = _render_user_msg(sig, project_index)
        resp = _call_claude(client, system_blocks, user_text)
        if resp is None or not resp["text"].strip():
            log.warning("narrative: Claude returned nothing for %s — skipping", actor)
            continue
        body = resp["text"].strip()
        persist(conn, actor, window_days, hashes[actor], body, source,
                model=MODEL,
                input_tokens=resp["input_tokens"],
                output_tokens=resp["output_tokens"])
        out[actor] = body
        log.info("narrative: wrote %s (in_tok=%d out_tok=%d)",
                 actor, resp["input_tokens"], resp["output_tokens"])

    return out

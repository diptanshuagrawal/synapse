"""
Two-pass Claude classifier for rollup subjects.

Public entry: classify_subjects(conn, subjects, projects) → (verdicts, stats).

Pass 1: title + body + MatterAI summary + Jira epic body + Confluence body.
        Claude either records classification or requests the diff.
Pass 2: re-call only for subjects flagged in pass 1, with diff fetched.

Caching: subject_summary table keyed by (subject, content_hash). Re-classify
only when content hash changes. Persisted incrementally, so an interrupted run
keeps progress.

Fallback: when ANTHROPIC_API_KEY is unset, or Claude / SDK errors are raised,
falls back to keyword classification (detect_domains) for affected subjects.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import diff_fetcher

log = logging.getLogger("llm-classifier")

ROOT = Path(__file__).parent.parent
MIGRATIONS_DIR = Path(__file__).parent / "migrations"

MODEL = "claude-sonnet-4-6"
PASS1_BATCH_SIZE = 10
PASS2_BATCH_SIZE = 4   # diffs are big — keep batch tighter
MAX_OUTPUT_TOKENS = 4096

BODY_CAP        = 2000
MATTERAI_CAP    = 500
EPIC_BODY_CAP   = 1000
CONF_BODY_CAP   = 1000

EPIC_PREFIX_RE = re.compile(r"\[Epic ([A-Z]+-\d+)\]")


# ── data types ────────────────────────────────────────────────────────────────

@dataclass
class SubjectInput:
    subject: str
    source: str            # 'github', 'jira', 'confluence'
    title: str
    body: str = ""
    matterai_summary: str = ""
    matterai_severity: dict = field(default_factory=dict)
    epic_key: str = ""
    epic_body: str = ""
    confluence_body: str = ""
    issue_type: str = ""   # jira only: 'Epic' | 'Task' | 'CMR' | 'Bug' | 'Story' | '' for non-jira
    story_points: float | None = None   # jira only
    sprint_id: int | None = None        # jira only
    sprint_name: str = ""               # jira only
    sprint_state: str = ""              # jira only: 'active' | 'closed' | 'future'


@dataclass
class SubjectVerdict:
    domains: list[str]
    summary: str
    risk_flags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    source: str = "claude"   # 'claude' or 'fallback'
    detail: str = ""          # richer per-PR narrative; empty when detail_summary=False


# ── schema ────────────────────────────────────────────────────────────────────

def ensure_schema(conn: sqlite3.Connection) -> None:
    sql_path = MIGRATIONS_DIR / "001_subject_summary.sql"
    if sql_path.exists():
        conn.executescript(sql_path.read_text())
    else:
        # inline fallback if migration file got separated
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subject_summary (
                subject TEXT NOT NULL, content_hash TEXT NOT NULL,
                domains TEXT NOT NULL, summary TEXT NOT NULL,
                risk_flags TEXT, confidence REAL, source TEXT NOT NULL,
                model TEXT, classified_at TEXT NOT NULL,
                input_tokens INTEGER, output_tokens INTEGER,
                PRIMARY KEY (subject, content_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_subject_summary_subject ON subject_summary(subject);
        """)
    # Additive column — safe to run on existing DBs; ignore if already present
    try:
        conn.execute("ALTER TABLE subject_summary ADD COLUMN detail TEXT")
    except Exception:
        pass
    conn.commit()


# ── hashing & truncation ──────────────────────────────────────────────────────

def _trunc(s: str, cap: int) -> str:
    s = s or ""
    if len(s) <= cap:
        return s
    return s[:cap] + "…"


def _content_hash(s: SubjectInput, with_diff: str = "") -> str:
    parts = [
        s.subject, s.source,
        _trunc(s.title, 500),
        _trunc(s.body, BODY_CAP),
        _trunc(s.matterai_summary, MATTERAI_CAP),
        s.epic_key,
        _trunc(s.epic_body, EPIC_BODY_CAP),
        _trunc(s.confluence_body, CONF_BODY_CAP),
        with_diff,
    ]
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return h[:32]


# ── keyword fallback ──────────────────────────────────────────────────────────

def _fallback_classify(s: SubjectInput, projects: list[dict]) -> SubjectVerdict:
    """Mirrors the original detect_domains() — kept here so rollup can drop it."""
    text = f"{s.title} {s.matterai_summary} {s.body}"
    low = text.lower()
    hits: list[str] = []
    epic_key = s.epic_key
    for proj in projects:
        if epic_key and epic_key in (proj.get("jira_epics") or []):
            hits.append(proj["slug"])
            continue
        for kw in proj.get("keywords", []):
            if kw.lower() in low:
                hits.append(proj["slug"])
                break
    summary = (s.matterai_summary or s.title or "").strip().replace("\n", " ")
    if len(summary) > 180:
        summary = summary[:177] + "…"
    return SubjectVerdict(
        domains=hits, summary=summary, risk_flags=[], confidence=0.0, source="fallback"
    )


# ── epic → slug mapping ───────────────────────────────────────────────────────

def _build_epic_to_slug(projects: list[dict]) -> dict[str, str]:
    """Map each jira epic key → project slug. First project claiming an epic wins."""
    m: dict[str, str] = {}
    for p in projects:
        for ek in (p.get("jira_epics") or []):
            if ek not in m:
                m[ek] = p["slug"]
    return m


def _collect_unmapped_epic_context(
    subjects: list[SubjectInput],
    epic_to_slug: dict[str, str],
    conn: sqlite3.Connection,
) -> list[dict]:
    """Gather child-aware context for epic keys referenced by subjects but absent
    from projects.yaml. Returned dicts are emitted to `pending_slug_creation.json`
    so chat-LLM can synthesise human-readable slugs + keywords (see /rollup skill).

    This function MUST NOT fabricate slugs of its own — historically it produced
    `epic-<key>` slugs which polluted projects.yaml.
    """
    unmapped = {s.epic_key for s in subjects if s.epic_key and s.epic_key not in epic_to_slug}
    if not unmapped:
        return []

    # Epic parent rows
    epic_titles: dict[str, str] = {}
    epic_bodies: dict[str, str] = {}
    ph = ",".join("?" * len(unmapped))
    cur = conn.execute(
        f"SELECT subject, title, COALESCE(body,'') FROM events "
        f"WHERE subject IN ({ph}) AND event_type = 'issue_created'",
        tuple(sorted(unmapped)),
    )
    for sub, title, body in cur.fetchall():
        clean = EPIC_PREFIX_RE.sub("", title or "").strip(" -[]")
        if clean:
            epic_titles[sub] = clean
        if body:
            epic_bodies[sub] = body[:600]

    out: list[dict] = []
    for ek in sorted(unmapped):
        # Child tickets carrying "[Epic <ek>]" prefix in their title
        pat = f"%[Epic {ek}]%"
        children = conn.execute(
            "SELECT subject, title, COALESCE(body,'') FROM events "
            "WHERE event_type = 'issue_created' AND title LIKE ? "
            "ORDER BY ts DESC LIMIT 15",
            (pat,),
        ).fetchall()
        child_ctx = [
            {
                "key": sub,
                "title": EPIC_PREFIX_RE.sub("", t or "").strip(" -[]"),
                "body_snippet": (b or "")[:240],
            }
            for sub, t, b in children
        ]
        out.append({
            "epic_key": ek,
            "epic_title": epic_titles.get(ek, ""),
            "epic_body_snippet": epic_bodies.get(ek, ""),
            "child_count": len(child_ctx),
            "children": child_ctx,
        })
        log.info("unmapped epic: %s (%d children) — needs slug creation",
                 ek, len(child_ctx))
    return out


def _apply_epic_anchor(v: SubjectVerdict, epic_key: str,
                       epic_to_slug: dict[str, str]) -> SubjectVerdict:
    """Guarantee the epic-derived slug appears first in domains (safety net for Claude misses)."""
    if not epic_key:
        return v
    slug = epic_to_slug.get(epic_key)
    if not slug:
        return v
    if slug in v.domains:
        if v.domains[0] != slug:
            v.domains.remove(slug)
            v.domains.insert(0, slug)
    else:
        v.domains.insert(0, slug)
    return v


# ── projects context for the LLM ──────────────────────────────────────────────

def _projects_context(projects: list[dict]) -> str:
    lines: list[str] = ["# Projects (slugs you may emit)\n"]
    for p in projects:
        kw = ", ".join(p.get("keywords", [])[:8])
        epics = ", ".join(p.get("jira_epics", []) or [])
        lines.append(f"- **{p['slug']}** — {p.get('name', p['slug'])}")
        if kw:
            lines.append(f"  hint keywords: {kw}")
        if epics:
            lines.append(f"  jira epics: {epics}")
    lines.append("")
    return "\n".join(lines)


SYSTEM_PROMPT = """You classify engineering work into projects. For each subject (a PR, Jira issue, or Confluence page) decide which projects it materially touches.

A project is "touched" if the change modifies its code paths, alters its observable behavior, or directly documents it. Hint keywords are guidance — judge by intent and substance, not surface string match. A subject can touch zero, one, or many projects.

IMPORTANT — epic anchor: When a subject shows `epic_domain: <slug>`, that slug MUST appear in your domains list. This is a deterministic mapping from the team's Jira epic structure and overrides your own inference. Add other slugs only when clearly warranted by the content.

For each subject, call exactly one tool:
  - `record_classification` when you can decide from the inputs given.
  - `request_diff` when, for a GitHub PR specifically, the title/body/MatterAI summary/epic/Confluence content is too thin to decide. Only allowed for source=github subjects. Never request a diff twice for the same subject.

Domains MUST be slugs from the project list. Summary ≤ 180 chars, action-first, present tense ("Fix double-charge in counter-charge engine retry path"; "Migrate UPI ATM withdrawals to new strategy"). Avoid filler.

Risk flags only when actually present in inputs: security, data-loss, panic, race, migration, breaking-api. Empty list when none.

Confidence is your own calibrated 0–1 estimate."""

DETAIL_INSTRUCTION = (
    "\n\nFor GitHub PRs, also populate `detail`: 3–5 sentences covering what specifically changed, "
    "which code areas/files were touched, key design decisions, reviewer concerns raised, or gotchas. "
    "Write for an EM who needs to understand the change without opening the PR. "
    "Omit (leave empty) for Jira issues or Confluence pages with no substantial code content."
)


def _tools(project_slugs: list[str], detail_summary: bool = False) -> list[dict]:
    return [
        {
            "name": "record_classification",
            "description": "Record the final classification for a subject.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "subject":     {"type": "string"},
                    "domains":     {"type": "array", "items": {"type": "string", "enum": project_slugs}},
                    "summary":     {"type": "string", "maxLength": 200},
                    "risk_flags":  {"type": "array", "items": {"type": "string", "enum": [
                        "security", "data-loss", "panic", "race", "migration", "breaking-api"
                    ]}},
                    "confidence":  {"type": "number", "minimum": 0, "maximum": 1},
                    **({"detail": {"type": "string", "maxLength": 800}} if detail_summary else {}),
                },
                "required": ["subject", "domains", "summary", "confidence"],
            },
        },
        {
            "name": "request_diff",
            "description": "Defer classification — diff is needed. Only valid for source=github subjects on pass 1.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "reason":  {"type": "string"},
                },
                "required": ["subject", "reason"],
            },
        },
    ]


# ── prompt building ───────────────────────────────────────────────────────────

def _render_subject(s: SubjectInput, diff_text: str = "", epic_domain: str = "") -> str:
    parts: list[str] = [
        f"## subject: {s.subject}",
        f"source: {s.source}",
        f"title: {s.title}",
    ]
    if s.epic_key:
        parts.append(f"jira_epic: {s.epic_key}")
    if epic_domain:
        parts.append(f"epic_domain: {epic_domain}  ← this slug MUST be in your domains list")
    if s.body.strip():
        parts.append(f"body:\n{_trunc(s.body, BODY_CAP)}")
    if s.matterai_summary:
        parts.append(f"matterai_summary: {_trunc(s.matterai_summary, MATTERAI_CAP)}")
    sev = s.matterai_severity or {}
    if any(sev.values()):
        parts.append(f"matterai_severity: red={sev.get('red',0)} orange={sev.get('orange',0)} yellow={sev.get('yellow',0)}")
    if s.epic_body:
        parts.append(f"epic_body:\n{_trunc(s.epic_body, EPIC_BODY_CAP)}")
    if s.confluence_body:
        parts.append(f"confluence_body:\n{_trunc(s.confluence_body, CONF_BODY_CAP)}")
    if diff_text:
        parts.append(f"diff:\n{diff_text}")
    return "\n".join(parts)


def _user_msg(subjects: list[SubjectInput], diffs: dict[str, str],
              epic_to_slug: dict[str, str] | None = None) -> str:
    et = epic_to_slug or {}
    rendered = "\n\n---\n\n".join(
        _render_subject(s, diffs.get(s.subject, ""), et.get(s.epic_key, "") if s.epic_key else "")
        for s in subjects
    )
    return (
        "Classify each subject below. Emit one tool call per subject (in order).\n\n"
        + rendered
    )


# ── persistence ───────────────────────────────────────────────────────────────

def _load_cached(conn: sqlite3.Connection, subject: str, content_hash: str) -> Optional[SubjectVerdict]:
    cur = conn.execute(
        "SELECT domains, summary, risk_flags, confidence, source, detail "
        "FROM subject_summary WHERE subject=? AND content_hash=?",
        (subject, content_hash),
    )
    row = cur.fetchone()
    if not row:
        return None
    return SubjectVerdict(
        domains=json.loads(row[0]),
        summary=row[1],
        risk_flags=json.loads(row[2] or "[]"),
        confidence=row[3] or 0.0,
        source=row[4],
        detail=row[5] or "",
    )


def _persist(conn: sqlite3.Connection, subject: str, content_hash: str,
             v: SubjectVerdict, model: Optional[str],
             input_tokens: Optional[int], output_tokens: Optional[int]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO subject_summary "
        "(subject, content_hash, domains, summary, risk_flags, confidence, "
        " source, model, classified_at, input_tokens, output_tokens, detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            subject, content_hash,
            json.dumps(v.domains), v.summary,
            json.dumps(v.risk_flags), v.confidence,
            v.source, model,
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            input_tokens, output_tokens,
            v.detail or None,
        ),
    )
    conn.commit()


# ── Claude call ──────────────────────────────────────────────────────────────

class _Stats:
    def __init__(self) -> None:
        self.cache_hits = 0
        self.claude_pass1 = 0
        self.claude_pass2 = 0
        self.fallback = 0
        self.diff_fetched = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.unmapped_epic_ctx: list[dict] = []  # epic-key contexts emitted for chat slug creation


def _call_claude(client, project_slugs: list[str], system_blocks: list[dict],
                 batch: list[SubjectInput], diffs: dict[str, str],
                 epic_to_slug: dict[str, str] | None = None,
                 detail_summary: bool = False,
                 max_tokens: int = MAX_OUTPUT_TOKENS) -> Optional[dict]:
    """One Claude call with retry on transient errors. Returns response dict or None."""
    import anthropic

    messages = [{"role": "user", "content": _user_msg(batch, diffs, epic_to_slug)}]
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system_blocks,
                tools=_tools(project_slugs, detail_summary=detail_summary),
                messages=messages,
            )
            return {
                "content": resp.content,
                "input_tokens":  getattr(resp.usage, "input_tokens", 0),
                "output_tokens": getattr(resp.usage, "output_tokens", 0),
            }
        except anthropic.APIStatusError as e:
            if 500 <= e.status_code < 600 or e.status_code == 429:
                wait = min(60, 2 ** attempt * 5)
                log.warning("Claude %d, retry in %ds (attempt %d)", e.status_code, wait, attempt + 1)
                time.sleep(wait)
                continue
            log.error("Claude APIStatusError %d: %s", e.status_code, e)
            return None
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            log.warning("Claude connectivity %s, retry (attempt %d)", e, attempt + 1)
            time.sleep(2 ** attempt * 2)
            continue
        except Exception as e:  # safety net — never let classifier crash rollup
            log.error("Claude unexpected error: %s", e)
            return None
    return None


def _parse_tool_calls(resp: dict) -> dict[str, dict]:
    """Return {subject: {tool_name, input}} from Claude response."""
    out: dict[str, dict] = {}
    for block in resp["content"]:
        if getattr(block, "type", None) != "tool_use":
            continue
        inp = block.input or {}
        sub = inp.get("subject")
        if sub:
            out[sub] = {"tool": block.name, "input": inp}
    return out


def _verdict_from_tool(tool_input: dict, project_slugs_set: set[str]) -> SubjectVerdict:
    raw_domains = tool_input.get("domains") or []
    domains = [d for d in raw_domains if d in project_slugs_set]
    summary = (tool_input.get("summary") or "").strip().replace("\n", " ")
    if len(summary) > 200:
        summary = summary[:197] + "…"
    detail = (tool_input.get("detail") or "").strip()
    if len(detail) > 800:
        detail = detail[:797] + "…"
    return SubjectVerdict(
        domains=domains,
        summary=summary,
        risk_flags=list(tool_input.get("risk_flags") or []),
        confidence=float(tool_input.get("confidence") or 0.0),
        source="claude",
        detail=detail,
    )


# ── public entrypoint ─────────────────────────────────────────────────────────

def classify_subjects(
    conn: sqlite3.Connection,
    subjects: list[SubjectInput],
    projects: list[dict],
    detail_summary: bool = False,
) -> tuple[dict[str, SubjectVerdict], _Stats]:
    """Classify all subjects. Cache hits short-circuit. Returns (verdicts, stats).

    detail_summary: when True, ask Claude for a richer 3-5 sentence per-PR detail
    narrative stored in SubjectVerdict.detail and subject_summary.detail.
    ~3× output tokens per PR subject. Default False.
    """
    ensure_schema(conn)

    stats = _Stats()
    verdicts: dict[str, SubjectVerdict] = {}
    epic_to_slug = _build_epic_to_slug(projects)

    # Collect context for any epic keys referenced by subjects but missing from
    # projects.yaml. The caller (rollup.py) writes this to
    # pending_slug_creation.json so chat-LLM can synthesise human-readable slugs.
    # No slug is fabricated here — `epic_to_slug` stays unchanged for these
    # epics, so their child subjects classify on title/body alone this run.
    stats.unmapped_epic_ctx = _collect_unmapped_epic_context(subjects, epic_to_slug, conn)

    project_slugs = [p["slug"] for p in projects]
    project_slugs_set = set(project_slugs)

    # When credentials are available, treat fallback-cached rows as misses so
    # they get re-classified by Claude — system auto-heals once auth arrives.
    api_key    = os.environ.get("ANTHROPIC_API_KEY")
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    upgrade_fallbacks = bool(api_key or auth_token)

    misses: list[SubjectInput] = []
    miss_hash: dict[str, str] = {}
    for s in subjects:
        h = _content_hash(s)
        cached = _load_cached(conn, s.subject, h)
        if cached and not (upgrade_fallbacks and cached.source == "fallback"):
            verdicts[s.subject] = _apply_epic_anchor(cached, s.epic_key, epic_to_slug)
            stats.cache_hits += 1
        else:
            misses.append(s)
            miss_hash[s.subject] = h

    if not misses:
        log.info("classify: all %d subjects cache-hit", len(subjects))
        return verdicts, stats

    if not (api_key or auth_token):
        log.warning("no Claude credentials → fallback for %d subjects", len(misses))
        for s in misses:
            v = _fallback_classify(s, projects)
            verdicts[s.subject] = v
            stats.fallback += 1
            _persist(conn, s.subject, miss_hash[s.subject], v, None, None, None)
        return verdicts, stats

    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "auth credentials present but anthropic SDK not installed. "
            "Install it or unset ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN to use keyword-only mode."
        )

    if api_key:
        client = anthropic.Anthropic(api_key=api_key)
        log.info("classify: using API key auth")
    else:
        # OAuth bearer (Claude Code login). Requires beta header.
        client = anthropic.Anthropic(
            auth_token=auth_token,
            default_headers={"anthropic-beta": "oauth-2025-04-20"},
        )
        log.info("classify: using OAuth bearer auth (Claude Code)")

    # System prompt with prompt-caching on the projects context.
    projects_ctx = _projects_context(projects)
    base_prompt = SYSTEM_PROMPT + (DETAIL_INSTRUCTION if detail_summary else "")
    system_blocks = [
        {"type": "text", "text": base_prompt},
        {"type": "text", "text": projects_ctx, "cache_control": {"type": "ephemeral"}},
    ]

    pass2_subjects: list[SubjectInput] = []

    # ── Pass 1 ────────────────────────────────────────────────────────────────
    for i in range(0, len(misses), PASS1_BATCH_SIZE):
        batch = misses[i:i + PASS1_BATCH_SIZE]
        resp = _call_claude(client, project_slugs, system_blocks, batch,
                            diffs={}, epic_to_slug=epic_to_slug,
                            detail_summary=detail_summary)
        if resp is None:
            raise RuntimeError(
                f"pass1: Claude API failed after retries (rate limit / 5xx / connectivity) "
                f"for batch of {len(batch)} subjects. Rollup aborted — fix tokens or rate limits and rerun. "
                f"Subjects in failed batch: {[s.subject for s in batch]}"
            )

        stats.input_tokens  += resp["input_tokens"]
        stats.output_tokens += resp["output_tokens"]
        calls = _parse_tool_calls(resp)

        for s in batch:
            call = calls.get(s.subject)
            if not call:
                # Claude didn't emit a tool call for this subject → fallback
                v = _fallback_classify(s, projects)
                verdicts[s.subject] = v
                stats.fallback += 1
                _persist(conn, s.subject, miss_hash[s.subject], v, MODEL,
                         resp["input_tokens"], resp["output_tokens"])
                continue
            if call["tool"] == "request_diff" and s.source == "github":
                pass2_subjects.append(s)
                continue
            if call["tool"] == "request_diff":
                # request_diff illegitimate for non-github → force fallback
                v = _fallback_classify(s, projects)
                verdicts[s.subject] = v
                stats.fallback += 1
                _persist(conn, s.subject, miss_hash[s.subject], v, MODEL,
                         resp["input_tokens"], resp["output_tokens"])
                continue
            v = _verdict_from_tool(call["input"], project_slugs_set)
            v = _apply_epic_anchor(v, s.epic_key, epic_to_slug)
            verdicts[s.subject] = v
            stats.claude_pass1 += 1
            _persist(conn, s.subject, miss_hash[s.subject], v, MODEL,
                     resp["input_tokens"], resp["output_tokens"])

    # ── Hard confidence threshold: promote low-conf github subjects to Pass 2 ──
    _CONF_THRESHOLD = 0.7
    _pass2_already = {s.subject for s in pass2_subjects}
    for s in list(misses):
        if s.subject not in verdicts:
            continue  # already in pass2 or fallback
        if s.source != "github":
            continue
        v = verdicts[s.subject]
        if v.source == "fallback":
            continue  # fallback already short-circuits; let upgrade_fallbacks handle
        if v.confidence < _CONF_THRESHOLD and s.subject not in _pass2_already:
            log.info(
                "conf<%.1f: promoting %s to pass2 (conf=%.2f)",
                _CONF_THRESHOLD, s.subject, v.confidence,
            )
            # Delete no-diff cache entry so future runs also go through pass2 path
            conn.execute(
                "DELETE FROM subject_summary WHERE subject=? AND content_hash=?",
                (s.subject, miss_hash[s.subject]),
            )
            conn.commit()
            del verdicts[s.subject]
            stats.claude_pass1 -= 1
            pass2_subjects.append(s)
            _pass2_already.add(s.subject)

    # ── Pass 2 (subjects Claude flagged + low-confidence promotions) ───────────
    if pass2_subjects:
        log.info("pass2: fetching diffs for %d subjects", len(pass2_subjects))
        diffs: dict[str, str] = {}
        for s in pass2_subjects:
            df = diff_fetcher.fetch_diff(s.subject)
            if df is not None:
                stats.diff_fetched += 1
                diffs[s.subject] = df.to_text()
            else:
                diffs[s.subject] = ""

        for i in range(0, len(pass2_subjects), PASS2_BATCH_SIZE):
            batch = pass2_subjects[i:i + PASS2_BATCH_SIZE]
            # rebuild content hash with diff included so cache key is stable on re-runs
            batch_hashes = {s.subject: _content_hash(s, with_diff=diffs.get(s.subject, "")) for s in batch}
            # cache check post-diff (might already exist if a previous run got here)
            need: list[SubjectInput] = []
            for s in batch:
                cached = _load_cached(conn, s.subject, batch_hashes[s.subject])
                if cached:
                    verdicts[s.subject] = cached
                    stats.cache_hits += 1
                else:
                    need.append(s)
            if not need:
                continue

            resp = _call_claude(client, project_slugs, system_blocks, need, diffs,
                                epic_to_slug=epic_to_slug, detail_summary=detail_summary)
            if resp is None:
                raise RuntimeError(
                    f"pass2: Claude API failed after retries (rate limit / 5xx / connectivity) "
                    f"for batch of {len(need)} subjects. Rollup aborted — fix tokens or rate limits and rerun. "
                    f"Subjects in failed batch: {[s.subject for s in need]}"
                )

            stats.input_tokens  += resp["input_tokens"]
            stats.output_tokens += resp["output_tokens"]
            calls = _parse_tool_calls(resp)
            for s in need:
                call = calls.get(s.subject)
                if not call or call["tool"] != "record_classification":
                    # No second deferral — fallback if pass2 didn't decide
                    v = _fallback_classify(s, projects)
                    verdicts[s.subject] = v
                    stats.fallback += 1
                    _persist(conn, s.subject, batch_hashes[s.subject], v, MODEL,
                             resp["input_tokens"], resp["output_tokens"])
                    continue
                v = _verdict_from_tool(call["input"], project_slugs_set)
                v = _apply_epic_anchor(v, s.epic_key, epic_to_slug)
                verdicts[s.subject] = v
                stats.claude_pass2 += 1
                _persist(conn, s.subject, batch_hashes[s.subject], v, MODEL,
                         resp["input_tokens"], resp["output_tokens"])

    log.info(
        "classify: cache=%d claude_p1=%d claude_p2=%d fallback=%d diff_fetched=%d in_tok=%d out_tok=%d",
        stats.cache_hits, stats.claude_pass1, stats.claude_pass2,
        stats.fallback, stats.diff_fetched, stats.input_tokens, stats.output_tokens,
    )
    return verdicts, stats


# ── helpers used by rollup.py for SubjectInput assembly ───────────────────────

def extract_epic_key(text: str) -> str:
    if not text:
        return ""
    m = EPIC_PREFIX_RE.search(text)
    return m.group(1) if m else ""

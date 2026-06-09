"""
slack_upsert.py — local helper for Slack ingest skills.

Owns:
  - Regex parser for the Slack MCP "detailed" text response format
  - UPSERT semantics into events.db
  - Edit + delete reconcile over a trailing window
  - DM hard-skip detector

Skills (.claude/commands/slack-{ingest,backfill,reconcile,discover}.md) call MCP
tools, get the text response back, and hand it to this module. Module owns SQL,
schema awareness, and dedup logic. Skill stays declarative.

Schema requirements: migration 004 columns must exist on the events table.
common.py::_ensure_schema applies them lazily on first connect.

See prd/slack-ingest.md §6 (transport + response format), §7 (schema delta),
§8 (ingest flow), §12 (hard constraints including DM hard-skip).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Iterable

# Allow `python -m derive.slack_upsert` to find sibling ingest module.
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import Event, Refs, enrich_refs  # noqa: E402
from derive.sources_config import slack_workspace  # noqa: E402

# ── Public types ────────────────────────────────────────────────────────────


@dataclass
class ParsedMessage:
    """One Slack message parsed out of an MCP text response."""

    actor_id: str              # U0… or B0… for bots
    actor_name: str            # display name
    ts: str                    # raw Slack ts string, e.g. "1778667150.756969"
    body: str                  # message text, mentions left as raw <@U…|name>
    is_bot: bool               # True if actor_id starts with "B"
    edited: bool               # True if "(edited)" suffix present
    thread_parent_ts: Optional[str] = None  # None for top-level, set for replies
    reactions_json: Optional[str] = None    # JSON dict serialised; None if absent
    reply_count: Optional[int] = None       # Channel-page only: `Thread: N replies` count
    files_json: Optional[str] = None        # JSON list of {id,name,mimetype,size,mode,permalink,user}; None if no attachments
    raw_block: str = ""        # full unparsed block for debugging


@dataclass
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


# ── Constants ───────────────────────────────────────────────────────────────

# Matches: === Message from <Name> (U0… or B0…) at <human-ts> ===
_MSG_HEADER = re.compile(
    r"^===\s+Message from\s+(?P<name>.+?)\s+\((?P<actor_id>[UB][A-Z0-9]+)\)\s+at\s+(?P<human_ts>.+?)\s+===\s*$",
    re.MULTILINE,
)

# slack_read_thread response uses a different shape: a parent block then
# zero-or-more reply blocks. Author + ts split across separate lines.
_THREAD_PARENT_HEADER = re.compile(
    r"^===\s+THREAD PARENT MESSAGE\s+===\s*$",
    re.MULTILINE,
)
_THREAD_REPLY_HEADER = re.compile(
    r"^---\s+Reply\s+\d+\s+of\s+\d+\s+---\s*$",
    re.MULTILINE,
)
_FROM_LINE = re.compile(
    r"^From:\s+(?P<name>.+?)\s+\((?P<actor_id>[UB][A-Z0-9]+)\)\s*$",
    re.MULTILINE,
)
_TIME_LINE = re.compile(r"^Time:\s+(?P<human_ts>.+?)\s*$", re.MULTILINE)

_MSG_TS_LINE = re.compile(r"^Message TS:\s+(?P<ts>\d+\.\d+)\s*$", re.MULTILINE)

# Optional "(edited)" trailing marker after the human ts in some channels.
_EDITED_MARKER = re.compile(r"\(edited\)", re.IGNORECASE)

# Reactions line, format TBD on live observation. Speculative regex.
_REACTIONS_LINE = re.compile(
    r"^Reactions:\s+(?P<reactions>.+)$",
    re.MULTILINE,
)

# Channel-page format: top-level message blocks that have a thread show
# `Thread: N replies (latest: ...)`. Captured to seed reply_count so the
# next /slack-ingest run knows which parents need a thread fetch.
_REPLY_COUNT_LINE = re.compile(
    r"^Thread:\s+(?P<count>\d+)\s+repl(?:y|ies)\b",
    re.MULTILINE,
)

# Lines emitted inside thread blocks that aren't part of the message body.
_THREAD_NOISE_LINES = (
    "=== THREAD REPLIES",
    "No thread messsages",   # slack-mcp typo, observed verbatim
    "No thread messages",
)

# Cursor inside pagination_info: "cursor: <base64>" — sometimes wrapped in backticks.
_CURSOR_LINE = re.compile(r"cursor:\s+`?(?P<cursor>[A-Za-z0-9+/=_-]+)`?")

# DM channel flags from MCP slack_search_channels response.
_DM_HINTS = ("private_channel im", "private_channel mpim", "im channel", "mpim channel")


# ── DM hard-skip ────────────────────────────────────────────────────────────


def is_dm_channel(channel_meta: dict | str) -> bool:
    """Return True if the channel is a 1:1 IM or a group MPIM.

    Accepts either a parsed dict (with is_im/is_mpim keys) or the raw
    slack_search_channels text snippet. Defence in depth — caller should
    check this AND verify channel_id is in config/slack_channels.yaml.
    """
    if isinstance(channel_meta, dict):
        return bool(channel_meta.get("is_im") or channel_meta.get("is_mpim"))
    # String fallback — look for hint substrings produced by MCP.
    s = channel_meta.lower()
    return "is_im" in s and "true" in s.split("is_im", 1)[1][:20] or \
           "is_mpim" in s and "true" in s.split("is_mpim", 1)[1][:20] or \
           any(hint in s for hint in _DM_HINTS)


# ── MCP response parsing ────────────────────────────────────────────────────


def parse_mcp_messages(text: str) -> list[ParsedMessage]:
    """Parse a slack_read_channel or slack_read_thread "detailed" response.

    Channel responses use `=== Message from <Name> (Uxxx) at <ts> ===` headers.
    Thread responses use a different shape: `=== THREAD PARENT MESSAGE ===`
    for the parent and `--- Reply N of M ---` for each reply, with the author
    and human-readable timestamp on separate `From:` / `Time:` lines.

    Returns a list of ParsedMessage in the order they appear (newest-first for
    slack_read_channel, oldest-first for slack_read_thread per Slack convention).
    """
    if not text:
        return []

    # Collect header positions from all three shapes, sort by file offset, then
    # carve blocks between consecutive headers.
    headers: list[tuple[str, re.Match]] = []
    for m in _MSG_HEADER.finditer(text):
        headers.append(("channel", m))
    for m in _THREAD_PARENT_HEADER.finditer(text):
        headers.append(("thread_parent", m))
    for m in _THREAD_REPLY_HEADER.finditer(text):
        headers.append(("thread_reply", m))
    if not headers:
        return []
    headers.sort(key=lambda x: x[1].start())

    out: list[ParsedMessage] = []
    for i, (kind, h) in enumerate(headers):
        block_start = h.start()
        block_end = headers[i + 1][1].start() if i + 1 < len(headers) else len(text)
        block = text[block_start:block_end]

        # Message TS line is required for every shape.
        ts_match = _MSG_TS_LINE.search(block)
        if not ts_match:
            continue
        ts = ts_match.group("ts")

        if kind == "channel":
            actor_id = h.group("actor_id")
            actor_name = h.group("name").strip()
            edited = bool(_EDITED_MARKER.search(h.group("human_ts")))
        else:
            # Thread parent / reply — actor + human ts live on separate lines
            # between the block header and the Message TS line. Positions are
            # relative to `block`, not `text`.
            from_m = _FROM_LINE.search(block, 0, ts_match.start())
            if not from_m:
                continue
            actor_id = from_m.group("actor_id")
            actor_name = from_m.group("name").strip()
            time_m = _TIME_LINE.search(block, 0, ts_match.start())
            edited = bool(_EDITED_MARKER.search(time_m.group("human_ts"))) if time_m else False

        is_bot = actor_id.startswith("B")

        # Body = everything after the Message TS line and before the next header,
        # less any trailing whitespace, reactions markers, and thread-noise
        # boilerplate emitted between the parent block and the first reply.
        body_start = ts_match.end()
        body_lines: list[str] = []
        reactions_json: Optional[str] = None
        reply_count: Optional[int] = None

        for line in block[body_start:].splitlines():
            if _REACTIONS_LINE.match(line):
                m = _REACTIONS_LINE.match(line)
                reactions_json = _parse_reactions_freeform(m.group("reactions"))
                continue
            rc_match = _REPLY_COUNT_LINE.match(line)
            if rc_match:
                reply_count = int(rc_match.group("count"))
                continue
            stripped = line.strip()
            if any(stripped.startswith(p) for p in _THREAD_NOISE_LINES):
                continue
            body_lines.append(line)

        body = "\n".join(body_lines).strip()

        out.append(ParsedMessage(
            actor_id=actor_id,
            actor_name=actor_name,
            ts=ts,
            body=body,
            is_bot=is_bot,
            edited=edited,
            reactions_json=reactions_json,
            reply_count=reply_count,
            raw_block=block,
        ))

    return out


def _parse_reactions_freeform(s: str) -> Optional[str]:
    """Parse a reactions line into a JSON dict {name: count}.

    Two observed formats from slack-mcp:
      - Channel:    "Reactions:  :+1: 5  :eyes: 2"     (colon-wrapped names)
      - Thread:     "Reactions: takecare (9) +1 (1)"   (bare names + paren-count)

    Returns None when nothing parses cleanly so callers can skip silently.
    Keys are normalised to bare emoji names (no surrounding colons).
    """
    pairs = re.findall(r":([A-Za-z0-9_+-]+):\s+(\d+)", s)
    if not pairs:
        pairs = re.findall(r"([A-Za-z0-9_+-]+)\s*\((\d+)\)", s)
    if not pairs:
        return None
    return json.dumps({k: int(v) for k, v in pairs})


def extract_cursor(pagination_info: str) -> Optional[str]:
    """Pull next-page cursor out of the pagination_info string. Returns None if
    last page ("End of results")."""
    if not pagination_info:
        return None
    if "end of results" in pagination_info.lower():
        return None
    m = _CURSOR_LINE.search(pagination_info)
    return m.group("cursor") if m else None


# ── UPSERT into events table ────────────────────────────────────────────────


def _event_id(channel_id: str, ts: str, thread_parent_ts: Optional[str]) -> str:
    """Stable ID for a Slack message.

    Top-level: slack:<channel>:<ts>
    Reply:     slack:<channel>:<thread_parent_ts>:<ts>

    Subject (separate from id) is:
      slack:<channel>:<thread_parent_ts or ts>   — one subject per thread
    """
    if thread_parent_ts and thread_parent_ts != ts:
        return f"slack:{channel_id}:{thread_parent_ts}:{ts}"
    return f"slack:{channel_id}:{ts}"


def _subject(channel_id: str, ts: str, thread_parent_ts: Optional[str]) -> str:
    return f"slack:{channel_id}:{thread_parent_ts or ts}"


def _ts_to_iso(slack_ts: str) -> str:
    """Convert Slack float-seconds ts to ISO8601 UTC."""
    dt = datetime.fromtimestamp(float(slack_ts), tz=timezone.utc)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def upsert_event(
    conn: sqlite3.Connection,
    msg: ParsedMessage,
    channel_id: str,
    thread_parent_ts: Optional[str] = None,
    slack_users_cache: Optional[dict[str, str]] = None,
) -> str:
    """Insert or update a single Slack message + its event_refs rows.

    Returns one of: 'inserted', 'updated', 'unchanged'.

    UPSERT key: events.id (= _event_id(...)).
    On edit (body differs or msg.edited=True), overwrite body + set edited_ts
    AND re-extract refs (refs may have changed if body changed).
    Never deletes — that's the reconcile pass's job (sets deleted_ts tombstone).

    slack_users_cache: {U-id: canonical} for resolving <@U…> mentions in body.
        Lazy-populate from state/slack_users_cache.json; mentions of unknown
        U-ids skip the person ref (backfill utility runs later).
    """
    event_id = _event_id(channel_id, msg.ts, thread_parent_ts)
    subject = _subject(channel_id, msg.ts, thread_parent_ts)
    event_type = "thread_reply" if (thread_parent_ts and thread_parent_ts != msg.ts) else "thread_started"
    iso_ts = _ts_to_iso(msg.ts)

    # Build an Event for enrich_refs (only fields it reads).
    ev = Event(
        id=event_id,
        source="slack",
        event_type=event_type,
        ts=iso_ts,
        actor=msg.actor_id,
        subject=subject,
        title=_title_from_body(msg.body),
        body=msg.body,
        url=_url(channel_id, msg.ts, thread_parent_ts),
    )
    enrich_refs(ev, actor_field="slack_id", slack_users_cache=slack_users_cache)

    # Lookup existing row.
    row = conn.execute(
        "SELECT body, edited_ts FROM events WHERE id = ?", (event_id,)
    ).fetchone()

    if row is None:
        # New row — INSERT events + event_refs.
        edited_ts = _now_iso() if msg.edited else None
        conn.execute(
            """INSERT INTO events
               (id, source, event_type, ts, actor, subject, title, body, url,
                raw_path, channel_id, thread_ts, edited_ts, reactions_json,
                reply_count, files_json)
               VALUES (?, 'slack', ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)""",
            (
                event_id, event_type, iso_ts, msg.actor_id, subject,
                ev.title, msg.body, ev.url,
                channel_id, thread_parent_ts, edited_ts, msg.reactions_json,
                msg.reply_count, msg.files_json,
            ),
        )
        _write_refs(conn, event_id, ev.refs)
        return "inserted"

    # Row exists — UPDATE if body differs or edited flag set.
    # Normalize whitespace before comparing: Slack frequently re-emits the
    # same message with cosmetic trailing-newline / indentation changes (and
    # so does the MCP layer when round-tripping). Those aren't real edits
    # and should not fire a refs-rewrite or churn `edited_ts`. The stored
    # body is still updated when a *true* edit lands because `msg.edited`
    # is True for genuine Slack edits.
    stored_body, stored_edited_ts = row[0], row[1]
    incoming_norm = (msg.body or "").strip()
    stored_norm = (stored_body or "").strip()
    body_changed = incoming_norm != stored_norm
    if body_changed or (msg.edited and not stored_edited_ts):
        conn.execute(
            """UPDATE events
                  SET body = ?,
                      title = ?,
                      edited_ts = ?,
                      reactions_json = ?,
                      reply_count = COALESCE(?, reply_count),
                      files_json = ?
                WHERE id = ?""",
            (
                msg.body,
                ev.title,
                _now_iso(),
                msg.reactions_json,
                msg.reply_count,
                msg.files_json,
                event_id,
            ),
        )
        # Wipe + re-insert refs (body may have new tickets/PRs/mentions).
        conn.execute("DELETE FROM event_refs WHERE event_id = ?", (event_id,))
        _write_refs(conn, event_id, ev.refs)
        return "updated"

    # Reactions can change without body change; refresh reactions_json silently.
    if msg.reactions_json and msg.reactions_json != _current_reactions(conn, event_id):
        conn.execute(
            "UPDATE events SET reactions_json = ? WHERE id = ?",
            (msg.reactions_json, event_id),
        )
        return "updated"

    # reply_count can grow without body change; refresh silently when parser
    # captured a newer value (channel re-page or reconcile re-fetch).
    if msg.reply_count is not None:
        stored_rc = conn.execute(
            "SELECT reply_count FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if stored_rc and stored_rc[0] != msg.reply_count:
            conn.execute(
                "UPDATE events SET reply_count = ? WHERE id = ?",
                (msg.reply_count, event_id),
            )
            return "updated"

    return "unchanged"


def _write_refs(conn: sqlite3.Connection, event_id: str, refs: Refs) -> None:
    """INSERT OR IGNORE every ref tuple for this event."""
    rows = (
        [(event_id, "person",       v) for v in refs.people]
        + [(event_id, "project",      v) for v in refs.projects]
        + [(event_id, "ticket",       v) for v in refs.tickets]
        + [(event_id, "page",         v) for v in refs.pages]
        + [(event_id, "pull_request", v) for v in refs.pull_requests]
        + [(event_id, "slack_thread", v) for v in refs.slack_threads]
    )
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO event_refs (event_id, ref_type, ref_value) VALUES (?, ?, ?)",
            rows,
        )


def _current_reactions(conn: sqlite3.Connection, event_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT reactions_json FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    return row[0] if row else None


def _title_from_body(body: str) -> str:
    """First line of body, truncated to 200 chars. Mirrors how jira/github
    rows derive title."""
    if not body:
        return ""
    first = body.split("\n", 1)[0]
    return first[:200]


_SLACK_WORKSPACE = slack_workspace()  # subdomain of slack.com, from config/sources.yaml


def _url(channel_id: str, ts: str, thread_parent_ts: Optional[str] = None) -> str:
    """Browser-clickable Slack permalink.

    Top-level: https://<ws>.slack.com/archives/<channel>/p<ts-no-dot>
    Reply:     same + ?thread_ts=<parent>&cid=<channel> (Slack auto-scrolls
               the reply into thread view)
    """
    ts_no_dot = ts.replace(".", "")
    base = f"https://{_SLACK_WORKSPACE}.slack.com/archives/{channel_id}/p{ts_no_dot}"
    if thread_parent_ts and thread_parent_ts != ts:
        return f"{base}?thread_ts={thread_parent_ts}&cid={channel_id}"
    return base


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Bulk upsert helper ──────────────────────────────────────────────────────


def upsert_messages(
    conn: sqlite3.Connection,
    messages: Iterable[ParsedMessage],
    channel_id: str,
    thread_parent_ts: Optional[str] = None,
    slack_users_cache: Optional[dict[str, str]] = None,
) -> UpsertResult:
    """Upsert a batch. Wraps in a transaction; rolls back on any error.

    slack_users_cache: passed to per-message upsert for <@U…> mention resolution.
    """
    result = UpsertResult()
    try:
        with conn:  # transaction
            for msg in messages:
                try:
                    outcome = upsert_event(
                        conn, msg, channel_id, thread_parent_ts, slack_users_cache,
                    )
                    if outcome == "inserted":
                        result.inserted += 1
                    elif outcome == "updated":
                        result.updated += 1
                    else:
                        result.skipped += 1
                except sqlite3.Error as e:
                    result.errors.append(f"{msg.ts}: {e}")
    except sqlite3.Error as e:
        result.errors.append(f"transaction-level: {e}")
    return result


# ── Reconcile (edits + deletions over a window) ─────────────────────────────


def reconcile_window(
    conn: sqlite3.Connection,
    channel_id: str,
    window_start_iso: str,
    api_msgs: list[ParsedMessage],
    thread_parent_ts_map: Optional[dict[str, str]] = None,
    slack_users_cache: Optional[dict[str, str]] = None,
) -> dict:
    """Reconcile a trailing window for one channel.

    Steps:
      1. For each msg in api_msgs → upsert (catches edits + late inserts).
      2. For each stored row in (channel, window) that isn't in api_ts_set →
         set deleted_ts = now (tombstone). Body preserved.

    thread_parent_ts_map: {msg.ts: parent_ts} for msgs that are replies.
        None for top-level msgs in the window.

    Returns counts: {inserted, updated, unchanged_in_api, tombstoned, errors}.
    """
    thread_parent_ts_map = thread_parent_ts_map or {}

    # Phase 1: upsert every api_msg.
    counts = {"inserted": 0, "updated": 0, "unchanged_in_api": 0, "tombstoned": 0, "errors": []}
    api_ts_set: set[str] = set()
    try:
        with conn:
            for msg in api_msgs:
                api_ts_set.add(msg.ts)
                parent_ts = thread_parent_ts_map.get(msg.ts)
                try:
                    outcome = upsert_event(
                        conn, msg, channel_id, parent_ts, slack_users_cache,
                    )
                    if outcome == "inserted":
                        counts["inserted"] += 1
                    elif outcome == "updated":
                        counts["updated"] += 1
                    else:
                        counts["unchanged_in_api"] += 1
                except sqlite3.Error as e:
                    counts["errors"].append(f"upsert {msg.ts}: {e}")
    except sqlite3.Error as e:
        counts["errors"].append(f"upsert-phase: {e}")
        return counts

    # Phase 2: tombstone any stored row in the window that the API didn't return.
    # We compare on the Slack ts (events.ts is ISO, but events.id encodes
    # raw ts after the channel prefix — easier to derive ts from id).
    try:
        with conn:
            # Pull every undeleted slack row for this channel in window.
            rows = conn.execute(
                """SELECT id, ts FROM events
                    WHERE source = 'slack'
                      AND channel_id = ?
                      AND ts >= ?
                      AND deleted_ts IS NULL""",
                (channel_id, window_start_iso),
            ).fetchall()

            for r in rows:
                # id format: slack:<channel>:<ts>  OR  slack:<channel>:<parent>:<ts>
                # Trailing slack_ts is the last colon-segment.
                slack_ts = r[0].rsplit(":", 1)[1]
                if slack_ts not in api_ts_set:
                    conn.execute(
                        "UPDATE events SET deleted_ts = ? WHERE id = ?",
                        (_now_iso(), r[0]),
                    )
                    counts["tombstoned"] += 1
    except sqlite3.Error as e:
        counts["errors"].append(f"tombstone-phase: {e}")

    return counts


# ── Inline tests (run: python -m derive.slack_upsert) ───────────────────────


def _selftest() -> None:
    """Run inline parser + DM-skip tests. Exits 0 on pass, non-zero on fail."""
    import sys

    # --- Parser tests ---
    sample_detailed = """Channel: #example-channel-internal (C0EXAMPLE)

=== Message from Ivan Example (U0EXAMPLE) at 2026-05-13 15:42:30 IST ===
Message TS: 1778667150.756969
<@U0EXAMPLE|Frank> please review the charge release
multi-line content here

=== Message from Owner Example (U0EXAMPLE) at 2026-05-13 12:29:24 IST ===
Message TS: 1778654964.500000
<!subteam^S0EXAMPLE> FYI

=== Message from EX Standup (B0EXAMPLE) at 2026-05-13 11:45:04 IST (edited) ===
Message TS: 1778650504.012345
Standup ping. Please join.
"""

    msgs = parse_mcp_messages(sample_detailed)
    assert len(msgs) == 3, f"expected 3 msgs, got {len(msgs)}"
    assert msgs[0].actor_id == "U0EXAMPLE"
    assert msgs[0].actor_name == "Ivan Example"
    assert msgs[0].ts == "1778667150.756969"
    assert not msgs[0].is_bot
    assert not msgs[0].edited
    assert "Frank" in msgs[0].body
    assert msgs[0].body.count("\n") == 1, f"body lines off: {msgs[0].body!r}"
    assert msgs[2].is_bot
    assert msgs[2].edited
    print("  ✓ parse_mcp_messages — 3 msgs, bot + edit detected")

    # --- Thread response parsing ---
    sample_thread = """=== THREAD PARENT MESSAGE ===
From: Ivan Example (U0EXAMPLE)
Time: 2026-05-13 15:42:30 IST
Message TS: 1778667150.756969
<@U0EXAMPLE|Frank> question about charges

=== THREAD REPLIES (2 total) ===

--- Reply 1 of 2 ---
From: Eve Example (U0MENTION)
Time: 2026-05-13 23:29:33 IST
Message TS: 1778695173.495759
we haven't conveyed anything to them.

--- Reply 2 of 2 ---
From: Eve Example (U0MENTION)
Time: 2026-05-13 23:31:03 IST
Message TS: 1778695263.302389
we should plan for the migration as next step.
Reactions: +1 (1)
"""
    tmsgs = parse_mcp_messages(sample_thread)
    assert len(tmsgs) == 3, f"expected 3 thread msgs (parent + 2 replies), got {len(tmsgs)}"
    assert tmsgs[0].ts == "1778667150.756969"
    assert tmsgs[0].actor_id == "U0EXAMPLE"
    assert tmsgs[0].actor_name == "Ivan Example"
    assert "Frank" in tmsgs[0].body
    assert "THREAD REPLIES" not in tmsgs[0].body, "noise leaked into parent body"
    assert tmsgs[1].ts == "1778695173.495759"
    assert tmsgs[1].actor_id == "U0MENTION"
    assert tmsgs[2].reactions_json == '{"+1": 1}', f"reactions parse off: {tmsgs[2].reactions_json}"
    print("  ✓ parse_mcp_messages — thread parent + 2 replies, bare-paren reactions")

    # No-reply thread variant (parent only, "No thread messsages" footer).
    sample_thread_no_replies = """=== THREAD PARENT MESSAGE ===
From: Owner Example (U0EXAMPLE)
Time: 2026-05-14 09:19:36 IST
Message TS: 1778730576.855459
<!channel> feeling feverish, taking the day off
Reactions: takecare (9)

No thread messsages
"""
    nmsgs = parse_mcp_messages(sample_thread_no_replies)
    assert len(nmsgs) == 1
    assert nmsgs[0].body.endswith("the day off"), f"body trimmed wrong: {nmsgs[0].body!r}"
    assert "No thread" not in nmsgs[0].body
    assert nmsgs[0].reactions_json == '{"takecare": 9}'
    print("  ✓ parse_mcp_messages — parent-only thread with no replies")

    # --- ID / subject scheme ---
    eid_top = _event_id("C01", "1778.5", None)
    eid_top_self = _event_id("C01", "1778.5", "1778.5")  # parent == own ts
    eid_reply = _event_id("C01", "1779.6", "1778.5")
    assert eid_top == "slack:C01:1778.5"
    assert eid_top_self == "slack:C01:1778.5"
    assert eid_reply == "slack:C01:1778.5:1779.6"
    sub_top = _subject("C01", "1778.5", None)
    sub_reply = _subject("C01", "1779.6", "1778.5")
    assert sub_top == "slack:C01:1778.5"
    assert sub_reply == "slack:C01:1778.5"
    print("  ✓ event_id + subject scheme")

    # --- DM detection ---
    assert is_dm_channel({"is_im": True})
    assert is_dm_channel({"is_mpim": True})
    assert not is_dm_channel({"is_private": True})
    assert not is_dm_channel({"is_im": False, "is_mpim": False})
    print("  ✓ is_dm_channel")

    # --- Cursor extraction ---
    info = "There are more messages available. To view the next page, use cursor: `bmV4dF90czoxNzc4`\n"
    assert extract_cursor(info) == "bmV4dF90czoxNzc4"
    assert extract_cursor("End of results - No more pages available.\n") is None
    print("  ✓ extract_cursor")

    # --- UPSERT round-trip on in-memory DB ---
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE events (
            id TEXT PRIMARY KEY, source TEXT, event_type TEXT, ts TEXT, actor TEXT,
            subject TEXT, title TEXT, body TEXT, url TEXT, raw_path TEXT,
            channel_id TEXT, thread_ts TEXT, edited_ts TEXT, deleted_ts TEXT,
            reactions_json TEXT, reply_count INTEGER, files_json TEXT
        );
        CREATE TABLE event_refs (
            event_id TEXT NOT NULL, ref_type TEXT NOT NULL, ref_value TEXT NOT NULL,
            PRIMARY KEY (event_id, ref_type, ref_value)
        );
    """)

    res = upsert_messages(conn, msgs, "C0EXAMPLE")
    assert res.inserted == 3, f"insert count off: {res}"
    assert res.errors == [], f"errors: {res.errors}"
    print(f"  ✓ upsert insert phase ({res.inserted} new)")

    # Re-run identical batch → all unchanged.
    res2 = upsert_messages(conn, msgs, "C0EXAMPLE")
    assert res2.inserted == 0 and res2.updated == 0, f"expected 0+0, got {res2}"
    print("  ✓ upsert idempotent on re-run")

    # Simulate an edit — bump body, set edited=True.
    msgs[0].body += "\n(appended edit)"
    msgs[0].edited = True
    res3 = upsert_messages(conn, [msgs[0]], "C0EXAMPLE")
    assert res3.updated == 1, f"expected 1 update, got {res3}"
    edited_ts = conn.execute("SELECT edited_ts FROM events WHERE id = ?",
                              (_event_id("C0EXAMPLE", msgs[0].ts, None),)).fetchone()
    assert edited_ts[0] is not None, "edited_ts not set"
    print("  ✓ upsert edit path")

    # Reconcile: drop msg[2] from api_msgs → expect tombstone.
    surviving = [msgs[0], msgs[1]]
    rc = reconcile_window(conn, "C0EXAMPLE",
                          window_start_iso=_ts_to_iso("1778000000.0"),
                          api_msgs=surviving)
    assert rc["tombstoned"] == 1, f"expected 1 tombstone, got {rc}"
    deleted = conn.execute("SELECT deleted_ts FROM events WHERE id = ?",
                            (_event_id("C0EXAMPLE", msgs[2].ts, None),)).fetchone()
    assert deleted[0] is not None
    print("  ✓ reconcile_window tombstones missing msg")

    # --- Ref extraction round-trip ---
    # New message referencing a ticket, a PR URL, a confluence page, a Slack thread,
    # and a known + unknown <@U…> mention.
    conn2 = sqlite3.connect(":memory:")
    conn2.executescript("""
        CREATE TABLE events (
            id TEXT PRIMARY KEY, source TEXT, event_type TEXT, ts TEXT, actor TEXT,
            subject TEXT, title TEXT, body TEXT, url TEXT, raw_path TEXT,
            channel_id TEXT, thread_ts TEXT, edited_ts TEXT, deleted_ts TEXT,
            reactions_json TEXT, reply_count INTEGER, files_json TEXT
        );
        CREATE TABLE event_refs (
            event_id TEXT NOT NULL, ref_type TEXT NOT NULL, ref_value TEXT NOT NULL,
            PRIMARY KEY (event_id, ref_type, ref_value)
        );
    """)
    ref_msg = ParsedMessage(
        actor_id="U0EXAMPLE",
        actor_name="Owner Example",
        ts="1779000000.000001",
        body=(
            "<@U0MENTION|Ivan> see EX-2660 — fix in "
            "https://github.com/example-org/service-a/pull/629 and "
            "Confluence /pages/300000000 + thread "
            "https://example.slack.com/archives/C0EXAMPLE/p1778667150756969"
        ),
        is_bot=False,
        edited=False,
    )
    cache = {"U0MENTION": "ivan-example"}  # known mention
    res_ref = upsert_messages(conn2, [ref_msg], "C0EXAMPLE",
                               slack_users_cache=cache)
    assert res_ref.inserted == 1, f"expected 1 insert, got {res_ref}"

    rows = conn2.execute(
        "SELECT ref_type, ref_value FROM event_refs ORDER BY ref_type, ref_value"
    ).fetchall()
    by_type: dict[str, set] = {}
    for rt, rv in rows:
        by_type.setdefault(rt, set()).add(rv)

    assert by_type.get("ticket") == {"EX-2660"}, f"ticket refs: {by_type.get('ticket')}"
    assert by_type.get("pull_request") == {"example-org/service-a#629"}, f"PR refs: {by_type.get('pull_request')}"
    assert by_type.get("page") == {"300000000"}, f"page refs: {by_type.get('page')}"
    assert "ivan-example" in (by_type.get("person") or set()), f"person refs: {by_type.get('person')}"
    # Slack thread URL with ts 1778667150756969 → reconstructed 1778667150.756969
    assert by_type.get("slack_thread") == {"slack:C0EXAMPLE:1778667150.756969"}, \
        f"slack_thread refs: {by_type.get('slack_thread')}"
    print("  ✓ enrich_refs extracts ticket / PR / page / slack_thread / person")

    print("\nall self-tests pass")


if __name__ == "__main__":
    _selftest()

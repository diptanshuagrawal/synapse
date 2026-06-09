#!/usr/bin/env python3
"""
slack_ingest_runner.py — invoked by .claude/commands/slack-{backfill,ingest,reconcile}.md.

Takes a Slack MCP `slack_read_channel` or `slack_read_thread` "detailed" text
response from a file (or stdin), parses it via derive.slack_upsert, upserts
into events.db, advances the per-channel cursor in state/slack_cursors.json.

Skills make the MCP call (Claude session is the only place MCP works) and
hand the response text here. Separation of concerns: skills orchestrate +
call MCP; this runner owns SQL + state mutation.

Usage:
    .venv/bin/python derive/slack_ingest_runner.py upsert \\
        --channel-id C0… \\
        --response-file /tmp/slack_response_<channel>.txt \\
        [--thread-parent-ts 1234.5678]    # set when response is a thread-replies fetch

    .venv/bin/python derive/slack_ingest_runner.py advance-cursor \\
        --channel-id C0… \\
        --new-cursor-ts 1778667150.756969

    .venv/bin/python derive/slack_ingest_runner.py read-cursor --channel-id C0…
    .venv/bin/python derive/slack_ingest_runner.py status      # print state snapshot

Output: structured JSON to stdout for skill to consume.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db, _load_people, append_raw, Event  # noqa: E402
from derive.slack_upsert import (  # noqa: E402
    parse_mcp_messages, upsert_messages, extract_cursor, _event_id,
    _subject, _ts_to_iso,
)

ROOT = _PKG_ROOT
CURSORS_PATH = ROOT / "state" / "slack_cursors.json"
ROUTINE_STATUS_PATH = ROOT / "state" / "slack_routine_status.json"
CHANNEL_META_PATH = ROOT / "state" / "slack_channel_meta.json"
RESUME_STATE_PATH = ROOT / "state" / "slack_backfill_resume.json"

# Matches the "Thread: N replies" marker emitted in parent-message blocks by
# slack_read_channel detailed-format. Used to identify which parents actually
# have replies worth fetching (vs every top-level message which is technically
# a thread parent).
_THREAD_REPLIES_MARKER = re.compile(r"^Thread:\s+\d+\s+repl", re.MULTILINE)


# ── Slack users cache from people.yaml ──────────────────────────────────────


def _build_slack_users_cache() -> dict[str, str]:
    """Build {U-id: canonical} from people.yaml::slack_id.

    Compact + side-effect free. Re-built on every runner invocation;
    cost negligible (~10 entries).
    """
    cache: dict[str, str] = {}
    for p in _load_people():
        slack_id = p.get("slack_id")
        canonical = p.get("canonical")
        if slack_id and canonical:
            cache[slack_id] = canonical
    return cache


# ── Cursor state ────────────────────────────────────────────────────────────


def _read_cursors() -> dict:
    if not CURSORS_PATH.exists():
        return {}
    with open(CURSORS_PATH) as f:
        return json.load(f)


def _write_cursors(data: dict) -> None:
    CURSORS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CURSORS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _read_routine_status() -> dict:
    if not ROUTINE_STATUS_PATH.exists():
        return {}
    with open(ROUTINE_STATUS_PATH) as f:
        return json.load(f)


def _write_routine_status(data: dict) -> None:
    ROUTINE_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ROUTINE_STATUS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _read_resume_state() -> dict:
    if not RESUME_STATE_PATH.exists():
        return {"channels": {}, "active_channel": None}
    with open(RESUME_STATE_PATH) as f:
        return json.load(f)


def _write_resume_state(data: dict) -> None:
    RESUME_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESUME_STATE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _channel_meta(channel_id: str) -> dict:
    if not CHANNEL_META_PATH.exists():
        return {}
    with open(CHANNEL_META_PATH) as f:
        cache = json.load(f)
    return cache.get("channels", {}).get(channel_id, {})


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Commands ────────────────────────────────────────────────────────────────


def cmd_upsert(args: argparse.Namespace) -> None:
    """Parse MCP response file + upsert messages + persist raw JSONL."""

    # DM hard-skip defence in depth — check channel_meta.is_private/Channel Type.
    meta = _channel_meta(args.channel_id)
    # If meta exists, refuse if channel-type looks like a DM. (slack-discover
    # only writes non-DM channels to the cache, so this should never fire,
    # but defence in depth.)
    if meta:
        # Heuristic: name starting with 'D' or 'mpdm-' is a DM/MPDM.
        if meta.get("is_im") or meta.get("is_mpim"):
            print(json.dumps({"error": "refused_dm", "channel_id": args.channel_id}))
            sys.exit(2)

    # Read MCP response.
    response_path = Path(args.response_file)
    if not response_path.exists():
        print(json.dumps({"error": "response_file_missing", "path": str(response_path)}))
        sys.exit(2)
    text = response_path.read_text()

    # Strip the JSON wrapper that Claude tool calls return.
    # Format: {"results":"...","pagination_info":"..."}  OR  {"messages":"..."}
    # MCP may also wrap as [{"type":"text","text":"<inner JSON>"}]; unwrap that first.
    try:
        wrapper = json.loads(text)
        if isinstance(wrapper, list) and wrapper and isinstance(wrapper[0], dict) and "text" in wrapper[0]:
            inner = wrapper[0]["text"]
            try:
                wrapper = json.loads(inner)
            except (json.JSONDecodeError, ValueError):
                wrapper = {"messages": inner}
        body_text = wrapper.get("messages") or wrapper.get("results") or text
        pagination_info = wrapper.get("pagination_info", "")
    except (json.JSONDecodeError, ValueError):
        body_text = text
        pagination_info = ""

    # Parse messages.
    messages = parse_mcp_messages(body_text)
    if not messages:
        print(json.dumps({
            "channel_id": args.channel_id,
            "parsed": 0,
            "next_cursor": extract_cursor(pagination_info),
        }))
        return

    # Persist raw JSONL — write the wrapped MCP response as-is for replayability.
    for m in messages:
        ev = Event(
            id=_event_id(args.channel_id, m.ts, args.thread_parent_ts),
            source="slack",
            event_type=("thread_reply" if args.thread_parent_ts and args.thread_parent_ts != m.ts
                        else "thread_started"),
            ts=_ts_to_iso(m.ts),
            actor=m.actor_id,
            subject=_subject(args.channel_id, m.ts, args.thread_parent_ts),
            title=None,
            body=m.body,
            url=None,
        )
        try:
            append_raw(ev)  # writes to raw/slack/YYYY/MM/DD.jsonl
        except Exception as e:
            # Raw mirror failure shouldn't block DB write — log + continue.
            print(json.dumps({"warning": "raw_append_failed", "ts": m.ts, "err": str(e)}),
                  file=sys.stderr)

    # UPSERT into DB with Slack users cache for mention resolution.
    cache = _build_slack_users_cache()
    conn = get_db()
    result = upsert_messages(
        conn, messages, args.channel_id,
        thread_parent_ts=args.thread_parent_ts,
        slack_users_cache=cache,
    )

    # Advance cursor — set to the NEWEST message ts in this page.
    # (Slack returns newest-first; messages[0] is newest unless thread fetch.)
    if not args.thread_parent_ts:  # only advance cursor on channel-level reads
        newest_ts = max(m.ts for m in messages)
        cursors = _read_cursors()
        cur_existing = cursors.get(args.channel_id, "0")
        # Only advance forward (avoid backtracking on out-of-order fetches).
        if float(newest_ts) > float(cur_existing):
            cursors[args.channel_id] = newest_ts
            _write_cursors(cursors)

    # Identify only the parents that ACTUALLY have replies (via "Thread: N replies"
    # marker in raw block). Top-level msgs without replies are not worth a
    # slack_read_thread fetch.
    if args.thread_parent_ts:
        parents_with_replies: list[str] = []
    else:
        parents_with_replies = [
            m.ts for m in messages
            if (m.thread_parent_ts is None or m.thread_parent_ts == m.ts)
               and _THREAD_REPLIES_MARKER.search(m.raw_block or "")
        ]

    print(json.dumps({
        "channel_id": args.channel_id,
        "parsed": len(messages),
        "inserted": result.inserted,
        "updated": result.updated,
        "skipped": result.skipped,
        "errors": result.errors,
        "next_cursor": extract_cursor(pagination_info),
        "thread_parents_with_replies": parents_with_replies,
    }, indent=2))


def cmd_read_cursor(args: argparse.Namespace) -> None:
    cursors = _read_cursors()
    print(json.dumps({
        "channel_id": args.channel_id,
        "cursor_ts": cursors.get(args.channel_id),
    }))


def cmd_read_cursors_all(args: argparse.Namespace) -> None:
    """Emit {channel_id, name, class, cursor_ts} list for every configured channel.

    Replaces the shell `for ID in …; do read-cursor; done` loop in /slack-ingest
    so the skill doesn't depend on shell variable expansion (which the harness
    treats as security-sensitive even with broad allow-rules)."""
    cfg_path = ROOT / "config" / "slack_channels.yaml"
    with cfg_path.open() as f:
        cfg = yaml.safe_load(f)
    cursors = _read_cursors()
    out = []
    for c in cfg.get("channels", []):
        cid = c.get("id")
        if not cid or cid == "TODO":
            continue
        out.append({
            "id": cid,
            "name": c.get("name"),
            "class": c.get("class"),
            "cursor_ts": cursors.get(cid),
        })
    print(json.dumps(out, indent=2))


def cmd_advance_cursor(args: argparse.Namespace) -> None:
    cursors = _read_cursors()
    existing = cursors.get(args.channel_id, "0")
    if float(args.new_cursor_ts) <= float(existing):
        print(json.dumps({"warning": "cursor_not_advanced", "existing": existing,
                          "proposed": args.new_cursor_ts}))
        return
    cursors[args.channel_id] = args.new_cursor_ts
    _write_cursors(cursors)
    print(json.dumps({"channel_id": args.channel_id, "cursor_ts": args.new_cursor_ts}))


def cmd_status(args: argparse.Namespace) -> None:
    cursors = _read_cursors()
    status = _read_routine_status()
    print(json.dumps({
        "cursors": cursors,
        "routine_status": status,
    }, indent=2))


def cmd_record_fire(args: argparse.Namespace) -> None:
    """Write fire-result row to state/slack_routine_status.json for cron-status."""
    status = _read_routine_status()
    status["last_success_ts"] = _now_iso()
    if args.summary_json:
        status["last_fire"] = json.loads(args.summary_json)
    _write_routine_status(status)
    print(json.dumps(status, indent=2))


# ── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_upsert = sub.add_parser("upsert", help="Parse MCP response file + write events.db")
    p_upsert.add_argument("--channel-id", required=True)
    p_upsert.add_argument("--response-file", required=True)
    p_upsert.add_argument("--thread-parent-ts", default=None,
                          help="Set when ingesting a slack_read_thread response.")
    p_upsert.set_defaults(func=cmd_upsert)

    p_read = sub.add_parser("read-cursor")
    p_read.add_argument("--channel-id", required=True)
    p_read.set_defaults(func=cmd_read_cursor)

    p_read_all = sub.add_parser("read-cursors-all",
                                help="Emit cursor map for every channel in config/slack_channels.yaml")
    p_read_all.set_defaults(func=cmd_read_cursors_all)

    p_adv = sub.add_parser("advance-cursor")
    p_adv.add_argument("--channel-id", required=True)
    p_adv.add_argument("--new-cursor-ts", required=True)
    p_adv.set_defaults(func=cmd_advance_cursor)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=cmd_status)

    p_record = sub.add_parser("record-fire",
                              help="Update state/slack_routine_status.json post-fire.")
    p_record.add_argument("--summary-json", default=None)
    p_record.set_defaults(func=cmd_record_fire)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

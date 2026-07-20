#!/usr/bin/env python3
"""
signals.py — meeting-signal state manager (P4 of the meeting-intelligence PRD).

Holds the durable signal state extracted from meeting transcripts by the
/meeting-notes session: commitments (said-vs-done), asks directed at the
owner, and untracked-work candidates for /ticketize.

State: state/meeting_signals.json
  {"commitments": [{id, person, promise, due, subject, ts, offset, status,
                    resolved_ts, resolved_note}],
   "asks":        [{id, person, ask, subject, ts, offset, status}],
   "untracked":   [{id, person, work, subject, ts, offset, status}]}

Design rules:
  - Pure stdlib (json/sqlite3) — runs under ANY python3, no yaml/venv dance.
  - The LLM session supplies canonical person handles (it has people.yaml in
    context); this script never guesses identity.
  - Deterministic evidence CANDIDATES only: `check` lists a person's activity
    since the promise, it does NOT decide whether a promise was kept — that
    judgement happens in-session (chat-only classification policy).
  - Append/update via atomic tmp+rename with a PID-suffix tmp (concurrent
    writer rule) — the standup routine and a /meeting-notes session may fire
    close together.

CLI:
  add <signals.json>      merge new signals (session-written file; dedup by id)
  check [--days N]        open commitments + evidence candidates since each
  list [--all]            current state (default: open items only)
  resolve <id> [note]     mark a commitment/ask/untracked item done
  gather-block <date>     the `# STANDUP CALL` block for standup_gather
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

WC = Path(__file__).resolve().parents[2]
STATE = WC / "state" / "meeting_signals.json"
DB = WC / "index" / "events.db"

KINDS = ("commitments", "asks", "untracked", "actions")


def _load() -> dict:
    if STATE.exists():
        with open(STATE) as f:
            data = json.load(f)
    else:
        data = {}
    for k in KINDS:
        data.setdefault(k, [])
    return data


def _save(data: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(f".json.tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)


def _mk_id(kind: str, person: str, text: str) -> str:
    return f"{kind[0]}-" + hashlib.sha1(f"{person}|{text}".encode()).hexdigest()[:10]


def cmd_add(path: str) -> None:
    with open(path) as f:
        incoming = json.load(f)
    data = _load()
    added = dup = 0
    for kind in KINDS:
        existing = {it["id"] for it in data[kind]}
        for it in incoming.get(kind, []):
            text = it.get("promise") or it.get("ask") or it.get("work") or it.get("action") or ""
            person = it.get("person") or it.get("assignee") or "(unattributed)"
            if not text:
                continue
            iid = it.get("id") or _mk_id(kind, person, text)
            if iid in existing:
                dup += 1
                continue
            it["id"] = iid
            it.setdefault("status", "open")
            it.setdefault("ts", datetime.now(timezone.utc).isoformat())
            data[kind].append(it)
            existing.add(iid)
            added += 1
    _save(data)
    print(f"OK added={added} duplicate={dup}")


def _person_activity(cur, person: str, since_iso: str, ticket: str | None, limit: int = 3):
    """Evidence candidates: the person's jira/github events since the promise.

    Matches by ref (event_refs person = canonical handle) — the same identity
    layer every ingest populates — optionally narrowed to the promised ticket.
    """
    q = (
        "SELECT e.ts, e.source, e.event_type, e.subject, COALESCE(e.to_status,''), "
        "COALESCE(substr(e.title,1,60),'') FROM events e "
        "JOIN event_refs r ON r.event_id = e.id AND r.ref_type='person' AND r.ref_value=? "
        "WHERE e.ts >= ? AND e.source IN ('jira','github') "
    )
    args: list = [person, since_iso]
    if ticket:
        q += "AND e.subject = ? "
        args.append(ticket)
    q += "ORDER BY e.ts DESC LIMIT ?"
    args.append(limit)
    return cur.execute(q, args).fetchall()


def _fmt_commit(it: dict) -> str:
    due = f" due={it['due']}" if it.get("due") else ""
    tick = f" ticket={it['ticket']}" if it.get("ticket") else ""
    return (f"  [{it['id']}] {it.get('person','?')} — \"{it.get('promise','')}\""
            f"{due}{tick} (from {it.get('subject','?')} @{it.get('offset','?')})")


def cmd_check(days: int = 14) -> None:
    data = _load()
    cur = sqlite3.connect(str(DB)).cursor()
    cur.connection.execute("PRAGMA busy_timeout = 30000")
    today = datetime.now(timezone.utc).date().isoformat()
    horizon = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    shown = 0
    for it in data["commitments"]:
        if it.get("status") != "open" or it.get("ts", "") < horizon:
            continue
        shown += 1
        overdue = it.get("due") and it["due"] < today
        print(_fmt_commit(it) + ("  ⚠️ OVERDUE" if overdue else ""))
        person = it.get("person") or ""
        if person and person != "(unattributed)":
            ev = _person_activity(cur, person, it.get("ts", ""), it.get("ticket"))
            if ev:
                for ts, src, et, sub, status, title in ev:
                    extra = f" →{status}" if status else (f" \"{title}\"" if title else "")
                    print(f"      evidence? {ts[:16]} {src}:{et} {sub}{extra}")
            else:
                print("      evidence? NONE since promise — candidate said-vs-done delta")
    if not shown:
        print("no open commitments in window")


def cmd_list(show_all: bool = False) -> None:
    data = _load()
    for kind in KINDS:
        items = [i for i in data[kind] if show_all or i.get("status") == "open"]
        if not items:
            continue
        print(f"# {kind} ({len(items)})")
        for it in items:
            text = it.get("promise") or it.get("ask") or it.get("work") or it.get("action") or ""
            who = it.get("person") or it.get("assignee") or "?"
            print(f"  [{it['id']}] ({it.get('status')}) {who} — {text}")


def cmd_resolve(iid: str, note: str = "") -> None:
    data = _load()
    for kind in KINDS:
        for it in data[kind]:
            if it["id"] == iid:
                it["status"] = "done"
                it["resolved_ts"] = datetime.now(timezone.utc).isoformat()
                if note:
                    it["resolved_note"] = note
                _save(data)
                print(f"OK resolved {iid} ({kind})")
                return
    sys.exit(f"ERROR: id not found: {iid}")


def cmd_gather_block(date_str: str) -> None:
    """The `# STANDUP CALL` block standup_gather embeds. Deterministic facts
    only; the /standup session does the judging + rendering."""
    data = _load()
    cur = sqlite3.connect(str(DB)).cursor()
    cur.connection.execute("PRAGMA busy_timeout = 30000")
    today = datetime.now(timezone.utc).date().isoformat()

    meetings = cur.execute(
        "SELECT subject, title, ts FROM events WHERE source='meeting' "
        "AND event_type='meeting_recorded' AND subject LIKE ? ORDER BY ts",
        (f"meeting:{date_str}:%",),
    ).fetchall()
    if meetings:
        for sub, title, ts in meetings:
            nseg = cur.execute(
                "SELECT COUNT(*) FROM events WHERE subject=? AND event_type='transcript_segment'",
                (sub,)).fetchone()[0]
            print(f"  MEETING {sub} \"{title}\" segments={nseg}")
    else:
        print(f"  (no meeting recordings ingested for {date_str})")

    open_c = [i for i in data["commitments"] if i.get("status") == "open"]
    if open_c:
        print(f"  OPEN COMMITMENTS ({len(open_c)}) — check each against the member's actual day (said-vs-done):")
        for it in open_c:
            overdue = it.get("due") and it["due"] < today
            print(_fmt_commit(it) + ("  ⚠️ OVERDUE" if overdue else ""))
            person = it.get("person") or ""
            if person and person != "(unattributed)":
                ev = _person_activity(cur, person, it.get("ts", ""), it.get("ticket"), limit=2)
                if ev:
                    for ts, src, et, sub, status, title in ev:
                        extra = f" →{status}" if status else ""
                        print(f"      evidence? {ts[:16]} {src}:{et} {sub}{extra}")
                else:
                    print("      evidence? NONE since promise")
    open_a = [i for i in data["asks"] if i.get("status") == "open"]
    if open_a:
        print(f"  OPEN MEETING ASKS→OWNER ({len(open_a)}) — surface in Your queue with (meeting) tag:")
        for it in open_a:
            print(f"  [{it['id']}] {it.get('person','?')} asked: \"{it.get('ask','')}\" (from {it.get('subject','?')})")
    open_u = [i for i in data["untracked"] if i.get("status") == "open"]
    if open_u:
        print(f"  UNTRACKED WORK MENTIONS ({len(open_u)}) — /ticketize candidates:")
        for it in open_u:
            print(f"  [{it['id']}] {it.get('person','?')} — {it.get('work','')} (from {it.get('subject','?')})")
    # Action items agreed in meetings, with an assignee. The standup session
    # routes them: assignee==owner → Your queue; assignee==teammate → route/
    # delegate bucket (or their §7c); (unassigned) → still surface to the owner.
    open_x = [i for i in data["actions"] if i.get("status") == "open"]
    if open_x:
        print(f"  MEETING ACTION ITEMS ({len(open_x)}) — route by assignee: owner→Your queue, teammate→route/delegate:")
        for it in open_x:
            due = f" due={it['due']}" if it.get("due") else ""
            print(f"  [{it['id']}] assignee={it.get('assignee','(unassigned)')} — \"{it.get('action','')}\"{due} (from {it.get('subject','?')})")
    if not (open_c or open_a or open_u or open_x or meetings):
        print("  (no meeting signals)")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "add" and len(sys.argv) == 3:
        cmd_add(sys.argv[2])
    elif cmd == "check":
        days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 14
        cmd_check(days)
    elif cmd == "list":
        cmd_list("--all" in sys.argv)
    elif cmd == "resolve" and len(sys.argv) >= 3:
        cmd_resolve(sys.argv[2], " ".join(sys.argv[3:]))
    elif cmd == "gather-block" and len(sys.argv) == 3:
        cmd_gather_block(sys.argv[2])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

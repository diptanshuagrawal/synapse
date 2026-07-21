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
  todos [--owner H]       owner-facing open to-dos as JSON (asks + owner's
                          actions/commitments + unassigned actions) + untracked
  resolve <id> [note]     mark a commitment/ask/untracked item done
  reopen <id>             flip a done item back to open (To-do un-check)
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

KINDS = ("commitments", "asks", "untracked", "actions", "suggestions")


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
            text = (it.get("promise") or it.get("ask") or it.get("work")
                    or it.get("action") or it.get("suggestion") or "")
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
            text = (it.get("promise") or it.get("ask") or it.get("work")
                    or it.get("action") or it.get("suggestion") or "")
            who = it.get("person") or it.get("assignee") or "?"
            print(f"  [{it['id']}] ({it.get('status')}) {who} — {text}")


# Assignee/person values that mean "nobody owns this yet" — an owner-facing
# to-do surfaces these (the owner still has to route them) but never guesses
# they belong to the owner.
UNASSIGNED = ("(unassigned)", "(unattributed)", "", None)

# The literal sentinel the /meeting-notes skill writes for the owner's own items
# (teammates get their canonical people.yaml handle; the owner gets "owner").
# gather-block routes on exactly this token ("assignee==owner → Your queue").
OWNER_TOKEN = "owner"


def _is_owner(value, owner_handle: str | None) -> bool:
    """True if this assignee/person is the owner — either the "owner" sentinel
    or (when resolvable) the owner's canonical handle."""
    return value == OWNER_TOKEN or (owner_handle is not None and value == owner_handle)


def owner_facing_todos(owner_handle: str | None, status: str = "open") -> list[dict]:
    """Items the OWNER personally has to act on, across all meetings.

    Attribution-honest — includes only:
      - actions whose assignee IS the owner ("owner" sentinel or canonical
        handle), or is unassigned (owner must route)
      - commitments the owner made (person == "owner"/canonical handle)
      - asks (every ask is directed at the owner by construction)
    Teammate-assigned actions and teammate commitments are NOT owner to-dos and
    are dropped here. `owner_handle` None (no people.yaml / unresolved) → owner
    matches nothing, so only unassigned actions + asks surface (never guessed).

    `status` selects which items to return ("open" for the live list; "done" for
    the Completed view). Returns normalized rows:
    {id, kind, text, who, subject, due, ts, offset, resolved_ts}.
    """
    data = _load()
    out: list[dict] = []

    def row(it: dict, kind: str, text_key: str, who_key: str) -> dict:
        return {
            "id": it["id"],
            "kind": kind,
            "text": it.get(text_key, ""),
            "who": it.get(who_key) or "(unassigned)",
            "subject": it.get("subject", ""),
            "due": it.get("due", ""),
            "ts": it.get("ts", ""),
            "offset": it.get("offset", ""),
            "resolved_ts": it.get("resolved_ts", ""),
        }

    for it in data["actions"]:
        if it.get("status") != status:
            continue
        assignee = it.get("assignee")
        if assignee in UNASSIGNED or _is_owner(assignee, owner_handle):
            out.append(row(it, "action", "action", "assignee"))
    for it in data["commitments"]:
        if it.get("status") != status:
            continue
        if _is_owner(it.get("person"), owner_handle):
            out.append(row(it, "commitment", "promise", "person"))
    for it in data["asks"]:
        if it.get("status") != status:
            continue
        out.append(row(it, "ask", "ask", "person"))
    return out


def owner_untracked() -> list[dict]:
    """Open untracked-work mentions — /ticketize fodder, NOT owner to-dos. Shown
    in the To-do view under a separate collapsed 'mentioned, not tracked' section."""
    return [
        {"id": it["id"], "kind": "untracked", "text": it.get("work", ""),
         "who": it.get("person") or "?", "subject": it.get("subject", ""),
         "ts": it.get("ts", "")}
        for it in _load()["untracked"] if it.get("status") == "open"
    ]


def _evidence_hint(cur, person: str, since_iso: str, ticket: str | None) -> str:
    """One-line said-vs-done hint for a follow-up: has the person actually moved
    on it since they said so? Deterministic — reads their jira/github events."""
    if not person or person in UNASSIGNED:
        return ""
    rows = _person_activity(cur, person, since_iso, ticket, limit=1)
    if not rows:
        return "no activity since"
    ts, src, et, sub, status, title = rows[0]
    if status:
        return f"→ {status} ({ts[:10]})"
    return f"{src}:{et} ({ts[:10]})"


def follow_up_items(owner_handle: str | None, status: str = "open",
                    with_evidence: bool = False) -> list[dict]:
    """Things OTHERS owe the owner — teammate-assigned actions + teammate
    commitments (someone said they'd do X; the owner tracks it). The inverse of
    owner_facing_todos: an item is a follow-up iff its owner is a *named* person
    who is NOT the owner (so "(unassigned)" and owner items are excluded).

    with_evidence attaches a said-vs-done hint per row (opens events.db once).
    """
    data = _load()

    def teammate(v) -> bool:
        return v not in UNASSIGNED and not _is_owner(v, owner_handle)

    out: list[dict] = []
    for it in data["actions"]:
        if it.get("status") == status and teammate(it.get("assignee")):
            out.append({"id": it["id"], "kind": "action", "text": it.get("action", ""),
                        "who": it.get("assignee"), "subject": it.get("subject", ""),
                        "due": it.get("due", ""), "ts": it.get("ts", ""),
                        "ticket": it.get("ticket", "")})
    for it in data["commitments"]:
        if it.get("status") == status and teammate(it.get("person")):
            out.append({"id": it["id"], "kind": "commitment", "text": it.get("promise", ""),
                        "who": it.get("person"), "subject": it.get("subject", ""),
                        "due": it.get("due", ""), "ts": it.get("ts", ""),
                        "ticket": it.get("ticket", "")})
    if with_evidence and out:
        try:
            cur = sqlite3.connect(str(DB)).cursor()
            cur.connection.execute("PRAGMA busy_timeout = 5000")
            for r in out:
                r["evidence"] = _evidence_hint(cur, r["who"], r.get("ts", ""), r.get("ticket") or None)
        except Exception:
            pass
    return out


def owner_suggestions(status: str = "open") -> list[dict]:
    """AI-inferred owner to-dos (STEP 5 `suggestions`) — proactive nudges beyond
    the explicit action items. Owner-facing by construction."""
    return [
        {"id": it["id"], "kind": "suggestion", "text": it.get("suggestion", ""),
         "who": "owner", "subject": it.get("subject", ""),
         "ts": it.get("ts", ""), "rationale": it.get("rationale", "")}
        for it in _load()["suggestions"] if it.get("status") == status
    ]


def cmd_todos(owner_handle: str | None) -> None:
    """Owner-facing to-dos as JSON (for /ask + the Steno To-do view)."""
    print(json.dumps({
        "items": owner_facing_todos(owner_handle),
        "follow_up": follow_up_items(owner_handle, with_evidence=True),
        "suggestions": owner_suggestions(),
        "done": owner_facing_todos(owner_handle, status="done"),
        "untracked": owner_untracked(),
    }, indent=2))


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


def cmd_reopen(iid: str) -> None:
    """Un-resolve: flip a done item back to open (the To-do Completed view's
    un-check). Idempotent; clears the resolved_ts/note stamps."""
    data = _load()
    for kind in KINDS:
        for it in data[kind]:
            if it["id"] == iid:
                it["status"] = "open"
                it.pop("resolved_ts", None)
                it.pop("resolved_note", None)
                _save(data)
                print(f"OK reopened {iid} ({kind})")
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
    elif cmd == "todos":
        owner = sys.argv[sys.argv.index("--owner") + 1] if "--owner" in sys.argv else None
        cmd_todos(owner)
    elif cmd == "resolve" and len(sys.argv) >= 3:
        cmd_resolve(sys.argv[2], " ".join(sys.argv[3:]))
    elif cmd == "reopen" and len(sys.argv) == 3:
        cmd_reopen(sys.argv[2])
    elif cmd == "gather-block" and len(sys.argv) == 3:
        cmd_gather_block(sys.argv[2])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

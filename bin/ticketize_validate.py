#!/usr/bin/env python3
"""ticketize_validate.py — DETERMINISTIC guard for ticketize candidate files.

Two invariants, checked against ground truth (people.yaml + events.db), so a
confabulated reporter or a mis-grounded evidence link can never reach the
Approve/Reject card. This is the *enforcement* layer behind the prose rules in
`.claude/commands/ticketize.md` — a regex/DB check, not model trust:

  1. reporter NAME matches people.yaml for its slack_id — catches an invented name
     (e.g. a guessed human name for a bare `from=<id>` the model couldn't resolve).
     FULLY deterministic: an invented/wrong name is always blocked.
  2. reporter's slack_id actually took part in the evidence Slack thread — catches a
     grossly wrong link (reporter never in that thread). LIMIT: it canNOT catch a link
     to a thread the reporter IS in but whose TOPIC differs from the summary — that is a
     semantic match, which stays a model judgment (the `evidence`-rule prose in
     `.claude/commands/ticketize.md`). So this is a partial, not total, evidence guard.

Born 2026-06-24: a DETECT pass guessed a reporter's name (fully closed here) and pasted
an off-topic evidence link the reporter happened to be in (only partially catchable).
`standup_gather` now resolves IDs->names at the source; this is the downstream gate so
the name invariant doesn't rely on the model following a rule.

CLI:  ticketize_validate.py <YYYY-MM-DD>   # validate one day's candidate file
      ticketize_validate.py --all          # validate every open candidate file
Exit 0 = clean, 1 = violations (printed to stderr), 2 = usage.
Importable: `violations(cands)` self-loads people.yaml + events.db (fail-soft).
"""
from __future__ import annotations

import glob
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PEOPLE = os.path.join(ROOT, "work-context/config/people.yaml")
DB = os.path.join(ROOT, "work-context/index/events.db")
STANDUP = os.path.join(ROOT, "management/standup")

# Same candidate-md grammar as relay_bot.parse_candidates (kept local to avoid a
# circular import: relay_bot imports THIS module for enforcement).
HEAD_RE = re.compile(r"^##\s+([A-Z]\d+)\s+·\s+(.*?)\s*(?:—\s*(\w+).*)?$")
FIELD_RE = re.compile(r"^-\s+([a-z_]+):\s*(.*?)\s*(?:#.*)?$")
SLACK_RE = re.compile(r"slack\.com/archives/([A-Z0-9]+)/p(\d{16,})")
REPORTER_ID_RE = re.compile(r"^(U[A-Z0-9]+)\s*\((.+?)\)\s*$")


def parse_candidates_file(path):
    """Return one dict per candidate block (label + every `- field:` value)."""
    out, cur, last = [], None, None
    with open(path) as fh:
        for raw in fh:
            line = raw.rstrip()
            h = HEAD_RE.match(line)
            if h:
                if cur:
                    out.append(cur)
                cur = {"label": h.group(1), "name": h.group(2).strip()}
                last = None
                continue
            if cur is None:
                continue
            f = FIELD_RE.match(line)
            if f:
                cur[f.group(1)] = f.group(2).strip()
                last = f.group(1)
            elif last and raw.startswith(("  ", "\t")) and line.strip():
                cur[last] += " " + line.strip()  # wrapped field value
            else:
                last = None
    if cur:
        out.append(cur)
    return out


def load_people(people_path=PEOPLE):
    """slack_id->{name,canon} and canonical->{name,sid} across ALL scopes."""
    import yaml
    with open(people_path) as fh:
        d = yaml.safe_load(fh)
    people = d.get("people", d)
    by_id, by_canon = {}, {}
    for p in (people if isinstance(people, list) else people.values()):
        if not isinstance(p, dict):
            continue
        sid, canon, name = p.get("slack_id"), p.get("canonical"), p.get("name")
        if sid:
            by_id[sid] = {"name": name, "canon": canon}
        if canon:
            by_canon[canon] = {"name": name, "sid": sid}
    return by_id, by_canon


def thread_authors(conn, ch, ts):
    """Distinct slack actors in the thread the evidence permalink points at —
    root (id == slack:ch:ts), every reply (thread_ts == ts), or a reply whose own
    ts == ts. Empty set => thread not ingested (caller skips the check, fail-soft)."""
    cur = conn.execute(
        "SELECT DISTINCT actor FROM events WHERE source='slack' AND channel_id=? "
        "AND (thread_ts=? OR id=? OR id LIKE ? OR id LIKE ?)",
        (ch, ts, f"slack:{ch}:{ts}", f"slack:{ch}:{ts}:%", f"slack:{ch}:%:{ts}"),
    )
    return {r[0] for r in cur.fetchall() if r[0]}


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def validate_candidates(cands, by_id, by_canon, conn):
    """Return a list of violation strings (empty = clean). Only still-`pending`
    candidates are gated; `created`/`rejected` rows are left alone."""
    out = []
    for c in cands:
        label = c.get("label", "?")
        if _norm(c.get("decision")) not in ("", "pending"):
            continue
        reporter = (c.get("reporter") or "").strip()
        rid = None
        m = REPORTER_ID_RE.match(reporter)
        if m:
            rid, claimed = m.group(1), m.group(2).strip()
            rec = by_id.get(rid)
            if not rec:
                out.append(f"{label}: reporter id {rid} not in people.yaml — cannot verify name {claimed!r}")
            elif rec["name"] and _norm(rec["name"]) != _norm(claimed):
                out.append(f"{label}: reporter name {claimed!r} != people.yaml {rec['name']!r} for {rid} (invented name?)")
        elif reporter:
            rec = by_canon.get(reporter.split()[0])
            if not rec:
                out.append(f"{label}: reporter {reporter!r} is neither 'Uxxx (Name)' nor a known people.yaml canonical")
            else:
                rid = rec["sid"]
        else:
            out.append(f"{label}: no reporter set")

        ev = c.get("evidence", "") or ""
        sm = SLACK_RE.search(ev)
        if sm and rid and conn is not None:
            ch, digits = sm.group(1), sm.group(2)
            ts = digits[:10] + "." + digits[10:]
            authors = thread_authors(conn, ch, ts)
            if authors and rid not in authors:
                out.append(
                    f"{label}: reporter {rid} is NOT an author in evidence thread {ch}/{ts} "
                    f"— wrong / mis-grounded link?"
                )
    return out


def violations(cands):
    """Self-loading wrapper for callers (e.g. relay_bot before posting): loads
    people.yaml + a read-only events.db, fail-soft if the DB is unavailable."""
    by_id, by_canon = load_people()
    conn = None
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    except Exception:
        conn = None
    try:
        return validate_candidates(cands, by_id, by_canon, conn)
    finally:
        if conn:
            conn.close()


def _candidate_files(arg):
    if arg == "--all":
        return sorted(glob.glob(os.path.join(STANDUP, "*", "ticket-candidates.md")), reverse=True)
    p = os.path.join(STANDUP, arg, "ticket-candidates.md")
    return [p] if os.path.exists(p) else []


def main(argv):
    if len(argv) != 1:
        print("usage: ticketize_validate.py <YYYY-MM-DD> | --all", file=sys.stderr)
        return 2
    files = _candidate_files(argv[0])
    if not files:
        print(f"no candidate file for {argv[0]} — nothing to validate", file=sys.stderr)
        return 0
    cands = []
    for f in files:
        cands += parse_candidates_file(f)
    viol = violations(cands)
    if viol:
        print(f"TICKETIZE VALIDATION FAILED ({len(viol)} issue(s)):", file=sys.stderr)
        for x in viol:
            print("  - " + x, file=sys.stderr)
        return 1
    print(f"ticketize validation OK — {len(cands)} candidate(s) clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

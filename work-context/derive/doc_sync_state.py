#!/usr/bin/env python3
"""doc_sync_state — state + Slack-message rendering for the doc-sync automation.

This module is NETWORK-FREE on purpose. All Confluence / code-graph / Slack calls
are done by the owning skill (chat, via MCP); this module only persists what chat
found and renders the two Slack messages from that state.

State lives in state/doc_sync.db. One row per inline comment the doc-sync sweep
posted — tracked by Confluence comment_id so the pending digest counts ONLY our
own drift comments, never the unrelated review comments already on a page.

CLI:
  init                              create schema
  record   --file batch.json        upsert comments (chat writes after posting)
  set-status --file statuses.json   update resolution_status for tracked ids
  render-sweep  --run-id 2026-06 --date "11 Jun 2026"
  render-digest --date "Wed 11 Jun 2026"
  list     [--open]                 dump tracked comments
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "state", "doc_sync.db")
PEOPLE_YAML = os.path.join(ROOT, "config", "people.yaml")

SEV_ICON = {"major": "⚠️", "medium": "▫️", "minor": "🔹"}
SEV_RANK = {"major": 0, "medium": 1, "minor": 2}

SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_sync_comments (
    comment_id        TEXT PRIMARY KEY,
    finding_key       TEXT,            -- stable dedup key: page_id|check_type|norm(anchor)
    page_id           TEXT NOT NULL,
    page_title        TEXT NOT NULL,
    page_url          TEXT NOT NULL,
    comment_url       TEXT NOT NULL,
    owner_account     TEXT,            -- Confluence/Jira account id (people.yaml jira_id)
    severity          TEXT,            -- major | medium | minor
    check_type        TEXT,            -- schema | behavior | decision | dependency | lld | sequence
    finding_title     TEXT NOT NULL,   -- short headline shown in Slack
    anchor            TEXT,            -- inline text the comment is anchored to
    created_ts        TEXT,
    resolution_status TEXT DEFAULT 'open',  -- open | resolved | dangling | reopened
    last_checked_ts   TEXT,
    sweep_run_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_docsync_status  ON doc_sync_comments(resolution_status);
CREATE INDEX IF NOT EXISTS idx_docsync_owner   ON doc_sync_comments(owner_account);
"""


def _finding_key(page_id, check_type, anchor):
    """Stable identity of a finding so a sweep never re-posts the same comment.
    Anchor is normalised (lowercased, whitespace-collapsed) so trivial text shifts
    don't mint a new key."""
    norm = re.sub(r"\s+", " ", (anchor or "").strip().lower())
    raw = f"{page_id}|{(check_type or '').lower()}|{norm}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _ensure_schema(c):
    c.executescript(SCHEMA)
    # migrate: add finding_key to a pre-existing db, then backfill
    cols = {r[1] for r in c.execute("PRAGMA table_info(doc_sync_comments)")}
    if "finding_key" not in cols:
        c.execute("ALTER TABLE doc_sync_comments ADD COLUMN finding_key TEXT")
    c.execute("CREATE INDEX IF NOT EXISTS idx_docsync_finding ON doc_sync_comments(finding_key)")
    for r in c.execute(
        "SELECT comment_id, page_id, check_type, anchor FROM doc_sync_comments "
        "WHERE finding_key IS NULL"
    ).fetchall():
        c.execute(
            "UPDATE doc_sync_comments SET finding_key=? WHERE comment_id=?",
            (_finding_key(r["page_id"], r["check_type"], r["anchor"]), r["comment_id"]),
        )


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    return c


def _people_map():
    """jira_id -> {name, slack_id, slack_handle}. Single source of truth = people.yaml."""
    try:
        import yaml
    except ImportError:
        return {}
    if not os.path.exists(PEOPLE_YAML):
        return {}
    with open(PEOPLE_YAML) as f:
        data = yaml.safe_load(f) or {}
    out = {}
    for p in data.get("people", []):
        jid = p.get("jira_id")
        if jid:
            out[jid] = {
                "name": p.get("name", jid),
                "slack_id": p.get("slack_id"),
                "slack_handle": p.get("slack_handle"),
            }
    return out


def _mention(account, pmap):
    info = pmap.get(account or "")
    if info and info.get("slack_id"):
        return f"<@{info['slack_id']}>"
    if info and info.get("name"):
        return f"**{info['name']}**"
    return "**(unknown owner)**"


def _owner_name(account, pmap):
    info = pmap.get(account or "")
    return info.get("name") if info else (account or "unknown")


def cmd_init(_args):
    with _conn():
        pass
    print(f"initialised {DB_PATH}")


def cmd_record(args):
    with open(args.file) as f:
        rows = json.load(f)
    if isinstance(rows, dict):
        rows = rows.get("comments", [])
    cols = [
        "comment_id", "finding_key", "page_id", "page_title", "page_url", "comment_url",
        "owner_account", "severity", "check_type", "finding_title", "anchor",
        "created_ts", "resolution_status", "last_checked_ts", "sweep_run_id",
    ]
    n = 0
    with _conn() as c:
        for r in rows:
            r.setdefault(
                "finding_key",
                _finding_key(r.get("page_id"), r.get("check_type"), r.get("anchor")),
            )
            vals = [r.get(k) for k in cols]
            placeholders = ",".join("?" for _ in cols)
            updates = ",".join(f"{k}=excluded.{k}" for k in cols if k != "comment_id")
            c.execute(
                f"INSERT INTO doc_sync_comments ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(comment_id) DO UPDATE SET {updates}",
                vals,
            )
            n += 1
    print(f"recorded {n} comment(s)")


def cmd_set_status(args):
    with open(args.file) as f:
        rows = json.load(f)
    if isinstance(rows, dict):
        rows = rows.get("statuses", [])
    n = 0
    with _conn() as c:
        for r in rows:
            c.execute(
                "UPDATE doc_sync_comments SET resolution_status=?, last_checked_ts=? "
                "WHERE comment_id=?",
                (r.get("resolution_status"), r.get("last_checked_ts"), r["comment_id"]),
            )
            n += c.rowcount
    print(f"updated {n} status row(s)")


def _fetch(open_only=False, run_id=None):
    q = "SELECT * FROM doc_sync_comments"
    conds, params = [], []
    if open_only:
        conds.append("resolution_status IN ('open','reopened')")
    if run_id:
        conds.append("sweep_run_id=?")
        params.append(run_id)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def cmd_render_sweep(args):
    pmap = _people_map()
    rows = _fetch(run_id=args.run_id) if args.run_id else _fetch()
    # group by (owner, page)
    by_owner = {}
    for r in rows:
        by_owner.setdefault(r["owner_account"], {}).setdefault(
            (r["page_title"], r["page_url"]), []
        ).append(r)
    n_docs = len({r["page_id"] for r in rows})
    out = []
    out.append(f"**📋 Monthly doc-drift sweep — {args.run_id or 'latest'}**")
    out.append(
        f"{n_docs} doc(s) checked · {len(rows)} finding(s) flagged. "
        "Owners tagged inline — please review and resolve each comment thread."
    )
    out.append("")
    for account, pages in sorted(
        by_owner.items(), key=lambda kv: _owner_name(kv[0], pmap)
    ):
        for (title, _url), items in pages.items():
            items.sort(key=lambda r: SEV_RANK.get(r["severity"], 9))
            out.append(f"{_mention(account, pmap)} — **{title}** ({len(items)})")
            for it in items:
                icon = SEV_ICON.get(it["severity"], "•")
                out.append(f"• {icon} {it['finding_title']} — [comment]({it['comment_url']})")
            out.append("")
    out.append(f"cc {_mention(args.cc, pmap) if args.cc else ''}".rstrip())
    print("\n".join(out))


def cmd_render_digest(args):
    pmap = _people_map()
    rows = _fetch(open_only=True)
    by_owner = {}
    for r in rows:
        by_owner.setdefault(r["owner_account"], []).append(r)
    out = []
    out.append(f"**🔁 Pending doc-review — {args.date or 'today'}**")
    if not rows:
        out.append("No open doc-drift review threads. 🎉")
        print("\n".join(out))
        return
    out.append(
        f"{len(rows)} open review thread(s) from the doc-drift sweeps. "
        "Resolve the thread once addressed and it drops off this list."
    )
    out.append("")
    for account, items in sorted(
        by_owner.items(), key=lambda kv: (-len(kv[1]), _owner_name(kv[0], pmap))
    ):
        items.sort(key=lambda r: SEV_RANK.get(r["severity"], 9))
        out.append(f"{_mention(account, pmap)} — {len(items)} open")
        for it in items:
            out.append(
                f"• {it['page_title']}: {it['finding_title']} — [comment]({it['comment_url']})"
            )
        out.append("")
    out.append(f"cc {_mention(args.cc, pmap) if args.cc else ''}".rstrip())
    print("\n".join(out))


def cmd_filter_new(args):
    """Dedup gate. Input: candidate findings [{page_id, check_type, anchor, ...}].
    Output: only the candidates with NO existing tracked comment for their finding_key,
    so a sweep can never re-post a comment it already raised.

    Default suppresses against ANY status (open/reopened/resolved) — a finding raised
    once is not raised again. Pass --allow-resolved-reflag to re-raise findings whose
    only prior comment was resolved (drift persisted after the thread was closed)."""
    with open(args.file) as f:
        cands = json.load(f)
    if isinstance(cands, dict):
        cands = cands.get("candidates", cands.get("findings", []))
    with _conn() as c:
        rows = c.execute(
            "SELECT finding_key, resolution_status FROM doc_sync_comments"
        ).fetchall()
    suppress = {}
    for r in rows:
        suppress.setdefault(r["finding_key"], set()).add(r["resolution_status"])
    new, skipped = [], []
    for cand in cands:
        fk = cand.get("finding_key") or _finding_key(
            cand.get("page_id"), cand.get("check_type"), cand.get("anchor")
        )
        cand["finding_key"] = fk
        statuses = suppress.get(fk)
        if statuses is None:
            new.append(cand)
        elif args.allow_resolved_reflag and statuses <= {"resolved"}:
            new.append(cand)  # only resolved priors, and re-flag allowed
        else:
            skipped.append({"finding_key": fk, "page_id": cand.get("page_id"),
                            "anchor": cand.get("anchor"), "prior_status": sorted(statuses)})
    out = {"new": new, "skipped": skipped,
           "summary": {"candidates": len(cands), "new": len(new), "skipped": len(skipped)}}
    if args.out:
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2)
    print(json.dumps(out["summary"]))
    if skipped:
        sys.stderr.write(f"deduped {len(skipped)} already-tracked finding(s)\n")


def cmd_list(args):
    rows = _fetch(open_only=args.open)
    for r in rows:
        print(f"{r['comment_id']}  {r['resolution_status']:9}  "
              f"{r['severity']:6}  {r['page_title'][:30]:30}  {r['finding_title']}")
    print(f"\n{len(rows)} comment(s)")


def main():
    ap = argparse.ArgumentParser(description="doc-sync state + Slack renderer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    p = sub.add_parser("record"); p.add_argument("--file", required=True); p.set_defaults(fn=cmd_record)
    p = sub.add_parser("set-status"); p.add_argument("--file", required=True); p.set_defaults(fn=cmd_set_status)
    p = sub.add_parser("render-sweep")
    p.add_argument("--run-id"); p.add_argument("--date"); p.add_argument("--cc")
    p.set_defaults(fn=cmd_render_sweep)
    p = sub.add_parser("render-digest")
    p.add_argument("--date"); p.add_argument("--cc")
    p.set_defaults(fn=cmd_render_digest)
    p = sub.add_parser("filter-new")
    p.add_argument("--file", required=True); p.add_argument("--out")
    p.add_argument("--allow-resolved-reflag", action="store_true")
    p.set_defaults(fn=cmd_filter_new)
    p = sub.add_parser("list"); p.add_argument("--open", action="store_true"); p.set_defaults(fn=cmd_list)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

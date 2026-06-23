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


_DEDUP_STOP = set(
    "the a an of for to in on is are be by with and or not but this that it its as at from has "
    "have was were will would can could should into per via vs no than then so we you they them "
    "our your their doc page api table tables involved code new value still now".split()
)
_DEDUP_DOMAIN = {"bsbda", "tds", "gst", "cgst", "sgst", "igst", "utgst", "upi", "atm", "casa",
                 "rrn", "dcms", "ift", "neft", "rtgs", "imps", "ledger", "occ"}


def _dedup_tokens(title, anchor):
    """(all_tokens, strong_tokens). strong = snake_case identifiers + domain acronyms."""
    s = re.sub(r"[^a-z0-9_ ]", " ", f"{title or ''} {anchor or ''}".lower())
    allt = {w for w in s.split() if len(w) >= 3 and w not in _DEDUP_STOP}
    strong = {t for t in allt if "_" in t or t in _DEDUP_DOMAIN}
    snake = {t for t in strong if "_" in t}
    return allt, strong, snake


def _fuzzy_dup(cand, rows):
    """Compare a candidate against existing same-page rows. Returns
    (kind, row) where kind ∈ {"hard","soft",None}:
      hard → shares a snake_case identifier AND Jaccard≥0.25 → treat as already-raised (skip).
      soft → shares a domain acronym OR Jaccard≥0.30 → KEEP but flag possible_dup_of (human decides).
    Conservative on purpose: a hard skip silently drops a finding, so it needs a specific
    shared identifier; everything fuzzier is surfaced, never dropped."""
    ca, cs, csnake = _dedup_tokens(cand.get("finding_title"), cand.get("anchor"))
    if not ca:
        return None, None
    best = (0.0, None, None)
    for r in rows:
        if str(r["page_id"]) != str(cand.get("page_id")):
            continue
        ra, rs, rsnake = _dedup_tokens(r["finding_title"], r["anchor"])
        if not ra:
            continue
        j = len(ca & ra) / len(ca | ra)
        shared_snake = csnake & rsnake
        shared_strong = cs & rs
        if shared_snake and j >= 0.25:
            return "hard", r
        score = j + (0.15 if shared_strong else 0)
        if (shared_strong or j >= 0.30) and score > best[0]:
            best = (score, r, j)
    if best[1] is not None and (best[2] >= 0.30 or (cs & _dedup_tokens(best[1]["finding_title"], best[1]["anchor"])[1])):
        return "soft", best[1]
    return None, None


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
            cur = c.execute(
                "UPDATE doc_sync_comments SET resolution_status=?, last_checked_ts=? "
                "WHERE comment_id=?",
                (r.get("resolution_status"), r.get("last_checked_ts"), r["comment_id"]),
            )
            n += cur.rowcount
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
    out.append(
        "_Please resolve each comment — either after making the change, or by "
        "replying with a reason to reject and then marking it resolved._"
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
            "SELECT comment_id, finding_key, page_id, check_type, finding_title, anchor, "
            "resolution_status FROM doc_sync_comments"
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
        # 1) exact-key match → already raised (unchanged behaviour)
        if statuses is not None and not (args.allow_resolved_reflag and statuses <= {"resolved"}):
            skipped.append({"finding_key": fk, "page_id": cand.get("page_id"),
                            "anchor": cand.get("anchor"), "prior_status": sorted(statuses),
                            "dup_kind": "exact"})
            continue
        # 2) fuzzy match against same-page rows (reworded re-finds the exact key misses)
        kind, match = _fuzzy_dup(cand, rows)
        if kind == "hard":
            skipped.append({"finding_key": fk, "page_id": cand.get("page_id"),
                            "anchor": cand.get("anchor"),
                            "prior_status": [match["resolution_status"]],
                            "dup_kind": "fuzzy_identifier",
                            "matches_comment_id": match["comment_id"],
                            "matches_title": match["finding_title"]})
            continue
        if kind == "soft":
            cand["possible_dup_of"] = {"comment_id": match["comment_id"],
                                       "title": match["finding_title"]}
        new.append(cand)
    out = {"new": new, "skipped": skipped,
           "summary": {"candidates": len(cands), "new": len(new), "skipped": len(skipped)}}
    if args.out:
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2)
    print(json.dumps(out["summary"]))
    if skipped:
        sys.stderr.write(f"deduped {len(skipped)} already-tracked finding(s)\n")


def cmd_discover_merge(args):
    """Self-maintaining inventory. Input: discovered candidates already filtered by chat
    to (team-owned service + team-member author + design-doc, not ops/RCA/oncall):
    [{id, title, author|owner, repo}]. Compares against ALL ids already known in the
    inventory (monitor + needs_confirm + excluded) and returns only the NEW ones.
    With --write, append-inserts the new ids under `needs_confirm:` in the inventory file
    (append-only, comment-preserving — never rewrites the hand-curated buckets). New docs
    land in needs_confirm so the sweep NEVER auto-comments on them until the owner promotes
    them to `monitor`."""
    import yaml as _yaml
    with open(args.candidates) as f:
        cands = json.load(f)
    if isinstance(cands, dict):
        cands = cands.get("candidates", cands.get("discovered", []))
    inv = _yaml.safe_load(open(args.inventory)) or {}
    known = set()
    for bucket in ("monitor", "needs_confirm", "excluded"):
        for r in (inv.get(bucket) or []):
            if r.get("id"):
                known.add(str(r["id"]))
    new = [c for c in cands if str(c.get("id")) not in known]
    summary = {"candidates": len(cands), "already_known": len(cands) - len(new), "new": len(new)}
    if args.write and new:
        lines = open(args.inventory).read().splitlines(keepends=True)
        # find the `needs_confirm:` key line; insert new entries right after it
        idx = next((i for i, ln in enumerate(lines) if ln.rstrip() == "needs_confirm:"), None)
        if idx is None:
            sys.stderr.write("needs_confirm: section not found — not writing\n")
        else:
            def esc(s): return '"' + str(s or "").replace('\\', '\\\\').replace('"', '\\"') + '"'
            ins = []
            for c in new:
                ins.append(
                    f'  - {{id: "{c.get("id")}", title: {esc(c.get("title"))}, '
                    f'author: {esc(c.get("author") or c.get("owner"))}, '
                    f'repo: {c.get("repo","?")}, why: newly_discovered}}\n'
                )
            lines[idx + 1:idx + 1] = ins
            with open(args.inventory, "w") as f:
                f.writelines(lines)
            summary["written_to"] = args.inventory
    print(json.dumps({"summary": summary, "new": new}, indent=2))


def _name_to_canonical(name):
    """Map a Confluence author display name → people.yaml canonical slug (best-effort)."""
    if not name:
        return name
    norm = lambda s: re.sub(r"[^a-z]", "", str(s).lower())
    target = norm(name)
    try:
        import yaml as _yaml
        ppl = _yaml.safe_load(open(PEOPLE_YAML)) or {}
    except Exception:
        return name
    for p in (ppl.get("people") or []):
        names = [p.get("name"), p.get("canonical"), p.get("slack_handle")]
        names += (p.get("git_names") or []) + (p.get("github_aliases") or [])
        if any(norm(n) == target for n in names if n):
            return p.get("canonical") or name
    return name


def _inv_region(lines, header):
    """[start,end) line range of a top-level bucket `header:` (e.g. 'needs_confirm')."""
    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == f"{header}:"), None)
    if start is None:
        return None, None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j]
        if s.strip() and not s[0].isspace() and not s.lstrip().startswith("#"):
            end = j
            break
    return start, end


def cmd_move(args):
    """Promote/Reject a discovered doc. promote: needs_confirm → monitor (sweep will check it).
    exclude: needs_confirm → excluded (with reason). Comment-preserving line surgery; only the
    matching id within needs_confirm is moved. Idempotent: no-op if id is already in the target."""
    import yaml as _yaml
    pid = str(args.id)
    inv = _yaml.safe_load(open(args.inventory)) or {}
    target = "monitor" if args.to == "promote" else "excluded"
    # already in target? idempotent success.
    if any(str(r.get("id")) == pid for r in (inv.get(target) or [])):
        print(json.dumps({"ok": True, "id": pid, "moved_to": target, "noop": "already there"}))
        return
    entry = next((r for r in (inv.get("needs_confirm") or []) if str(r.get("id")) == pid), None)
    if entry is None:
        sys.stderr.write(f"id {pid} not in needs_confirm — nothing to move\n")
        sys.exit(2)
    lines = open(args.inventory).read().splitlines(keepends=True)
    nc_start, nc_end = _inv_region(lines, "needs_confirm")
    if nc_start is None:
        sys.stderr.write("needs_confirm: section not found\n"); sys.exit(2)
    row_idx = next((i for i in range(nc_start + 1, nc_end)
                    if re.search(rf'id:\s*"?{re.escape(pid)}"?\b', lines[i])), None)
    if row_idx is None:
        sys.stderr.write(f"id {pid} line not found in needs_confirm region\n"); sys.exit(2)
    del lines[row_idx]
    def esc(s): return '"' + str(s or "").replace('\\', '\\\\').replace('"', '\\"') + '"'
    title = entry.get("title"); repo = entry.get("repo", "?")
    author = entry.get("author") or entry.get("owner")
    if target == "monitor":
        owner = _name_to_canonical(author)
        kind = entry.get("kind", "design")
        new_line = (f'  - {{id: "{pid}", title: {esc(title)}, owner: {owner}, '
                    f'repo: {repo}, kind: {kind}, note: {esc("promoted via relay " + (args.run_id or ""))}}}\n')
    else:
        new_line = (f'  - {{id: "{pid}", title: {esc(title)}, reason: discovery_rejected, '
                    f'author: {esc(author)}}}\n')
    # region indices shifted by the delete; recompute target header
    tgt_idx = next((i for i, ln in enumerate(lines) if ln.rstrip() == f"{target}:"), None)
    if tgt_idx is None:
        sys.stderr.write(f"{target}: section not found\n"); sys.exit(2)
    lines[tgt_idx + 1:tgt_idx + 1] = [new_line]
    with open(args.inventory, "w") as f:
        f.writelines(lines)
    print(json.dumps({"ok": True, "id": pid, "moved_to": target, "title": title}))


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
    p = sub.add_parser("discover-merge")
    p.add_argument("--inventory", required=True); p.add_argument("--candidates", required=True)
    p.add_argument("--write", action="store_true")
    p.set_defaults(fn=cmd_discover_merge)
    p = sub.add_parser("move", help="promote/reject a discovered doc: needs_confirm -> monitor|excluded")
    p.add_argument("--inventory", required=True); p.add_argument("--id", required=True)
    p.add_argument("--to", required=True, choices=["promote", "exclude"])
    p.add_argument("--run-id")
    p.set_defaults(fn=cmd_move)
    p = sub.add_parser("list"); p.add_argument("--open", action="store_true"); p.set_defaults(fn=cmd_list)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

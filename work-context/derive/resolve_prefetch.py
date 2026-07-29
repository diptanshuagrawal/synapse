#!/usr/bin/env python3
"""Batch Jira prefetch for /resolve-initiatives — moves all searches out of the chat loop.

Reads  derived/initiatives-in.json  (planner "Resolve via chat" export)
   +   derived/initiatives-out.json (previous resolution, may be absent)
Diffs by the _src fingerprint (name|task|epic|type|assignees, lowercased; an input epic
that merely echoes the entry's own resolved epic is NOT a diff), then for rows that need
resolution runs every Jira search concurrently and dumps the evidence.

Rows may carry a `task` (a specific work item inside the initiative; several rows can
share a `name` with different tasks). Entries are keyed by name+task. For task rows the
ticket search matches the TASK text (and runs even when the epic is preset), so the
resolver can size the row from its matched tickets instead of the whole epic.

Writes derived/resolve-prefetch.json:
  { "_generated", "unchanged": [<out-entries verbatim, _src refreshed>],
    "resolve": [ { "input": {...}, "fingerprint",
                   "epicSearch":   [{key,summary,status}],          # epics whose title matches
                   "ticketSearch": [{key,summary,status,sp,assignee,parent,parentSummary,updated}],
                   "candidates":   [{epic,summary,status,votes,remainingSP,
                                     doneRecentSP,doneRecentN,
                                     open:[{key,status,assignee,sp,updated,summary}]}] } ] }

The chat session then only judges: pick the epic (or ticket-level fallback), write the
comment, assemble initiatives-out.json. Zero curl round-trips in chat.

Usage:  .venv/bin/python derive/resolve_prefetch.py [--all]
        --all  ignore the diff, prefetch every initiative
"""
import base64
import json
import pathlib
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from derive.sources_config import atlassian_host, jira_project_keys  # noqa: E402

DERIVED = ROOT / "derived"
JIRA_HOST = atlassian_host()
PROJECT = (jira_project_keys() or [""])[0]   # from config; no hardcoded org key
SP_FIELD = "customfield_10051"


def _secret(name):
    return (pathlib.Path.home() / ".secrets" / name).read_text().strip()


def _auth():
    return "Basic " + base64.b64encode(
        f"{_secret('atlassian_email')}:{_secret('atlassian_token')}".encode()).decode()


def jql(query, fields, max_results=50):
    body = json.dumps({"jql": query, "fields": fields, "maxResults": max_results}).encode()
    req = urllib.request.Request(
        f"https://{JIRA_HOST}/rest/api/3/search/jql", data=body, method="POST")
    req.add_header("Authorization", _auth())
    req.add_header("Content-Type", "application/json")
    return json.load(urllib.request.urlopen(req, timeout=30)).get("issues", [])


def fingerprint(row, blank_epic=False):
    vals = [(row.get(k) or "").strip().lower()
            for k in ("name", "task", "epic", "type", "assignees")]
    if blank_epic:
        vals[2] = ""
    return "|".join(vals)


def entry_key(row):
    """Merge identity: initiative name + task (several rows can share a name)."""
    return ((row.get("name") or "").strip() + "||" + (row.get("task") or "").strip()).lower()


def _slim(i):
    f = i["fields"]
    parent = f.get("parent") or {}
    return {"key": i["key"], "summary": (f.get("summary") or "")[:80],
            "status": f["status"]["name"],
            "sp": f.get(SP_FIELD) or 0,
            "assignee": ((f.get("assignee") or {}).get("displayName") or ""),
            "parent": parent.get("key", ""),
            "parentSummary": ((parent.get("fields") or {}).get("summary") or "")[:80],
            "updated": (f.get("updated") or "")[:10]}


TICKET_FIELDS = ["summary", "status", "assignee", "parent", SP_FIELD, "updated"]


def search_initiative(name, task, preset_epic):
    """Both text searches for one initiative row.

    Epic search runs on the initiative name (skipped when the epic is preset).
    Ticket search runs on the task text when set (even with a preset epic — the
    row must size from its own tickets), else on the name."""
    if preset_epic and not task:
        return {"epicSearch": [], "ticketSearch": []}
    epics = []
    if not preset_epic:
        safe = name.replace('"', "")
        epics = jql(f'project = {PROJECT} AND issuetype = Epic AND summary ~ "{safe}"',
                    ["summary", "status"])
    text = (task or name).replace('"', "")
    tickets = jql(f'project = {PROJECT} AND issuetype != Epic AND summary ~ "{text}"',
                  TICKET_FIELDS)
    return {
        "epicSearch": [{"key": i["key"], "summary": i["fields"]["summary"][:80],
                        "status": i["fields"]["status"]["name"]} for i in epics],
        "ticketSearch": [_slim(i) for i in tickets]}


def main():
    force_all = "--all" in sys.argv
    inp = json.loads((DERIVED / "initiatives-in.json").read_text())["initiatives"]
    try:
        prev = json.loads((DERIVED / "initiatives-out.json").read_text())["initiatives"]
    except Exception:
        prev = []
    old = {entry_key(e): e for e in prev}

    unchanged, resolve_rows = [], []
    for row in inp:
        e = old.get(entry_key(row))
        cur = fingerprint(row)
        if e and not force_all:
            stored = e.get("_src", "")
            echo_ok = (stored == fingerprint(row, blank_epic=True)
                       and (row.get("epic") or "").strip() == (e.get("epic") or ""))
            if stored == cur or echo_ok:
                e["_src"] = cur
                unchanged.append(e)
                continue
        resolve_rows.append((row, cur))

    # fan out the per-initiative text searches
    with ThreadPoolExecutor(max_workers=6) as ex:
        searches = list(ex.map(
            lambda rc: search_initiative(rc[0]["name"], (rc[0].get("task") or "").strip(),
                                         (rc[0].get("epic") or "").strip()),
            resolve_rows))

    # candidate epics per initiative: preset epic, epic-title hits, ticket parents (voted)
    per_init_candidates, all_epics = [], set()
    for (row, _), s in zip(resolve_rows, searches):
        votes = {}
        preset = (row.get("epic") or "").strip()
        if preset:
            votes[preset] = {"votes": 0, "summary": "", "status": ""}
        for ep in s["epicSearch"]:
            if ep["status"].lower() not in ("cancelled", "done"):
                votes.setdefault(ep["key"], {"votes": 0, "summary": ep["summary"],
                                             "status": ep["status"]})["votes"] += 2
        for t in s["ticketSearch"]:
            if t["parent"] and t["status"].lower() not in ("cancelled", "done"):
                votes.setdefault(t["parent"], {"votes": 0, "summary": t["parentSummary"],
                                               "status": ""})["votes"] += 1
        top = sorted(votes.items(), key=lambda kv: -kv[1]["votes"])[:3]
        per_init_candidates.append(top)
        all_epics.update(k for k, _ in top)

    # one batched children query for every candidate epic (open + recent-done)
    open_by_epic, done_by_epic = {}, {}
    if all_epics:
        keys = ",".join(sorted(all_epics))
        for i in jql(f"parent in ({keys}) AND statusCategory != Done", TICKET_FIELDS, 100):
            open_by_epic.setdefault(i["fields"]["parent"]["key"], []).append(_slim(i))
        for i in jql(f"parent in ({keys}) AND statusCategory = Done AND updated >= -35d",
                     ["parent", SP_FIELD], 100):
            agg = done_by_epic.setdefault(i["fields"]["parent"]["key"], {"sp": 0, "n": 0})
            agg["sp"] += i["fields"].get(SP_FIELD) or 0
            agg["n"] += 1

    out_rows = []
    for (row, fp), s, cands in zip(resolve_rows, searches, per_init_candidates):
        candidates = []
        for key, meta in cands:
            open_t = open_by_epic.get(key, [])
            done = done_by_epic.get(key, {"sp": 0, "n": 0})
            candidates.append({"epic": key, "summary": meta["summary"], "status": meta["status"],
                               "votes": meta["votes"],
                               "remainingSP": round(sum(t["sp"] for t in open_t), 2),
                               "doneRecentSP": round(done["sp"], 2), "doneRecentN": done["n"],
                               "open": open_t})
        out_rows.append({"input": row, "fingerprint": fp,
                         "epicSearch": s["epicSearch"], "ticketSearch": s["ticketSearch"],
                         "candidates": candidates})

    from datetime import date
    out = {"_generated": str(date.today()), "unchanged": unchanged, "resolve": out_rows}
    (DERIVED / "resolve-prefetch.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"unchanged={len(unchanged)} resolve={len(out_rows)} "
          f"-> {DERIVED / 'resolve-prefetch.json'}")


if __name__ == "__main__":
    main()

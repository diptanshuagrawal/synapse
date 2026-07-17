#!/usr/bin/env python3
"""Synapse ecosystem pages — the read-mostly surfaces that aren't the planner.

One isolated server so the big dashboard/planner files are never touched. Serves:
  /people        per-dev cockpit (throughput / review load / activity)
  /pr-friction   PR review-friction report
  /releases      CMR deploy timeline
  /docs          doc-drift status
  /meetings      upcoming meetings + notes
  /ask           cross-source narrative console (chat round-trip)

Read-only over index/events.db + config. Runs internal-only behind synapse_server.

    .venv/bin/python derive/synapse_pages.py [port]      # default 8768
"""
import os, sys, json, sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "derive"))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "index", "events.db")

import synapse_nav
import yaml

try:
    import jira_metrics
except Exception:
    jira_metrics = None


def _conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.row_factory = sqlite3.Row
    return c


def _people():
    """Team roster from people.yaml (scope:team + owner), with display fields."""
    with open(os.path.join(ROOT, "config", "people.yaml")) as f:
        data = yaml.safe_load(f).get("people", [])
    out = []
    for p in data:
        if not p.get("canonical"):
            continue
        scope = p.get("scope", "")
        if scope not in ("team", "owner") and not p.get("owner_is_manager"):
            continue
        out.append({"canonical": p["canonical"], "name": p.get("name", p["canonical"]),
                    "role": p.get("role", ""), "scope": scope})
    return out


def _lookup():
    if jira_metrics:
        return jira_metrics.load_people_lookup()
    with open(os.path.join(ROOT, "config", "people.yaml")) as f:
        data = yaml.safe_load(f).get("people", [])
    lk = {}
    for p in data:
        c = p.get("canonical")
        if not c:
            continue
        for k in ("canonical", "github", "email", "jira_id", "slack_id", "slack_handle", "name", "git_name"):
            if p.get(k):
                lk[str(p[k]).lower().strip()] = c
    return lk


# ── data: People ──────────────────────────────────────────────────────────────
_BUCKET = {
    ("github", "pr_opened"): "prsOpened", ("github", "pr_merged"): "prsMerged",
    ("github", "review"): "reviews", ("github", "comment"): "reviews",
    ("github", "commit_in_pr"): "commits", ("github", "commit_pushed"): "commits",
    ("jira", "issue_created"): "jiraCreated", ("jira", "status_change"): "jiraUpdates",
    ("slack", "thread_reply"): "slack", ("slack", "thread_started"): "slack",
}
_FIELDS = ["prsOpened", "prsMerged", "reviews", "commits", "jiraCreated", "jiraUpdates", "slack"]


def people_activity(days=30):
    team = _people()
    lk = _lookup()
    canon_set = {p["canonical"] for p in team}
    rows = {p["canonical"]: {f: 0 for f in _FIELDS} | {"activity": 0} for p in team}
    with _conn() as c:
        q = ("SELECT source, event_type, LOWER(actor) a, COUNT(*) n FROM events "
             "WHERE actor IS NOT NULL AND ts >= datetime('now', ?) GROUP BY 1,2,3")
        for r in c.execute(q, (f"-{int(days)} day",)):
            canon = lk.get(r["a"])
            if canon not in canon_set:
                continue
            rows[canon]["activity"] += r["n"]
            b = _BUCKET.get((r["source"], r["event_type"]))
            if b:
                rows[canon][b] += r["n"]
    people = []
    for p in team:
        people.append({**p, **rows[p["canonical"]]})
    people.sort(key=lambda x: x["activity"], reverse=True)
    totals = {f: sum(p[f] for p in people) for f in _FIELDS + ["activity"]}
    return {"days": days, "people": people, "totals": totals}


# ── data: PR friction ─────────────────────────────────────────────────────────
def pr_friction_report(limit=40):
    lk = _lookup()
    nm = {p["canonical"]: p["name"] for p in _people()}
    prs, cat, by_dev = [], {}, {}
    tot = {"human": 0, "agentic": 0, "prs": 0, "rework": 0}
    with _conn() as c:
        q = ("SELECT f.subject s, f.score, f.dominant_category dc, f.category_counts_json cc, "
             "m.repo, m.number, m.state, m.additions, m.deletions, m.files_changed, "
             "(SELECT e.actor FROM events e WHERE e.subject=f.subject AND e.event_type='pr_opened' LIMIT 1) actor "
             "FROM pr_friction f LEFT JOIN pr_meta m ON m.subject=f.subject "
             "ORDER BY f.score DESC")
        for r in c.execute(q):
            counts = json.loads(r["cc"] or "{}")
            human = sum(v.get("human", 0) for v in counts.values())
            agentic = sum(sum(x for k, x in v.items() if k != "human") for v in counts.values())
            dc = r["dc"] or "clean"
            cat[dc] = cat.get(dc, 0) + 1
            tot["prs"] += 1
            tot["human"] += human
            tot["agentic"] += agentic
            if dc == "rework":
                tot["rework"] += 1
            canon = lk.get((r["actor"] or "").lower())
            author = nm.get(canon, canon or "—")
            url = (f"https://github.com/{r['repo']}/pull/{r['number']}"
                   if r["repo"] and r["number"] else None)
            if canon:
                d = by_dev.setdefault(author, {"prs": 0, "score": 0.0, "human": 0, "agentic": 0})
                d["prs"] += 1
                d["score"] += r["score"] or 0
                d["human"] += human
                d["agentic"] += agentic
            if (r["score"] or 0) > 0 and dc != "clean":
                prs.append({"subject": r["s"], "repo": r["repo"], "number": r["number"],
                            "score": round(r["score"] or 0, 1), "category": dc, "state": r["state"],
                            "adds": r["additions"], "dels": r["deletions"], "files": r["files_changed"],
                            "human": human, "agentic": agentic, "author": author, "url": url})
    devs = [{"name": k, "prs": v["prs"], "avgScore": round(v["score"] / v["prs"], 1) if v["prs"] else 0,
             "human": v["human"], "agentic": v["agentic"]} for k, v in by_dev.items()]
    devs.sort(key=lambda x: x["avgScore"], reverse=True)
    return {"prs": prs[:limit], "categories": cat, "byDev": devs, "totals": tot}


# ── data: Releases ────────────────────────────────────────────────────────────
def releases_report(limit=60):
    lk = _lookup()
    nm = {p["canonical"]: p["name"] for p in _people()}

    def owner_name(raw):
        if not raw:
            return "—"
        canon = lk.get(raw.lower().strip()) or lk.get(raw.lstrip("@").lower().strip())
        return nm.get(canon, raw.lstrip("@"))

    with _conn() as c:
        rows = c.execute(
            "SELECT cmr_subject, title, slug, service, release_owner, released_at, approved_by, "
            "outcome, pr_urls_json, url, is_feature_release FROM feature_release "
            "WHERE released_at IS NOT NULL AND released_at != '' "
            "ORDER BY released_at DESC").fetchall()
    rels, by_service, outcomes, seen = [], {}, {}, set()
    feat = 0
    for r in rows:
        if r["cmr_subject"] in seen:          # one CMR ↔ many slugs → collapse to one release
            continue
        seen.add(r["cmr_subject"])
        prs = json.loads(r["pr_urls_json"] or "[]")
        oc = (r["outcome"] or "released").lower()
        outcomes[oc] = outcomes.get(oc, 0) + 1
        if r["service"]:
            by_service[r["service"]] = by_service.get(r["service"], 0) + 1
        if r["is_feature_release"]:
            feat += 1
        if len(rels) < limit:
            rels.append({"title": (r["title"] or r["slug"] or "release")[:90], "slug": r["slug"],
                         "service": r["service"] or "—", "owner": owner_name(r["release_owner"] or r["approved_by"]),
                         "releasedAt": r["released_at"], "outcome": oc, "url": r["url"],
                         "prCount": len(prs), "feature": bool(r["is_feature_release"])})
    return {"releases": rels, "byService": by_service, "outcomes": outcomes,
            "total": len(seen), "feature": feat}


# ── data: Doc drift ───────────────────────────────────────────────────────────
def docs_report():
    path = os.path.join(ROOT, "state", "doc_sync.db")
    if not os.path.exists(path):
        return {"findings": [], "byPage": [], "totals": {"open": 0, "resolved": 0}}
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    findings, pages, sev = [], {}, {}
    tot = {"open": 0, "resolved": 0}
    with c:
        for r in c.execute(
            "SELECT page_title, page_url, severity, check_type, finding_title, owner_account, "
            "resolution_status, created_ts, comment_url FROM doc_sync_comments ORDER BY created_ts DESC"):
            st = (r["resolution_status"] or "open").lower()
            tot["open" if st == "open" else "resolved"] = tot.get("open" if st == "open" else "resolved", 0) + 1
            if st == "open":
                s = (r["severity"] or "info").lower()
                sev[s] = sev.get(s, 0) + 1
                pg = r["page_title"] or "—"
                pages[pg] = pages.get(pg, {"page": pg, "url": r["page_url"], "count": 0})
                pages[pg]["count"] += 1
                findings.append({"page": pg, "pageUrl": r["page_url"], "severity": s,
                                 "check": r["check_type"] or "", "title": r["finding_title"] or "",
                                 "owner": r["owner_account"] or "", "url": r["comment_url"] or r["page_url"],
                                 "created": r["created_ts"]})
    by_page = sorted(pages.values(), key=lambda x: x["count"], reverse=True)
    return {"findings": findings, "byPage": by_page, "severities": sev, "totals": tot}


# ── data: Meetings ────────────────────────────────────────────────────────────
def meetings_report(limit=60):
    with _conn() as c:
        segs = {r["subject"]: r["n"] for r in c.execute(
            "SELECT subject, COUNT(*) n FROM events WHERE source='meeting' "
            "AND event_type='transcript_segment' GROUP BY subject")}
        rows = c.execute(
            "SELECT subject, title, ts, url FROM events WHERE source='meeting' "
            "AND event_type='meeting_recorded' ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    meetings = [{"title": (r["title"] or r["subject"]).strip(), "ts": r["ts"], "subject": r["subject"],
                 "segments": segs.get(r["subject"], 0), "url": r["url"]} for r in rows]
    return {"meetings": meetings, "total": len(meetings),
            "totalSegments": sum(segs.values())}


# ── data: Ask (LLM round-trip) ────────────────────────────────────────────────
def ask_answer():
    p = os.path.join(ROOT, "derived", "ask-answer.json")
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ── data: Topics / clusters ───────────────────────────────────────────────────
def topics_report(limit=60, scope="active"):
    with _conn() as c:
        rows = c.execute(
            "SELECT cluster_id, label, status, member_count, last_activity_ts, summary, "
            "source_breakdown_json, blockers_json FROM topic_brief "
            "WHERE label IS NOT NULL AND label != '' ORDER BY last_activity_ts DESC").fetchall()
    topics, counts = [], {}
    for r in rows:
        st = (r["status"] or "").upper()
        counts[st] = counts.get(st, 0) + 1
        if scope == "active" and st != "ACTIVE":
            continue
        if len(topics) >= limit:
            continue
        sb = json.loads(r["source_breakdown_json"] or "{}")
        blk = json.loads(r["blockers_json"] or "[]")
        topics.append({"id": r["cluster_id"], "label": r["label"], "status": st,
                       "members": r["member_count"] or 0, "lastTs": r["last_activity_ts"],
                       "summary": (r["summary"] or "")[:240],
                       "sources": sb, "blockers": len(blk) if isinstance(blk, list) else 0})
    return {"topics": topics, "counts": counts, "scope": scope, "total": len(rows)}


# ── data: Velocity ────────────────────────────────────────────────────────────
def velocity_report(days=120):
    if not jira_metrics:
        return {"error": "jira_metrics unavailable"}
    import datetime as dt
    end = dt.datetime.utcnow()
    start = end - dt.timedelta(days=int(days))
    nm = {p["canonical"]: p["name"] for p in _people()}
    with _conn() as c:
        credits = jira_metrics.compute_done_credits(
            c, start.isoformat(), end.isoformat(), _lookup())
    by_sprint = jira_metrics.aggregate_velocity_by_sprint(credits)
    by_actor = jira_metrics.aggregate_velocity_by_actor(credits)
    sprints = [{"name": k, "sp": round(v["sp"], 1), "tickets": v["tickets"], "state": v.get("state", "")}
               for k, v in by_sprint.items()]
    sprints.sort(key=lambda x: x["name"])
    devs = [{"name": nm.get(k, k), "sp": round(v["sp"], 1), "tickets": v["tickets"]}
            for k, v in by_actor.items() if k in nm]
    devs.sort(key=lambda x: x["sp"], reverse=True)
    return {"days": days, "sprints": sprints, "devs": devs,
            "totalSP": round(sum(s["sp"] for s in sprints), 1),
            "totalTickets": sum(s["tickets"] for s in sprints)}


# ── data: Service briefs ──────────────────────────────────────────────────────
def services_report():
    with _conn() as c:
        rows = c.execute(
            "SELECT subject, title, ts, LENGTH(body) blen FROM events "
            "WHERE source='service' AND event_type='service_brief' ORDER BY ts DESC").fetchall()
    svcs = {}
    for r in rows:
        subj = r["subject"] or ""
        svc = subj[len("service:"):subj.index("#")] if "service:" in subj and "#" in subj else subj
        section = subj.split("#", 1)[1] if "#" in subj else subj
        s = svcs.setdefault(svc, {"service": svc, "sections": [], "lastTs": r["ts"], "chars": 0})
        s["sections"].append({"section": section, "title": r["title"] or section, "chars": r["blen"] or 0})
        s["chars"] += r["blen"] or 0
        if r["ts"] > s["lastTs"]:
            s["lastTs"] = r["ts"]
    services = sorted(svcs.values(), key=lambda x: len(x["sections"]), reverse=True)
    return {"services": services, "total": len(services)}


# ── data: Subject timeline ────────────────────────────────────────────────────
def timeline_report(q, limit=200):
    q = (q or "").strip()
    if not q:
        return {"q": "", "events": [], "subjects": []}
    nm = {p["canonical"]: p["name"] for p in _people()}
    lk = _lookup()
    like = f"%{q}%"
    with _conn() as c:
        rows = c.execute(
            "SELECT source, event_type, ts, actor, title, url, subject FROM events "
            "WHERE subject = ? OR subject LIKE ? OR title LIKE ? ORDER BY ts ASC LIMIT ?",
            (q, like, like, limit)).fetchall()
    subjects = {}
    events = []
    for r in rows:
        canon = lk.get((r["actor"] or "").lower())
        subjects[r["subject"]] = subjects.get(r["subject"], 0) + 1
        events.append({"ts": r["ts"], "source": r["source"], "type": r["event_type"],
                       "actor": nm.get(canon, r["actor"] or "—"), "title": (r["title"] or "")[:120],
                       "url": r["url"], "subject": r["subject"]})
    subs = sorted(subjects.items(), key=lambda x: x[1], reverse=True)[:12]
    return {"q": q, "events": events, "subjects": [{"subject": s, "count": n} for s, n in subs]}


# ── HTML shell ──────────────────────────────────────────────────────────────
_CSS = """
*{box-sizing:border-box} body{margin:0;background:#f6f4f1;color:#15202e;
 font-family:'Hanken Grotesk',-apple-system,system-ui,sans-serif;font-size:14px;line-height:1.45;}
.wrap{max-width:1180px;margin:0 auto;padding:26px 24px 70px;}
h1{font-size:21px;font-weight:700;margin:0;letter-spacing:-.01em;}
.sub{color:#6b7280;font-size:12.5px;margin:3px 0 0;}
.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px;flex-wrap:wrap;}
.seg{display:inline-flex;border:1px solid #e4e0da;border-radius:9px;overflow:hidden;}
.seg button{font:inherit;font-size:12.5px;padding:6px 13px;border:none;background:#fff;color:#6b7280;cursor:pointer;}
.seg button.on{background:#ff6a5b;color:#fff;}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px;}
.card{background:#fff;border:1px solid #ece8e2;border-radius:13px;padding:13px 15px;}
.card .l{font-size:10px;letter-spacing:.05em;text-transform:uppercase;color:#8a94a6;}
.card .v{font-size:22px;font-weight:700;font-family:'IBM Plex Mono',monospace;margin-top:3px;}
.panel{background:#fff;border:1px solid #ece8e2;border-radius:13px;padding:4px 16px 12px;}
table{width:100%;border-collapse:collapse;}
th{text-align:right;font-size:10px;letter-spacing:.04em;text-transform:uppercase;color:#8a94a6;font-weight:600;
 padding:12px 10px 9px;border-bottom:1px solid #ece8e2;cursor:pointer;white-space:nowrap;}
th.l,td.l{text-align:left;}
td{padding:10px;border-bottom:1px solid #f1ede7;text-align:right;font-family:'IBM Plex Mono',monospace;font-size:13px;}
tr:last-child td{border-bottom:none;} tbody tr:hover{background:#faf8f5;}
.nm{font-family:'Hanken Grotesk',sans-serif;font-weight:600;} .role{color:#9aa6b4;font-size:11px;margin-left:6px;}
.bar{height:6px;border-radius:4px;background:#f0ece6;overflow:hidden;min-width:60px;}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#ffb14a,#ff6a5b);}
.state{padding:30px;text-align:center;color:#8a94a6;}
.soon{padding:48px 24px;text-align:center;color:#8a94a6;}
.soon .ic{font-size:34px;} .soon h2{color:#15202e;font-weight:600;margin:10px 0 4px;font-size:17px;}
"""


def _doc(title, body, js=""):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} · Synapse</title>
<style>@import url('https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');{_CSS}</style>
</head><body><div class="wrap">{body}</div><script>{js}</script></body></html>"""


def page_people():
    body = """
<div class="top">
  <div><h1>People</h1><div class="sub">Per-dev activity across GitHub · Jira · Slack. Read-only.</div></div>
  <div class="seg" id="win">
    <button data-d="7">7d</button><button data-d="30" class="on">30d</button><button data-d="90">90d</button>
  </div>
</div>
<div class="cards" id="cards"></div>
<div class="panel"><div id="tbl" class="state">Loading…</div></div>
"""
    js = r"""
let DAYS=30, SORT='activity', DIR=-1, DATA=null;
const COLS=[['prsOpened','PRs'],['prsMerged','Merged'],['reviews','Reviews'],['commits','Commits'],
            ['jiraCreated','Jira new'],['jiraUpdates','Jira upd'],['slack','Slack'],['activity','Activity']];
async function load(){
  document.getElementById('tbl').innerHTML='<div class="state">Loading…</div>';
  DATA=await (await fetch('/api/people?days='+DAYS)).json();
  render();
}
function render(){
  const t=DATA.totals;
  document.getElementById('cards').innerHTML=
    [['People',DATA.people.length],['PRs merged',t.prsMerged],['Reviews',t.reviews],
     ['Commits',t.commits],['Total activity',t.activity]]
    .map(([l,v])=>`<div class="card"><div class="l">${l}</div><div class="v">${v}</div></div>`).join('');
  const ppl=[...DATA.people].sort((a,b)=>(a[SORT]-b[SORT])*DIR);
  const max=Math.max(1,...ppl.map(p=>p.activity));
  const head='<th class="l" data-k="name">Person</th>'+COLS.map(([k,l])=>`<th data-k="${k}">${l}</th>`).join('')+'<th>Load</th>';
  const rows=ppl.map(p=>`<tr><td class="l"><span class="nm">${p.name}</span><span class="role">${p.role||''}</span></td>`
    +COLS.map(([k])=>`<td>${p[k]||0}</td>`).join('')
    +`<td><div class="bar"><i style="width:${Math.round(p.activity/max*100)}%"></i></div></td></tr>`).join('');
  document.getElementById('tbl').innerHTML=`<table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
  document.querySelectorAll('th[data-k]').forEach(th=>th.onclick=()=>{
    const k=th.dataset.k; if(k==='name'){SORT='name';} else {SORT=k;}
    DIR=(SORT===k&&DIR===-1)?1:-1; render();
  });
}
document.querySelectorAll('#win button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('#win button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); DAYS=+b.dataset.d; load();
});
load();
"""
    return _doc("People", body, js)


def page_pr_friction():
    body = """
<div class="top"><div><h1>PR friction</h1>
  <div class="sub">Review-friction score per PR, dominant cause, and human vs agentic comment split.</div></div></div>
<div class="cards" id="cards"></div>
<div class="panel" style="margin-bottom:16px"><div id="cats" class="state">Loading…</div></div>
<div class="panel"><div id="tbl" class="state">Loading…</div></div>
"""
    js = r"""
async function load(){
  const d=await (await fetch('/api/pr-friction')).json();
  const t=d.totals;
  document.getElementById('cards').innerHTML=
    [['PRs scored',t.prs],['Rework PRs',t.rework],['Human comments',t.human],['Agentic comments',t.agentic]]
    .map(([l,v])=>`<div class="card"><div class="l">${l}</div><div class="v">${v}</div></div>`).join('');
  const cats=Object.entries(d.categories).filter(([k])=>k!=='clean').sort((a,b)=>b[1]-a[1]);
  const cmax=Math.max(1,...cats.map(c=>c[1]));
  document.getElementById('cats').innerHTML='<div style="padding:12px 4px"><div class="l" style="margin-bottom:8px">Friction by dominant cause</div>'
    +cats.map(([k,v])=>`<div style="display:flex;align-items:center;gap:10px;margin:5px 0">
       <div style="width:110px;font-size:12px">${k}</div>
       <div class="bar" style="flex:1"><i style="width:${Math.round(v/cmax*100)}%"></i></div>
       <div style="width:36px;text-align:right;font-family:monospace">${v}</div></div>`).join('')+'</div>';
  const rows=d.prs.map(p=>`<tr>
     <td class="l">${p.url?`<a href="${p.url}" target="_blank" style="color:#c2452f;text-decoration:none">${p.repo?p.repo.split('/').pop():''}#${p.number}</a>`:p.subject}</td>
     <td class="l">${p.author}</td>
     <td class="l"><span style="background:#f3ede6;border-radius:6px;padding:2px 8px;font-size:11px;font-family:sans-serif">${p.category}</span></td>
     <td>${p.score}</td><td>${p.human}</td><td>${p.agentic}</td>
     <td style="color:#9aa6b4">+${p.adds||0}/-${p.dels||0}</td></tr>`).join('');
  document.getElementById('tbl').innerHTML=`<table><thead><tr>
     <th class="l">PR</th><th class="l">Author</th><th class="l">Cause</th>
     <th>Friction</th><th>Human</th><th>Agentic</th><th>Size</th></tr></thead><tbody>${rows}</tbody></table>`;
}
load();
"""
    return _doc("PR friction", body, js)


def page_releases():
    body = """
<div class="top"><div><h1>Releases</h1>
  <div class="sub">Feature releases from CMR records — most recent first.</div></div></div>
<div class="cards" id="cards"></div>
<div class="panel"><div id="tbl" class="state">Loading…</div></div>
"""
    js = r"""
function fmt(s){ if(!s) return '—'; const d=new Date(s); return isNaN(d)?s.slice(0,10):d.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}); }
function badge(o){ const c=o.includes('rollback')||o.includes('emergency')?'#c2452f':o.includes('fail')?'#c2452f':'#2f7d4f';
  const bg=c==='#c2452f'?'rgba(194,69,47,.10)':'rgba(47,125,79,.12)';
  return `<span style="background:${bg};color:${c};border-radius:6px;padding:2px 8px;font-size:11px;font-family:sans-serif">${o}</span>`; }
async function load(){
  const d=await (await fetch('/api/releases')).json();
  document.getElementById('cards').innerHTML=
    [['Releases (with date)',d.total],['Feature releases',d.feature],['Services',Object.keys(d.byService).length]]
    .map(([l,v])=>`<div class="card"><div class="l">${l}</div><div class="v">${v}</div></div>`).join('');
  const rows=d.releases.map(r=>`<tr>
     <td class="l">${fmt(r.releasedAt)}</td>
     <td class="l"><span class="nm">${r.url?`<a href="${r.url}" target="_blank" style="color:#15202e;text-decoration:none">${r.title}</a>`:r.title}</span></td>
     <td class="l" style="color:#6b7280">${r.service}</td>
     <td class="l" style="color:#6b7280">${r.owner}</td>
     <td>${r.prCount||''}</td>
     <td class="l">${badge(r.outcome)}</td></tr>`).join('');
  document.getElementById('tbl').innerHTML=`<table><thead><tr>
     <th class="l">Released</th><th class="l">Release</th><th class="l">Service</th>
     <th class="l">Owner</th><th>PRs</th><th class="l">Outcome</th></tr></thead><tbody>${rows}</tbody></table>`;
}
load();
"""
    return _doc("Releases", body, js)


def page_docs():
    body = """
<div class="top"><div><h1>Doc drift</h1>
  <div class="sub">Open TRD/PRD drift findings from the doc-sync sweep.</div></div></div>
<div class="cards" id="cards"></div>
<div class="panel"><div id="tbl" class="state">Loading…</div></div>
"""
    js = r"""
function sev(s){ const m={high:'#c2452f',medium:'#b5671a',low:'#8a94a6',info:'#8a94a6'}; const c=m[s]||'#8a94a6';
  return `<span style="background:${c}1a;color:${c};border-radius:6px;padding:2px 8px;font-size:11px;font-family:sans-serif">${s}</span>`; }
async function load(){
  const d=await (await fetch('/api/docs')).json();
  document.getElementById('cards').innerHTML=
    [['Open findings',d.totals.open],['Resolved',d.totals.resolved],['Docs affected',d.byPage.length]]
    .map(([l,v])=>`<div class="card"><div class="l">${l}</div><div class="v">${v}</div></div>`).join('');
  if(!d.findings.length){ document.getElementById('tbl').innerHTML='<div class="soon"><div class="ic">✅</div><h2>No open drift</h2><p>Every tracked doc is in sync.</p></div>'; return; }
  const rows=d.findings.map(f=>`<tr>
    <td class="l"><span class="nm">${f.url?`<a href="${f.url}" target="_blank" style="color:#15202e;text-decoration:none">${f.title}</a>`:f.title}</span></td>
    <td class="l" style="color:#6b7280">${f.page}</td>
    <td class="l" style="color:#9aa6b4">${f.check}</td>
    <td class="l">${sev(f.severity)}</td></tr>`).join('');
  document.getElementById('tbl').innerHTML=`<table><thead><tr>
    <th class="l">Finding</th><th class="l">Doc</th><th class="l">Check</th><th class="l">Severity</th></tr></thead><tbody>${rows}</tbody></table>`;
}
load();
"""
    return _doc("Doc drift", body, js)


def page_meetings():
    body = """
<div class="top"><div><h1>Meetings</h1>
  <div class="sub">Recorded meetings ingested by the meeting-intelligence pipeline.</div></div></div>
<div class="cards" id="cards"></div>
<div class="panel"><div id="tbl" class="state">Loading…</div></div>
"""
    js = r"""
function fmt(s){ const d=new Date(s); return isNaN(d)?s:d.toLocaleString('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}); }
async function load(){
  const d=await (await fetch('/api/meetings')).json();
  document.getElementById('cards').innerHTML=
    [['Meetings',d.total],['Transcript segments',d.totalSegments]]
    .map(([l,v])=>`<div class="card"><div class="l">${l}</div><div class="v">${v}</div></div>`).join('');
  if(!d.meetings.length){ document.getElementById('tbl').innerHTML='<div class="soon"><div class="ic">🗣️</div><h2>No meetings yet</h2><p>Drop a recording in the transcripts inbox.</p></div>'; return; }
  const rows=d.meetings.map(m=>`<tr>
    <td class="l" style="color:#6b7280;white-space:nowrap">${fmt(m.ts)}</td>
    <td class="l"><span class="nm" style="text-transform:capitalize">${m.title}</span></td>
    <td>${m.segments}</td></tr>`).join('');
  document.getElementById('tbl').innerHTML=`<table><thead><tr>
    <th class="l">When</th><th class="l">Meeting</th><th>Segments</th></tr></thead><tbody>${rows}</tbody></table>`;
}
load();
"""
    return _doc("Meetings", body, js)


def page_ask():
    body = """
<div class="top"><div><h1>Ask</h1>
  <div class="sub">Cross-source narratives over the whole corpus. The answer is written by Claude Code.</div></div></div>
<div class="panel" style="margin-bottom:16px;padding:16px">
  <textarea id="q" placeholder="e.g. How is <teammate> doing this month?  ·  What happened with <feature>?  ·  Team pulse last 2 weeks"
    style="width:100%;min-height:70px;font:inherit;font-size:14px;border:1px solid #e4e0da;border-radius:10px;padding:10px;resize:vertical"></textarea>
  <div style="display:flex;gap:10px;align-items:center;margin-top:10px">
    <button id="send" style="font:inherit;font-size:13px;background:#ff6a5b;color:#fff;border:none;border-radius:9px;padding:9px 16px;cursor:pointer">Stage question</button>
    <button id="load" style="font:inherit;font-size:13px;background:#fff;color:#c2452f;border:1px solid #e4e0da;border-radius:9px;padding:9px 16px;cursor:pointer">↻ Load answer</button>
    <span id="msg" style="color:#8a94a6;font-size:12.5px"></span>
  </div>
</div>
<div class="panel"><div id="ans" class="soon"><div class="ic">🔎</div><h2>Ask about anyone or anything</h2>
  <p>Stage a question, run <code>/ask</code> in Claude Code, then Load answer.</p></div></div>
"""
    js = r"""
const msg=document.getElementById('msg');
document.getElementById('send').onclick=async()=>{
  const q=document.getElementById('q').value.trim(); if(!q){msg.textContent='type a question first';return;}
  const r=await (await fetch('/api/ask-dump',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})})).json();
  msg.textContent=r.ok?'staged — now run  /ask  in Claude Code, then Load answer':'error: '+(r.error||'?');
};
document.getElementById('load').onclick=async()=>{
  const a=await (await fetch('/api/ask-answer')).json();
  if(!a||!a.answer){msg.textContent='no answer yet — run /ask first';return;}
  msg.textContent='';
  const html=a.answer.replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/^### (.*)$/gm,'<h3 style="margin:14px 0 4px">$1</h3>')
    .replace(/^## (.*)$/gm,'<h2 style="margin:16px 0 6px;font-size:17px">$1</h2>')
    .replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/^- (.*)$/gm,'• $1<br>').replace(/\n\n/g,'<br><br>');
  document.getElementById('ans').className='';
  document.getElementById('ans').innerHTML=`<div style="padding:16px;line-height:1.6">${html}</div>`
    +(a.question?`<div style="padding:0 16px 14px;color:#9aa6b4;font-size:12px">Q: ${a.question}</div>`:'');
};
"""
    return _doc("Ask", body, js)


def page_topics():
    body = """
<div class="top"><div><h1>Topics</h1>
  <div class="sub">Auto-clustered themes — what the team is actually working on.</div></div>
  <div class="seg" id="scope"><button data-s="active" class="on">Active</button><button data-s="all">All</button></div></div>
<div class="cards" id="cards"></div>
<div class="panel"><div id="list" class="state">Loading…</div></div>
"""
    js = r"""
let SCOPE='active';
function st(s){ const m={ACTIVE:'#2f7d4f',RECURRING:'#b5671a',DORMANT:'#8a94a6',RESOLVED:'#6b7280'}; const c=m[s]||'#8a94a6';
  return `<span style="background:${c}1a;color:${c};border-radius:6px;padding:2px 8px;font-size:11px">${s||'—'}</span>`; }
function fmt(s){ if(!s)return''; const d=new Date(s); return isNaN(d)?'':d.toLocaleDateString('en-US',{month:'short',day:'numeric'}); }
async function load(){
  document.getElementById('list').innerHTML='<div class="state">Loading…</div>';
  const d=await (await fetch('/api/topics?scope='+SCOPE)).json();
  document.getElementById('cards').innerHTML=
    [['Clusters (total)',d.total],['Active',d.counts.ACTIVE||0],['Recurring',d.counts.RECURRING||0]]
    .map(([l,v])=>`<div class="card"><div class="l">${l}</div><div class="v">${v}</div></div>`).join('');
  document.getElementById('list').innerHTML=d.topics.map(t=>`
    <div style="padding:13px 4px;border-bottom:1px solid #f1ede7">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span class="nm" style="font-size:14.5px">${t.label}</span>${st(t.status)}
        <span style="color:#9aa6b4;font-size:12px">${t.members} items · ${fmt(t.lastTs)}${t.blockers?` · <span style="color:#c2452f">${t.blockers} blockers</span>`:''}</span>
      </div>
      ${t.summary?`<div style="color:#6b7280;font-size:13px;margin-top:4px">${t.summary}</div>`:''}
    </div>`).join('')||'<div class="state">No topics.</div>';
}
document.querySelectorAll('#scope button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('#scope button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); SCOPE=b.dataset.s; load();
});
load();
"""
    return _doc("Topics", body, js)


def page_velocity():
    body = """
<div class="top"><div><h1>Velocity</h1>
  <div class="sub">Story points completed per sprint and per dev (assigned-credit).</div></div></div>
<div class="cards" id="cards"></div>
<div class="panel" style="margin-bottom:16px"><div id="sprints" class="state">Loading…</div></div>
<div class="panel"><div id="devs" class="state"></div></div>
"""
    js = r"""
async function load(){
  const d=await (await fetch('/api/velocity')).json();
  if(d.error){document.getElementById('sprints').innerHTML='<div class="state">'+d.error+'</div>';return;}
  document.getElementById('cards').innerHTML=
    [['SP done',d.totalSP],['Tickets',d.totalTickets],['Sprints',d.sprints.length]]
    .map(([l,v])=>`<div class="card"><div class="l">${l}</div><div class="v">${v}</div></div>`).join('');
  const smax=Math.max(1,...d.sprints.map(s=>s.sp));
  document.getElementById('sprints').innerHTML='<div style="padding:10px 4px"><div class="l" style="margin-bottom:8px">SP by sprint</div>'
    +d.sprints.map(s=>`<div style="display:flex;align-items:center;gap:10px;margin:5px 0">
      <div style="width:150px;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${s.name}${s.state==='active'?' •':''}</div>
      <div class="bar" style="flex:1"><i style="width:${Math.round(s.sp/smax*100)}%"></i></div>
      <div style="width:64px;text-align:right;font-family:monospace">${s.sp} <span style="color:#9aa6b4">/${s.tickets}</span></div></div>`).join('')+'</div>';
  const rows=d.devs.map(v=>`<tr><td class="l"><span class="nm">${v.name}</span></td><td>${v.sp}</td><td>${v.tickets}</td></tr>`).join('');
  document.getElementById('devs').innerHTML=`<table><thead><tr><th class="l">Dev</th><th>SP</th><th>Tickets</th></tr></thead><tbody>${rows}</tbody></table>`;
}
load();
"""
    return _doc("Velocity", body, js)


def page_services():
    body = """
<div class="top"><div><h1>Services</h1>
  <div class="sub">Auto-generated service context — glossary, data model, endpoints, Kafka.</div></div></div>
<div class="cards" id="cards"></div>
<div id="svcs" class="state">Loading…</div>
"""
    js = r"""
function fmt(s){ if(!s)return''; const d=new Date(s); return isNaN(d)?'':d.toLocaleDateString('en-US',{month:'short',day:'numeric'}); }
async function load(){
  const d=await (await fetch('/api/services')).json();
  document.getElementById('cards').innerHTML=
    [['Services',d.total],['Brief sections',d.services.reduce((a,s)=>a+s.sections.length,0)]]
    .map(([l,v])=>`<div class="card"><div class="l">${l}</div><div class="v">${v}</div></div>`).join('');
  document.getElementById('svcs').innerHTML=d.services.map(s=>`
    <div class="panel" style="margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:10px;padding:12px 4px 8px;border-bottom:1px solid #f1ede7">
        <span class="nm" style="font-size:15px">${s.service}</span>
        <span style="color:#9aa6b4;font-size:12px">${s.sections.length} sections · updated ${fmt(s.lastTs)}</span></div>
      <div style="display:flex;flex-wrap:wrap;gap:7px;padding:11px 4px">
        ${s.sections.map(x=>`<span style="background:#f3ede6;border-radius:7px;padding:4px 10px;font-size:12px">${x.title.replace(s.service+' — ','')}</span>`).join('')}
      </div></div>`).join('');
}
load();
"""
    return _doc("Services", body, js)


def page_timeline():
    body = """
<div class="top"><div><h1>Timeline</h1>
  <div class="sub">Any ticket, PR, or feature — its full cross-source history.</div></div></div>
<div class="panel" style="margin-bottom:16px;padding:14px 16px">
  <input id="q" placeholder="ticket key  ·  feature name  ·  PR number  ·  keyword"
    style="width:100%;font:inherit;font-size:14px;border:1px solid #e4e0da;border-radius:10px;padding:10px">
</div>
<div class="panel"><div id="tl" class="soon"><div class="ic">🧵</div><h2>Trace anything</h2><p>Search a subject to see every event across GitHub, Jira, Slack, Confluence.</p></div></div>
"""
    js = r"""
const ic={github:'🔀',jira:'📋',slack:'💬',confluence:'📄',meeting:'🗣️',service:'🧱'};
function fmt(s){ const d=new Date(s); return isNaN(d)?s:d.toLocaleString('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}); }
let timer=null;
document.getElementById('q').addEventListener('input',e=>{ clearTimeout(timer); timer=setTimeout(()=>run(e.target.value),350); });
async function run(q){
  if(!q||q.trim().length<2){return;}
  document.getElementById('tl').className='state'; document.getElementById('tl').innerHTML='Searching…';
  const d=await (await fetch('/api/timeline?q='+encodeURIComponent(q))).json();
  if(!d.events.length){ document.getElementById('tl').innerHTML='No events for “'+q+'”.'; return; }
  const chips=d.subjects.map(s=>`<span style="background:#f3ede6;border-radius:6px;padding:2px 8px;font-size:11px;margin-right:6px">${s.subject.split('/').pop()} · ${s.count}</span>`).join('');
  const rows=d.events.map(e=>`<div style="display:flex;gap:12px;padding:9px 4px;border-bottom:1px solid #f1ede7">
    <div style="width:130px;color:#9aa6b4;font-size:12px;white-space:nowrap">${fmt(e.ts)}</div>
    <div style="width:22px">${ic[e.source]||'•'}</div>
    <div style="flex:1"><span style="color:#6b7280;font-size:12px">${e.type}</span> · ${e.actor}
      ${e.title?`<div style="font-size:13px">${e.url?`<a href="${e.url}" target="_blank" style="color:#c2452f;text-decoration:none">${e.title}</a>`:e.title}</div>`:''}</div></div>`).join('');
  document.getElementById('tl').className='';
  document.getElementById('tl').innerHTML=`<div style="padding:12px 12px 4px">${chips}</div>`
    +`<div style="padding:4px 12px 12px">${rows}</div>`;
}
"""
    return _doc("Timeline", body, js)


def page_soon(title, icon, note):
    body = f'<div class="top"><div><h1>{title}</h1></div></div>' \
           f'<div class="panel"><div class="soon"><div class="ic">{icon}</div>' \
           f'<h2>{title}</h2><p>{note}</p></div></div>'
    return _doc(title, body)


PAGES = {
    "/people": ("people", page_people),
    "/pr-friction": ("pr", page_pr_friction),
    "/releases": ("releases", page_releases),
    "/docs": ("docs", page_docs),
    "/meetings": ("meetings", page_meetings),
    "/ask": ("ask", page_ask),
    "/topics": ("topics", page_topics),
    "/velocity": ("velocity", page_velocity),
    "/services": ("services", page_services),
    "/timeline": ("timeline", page_timeline),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if path == "/api/people":
                self._send(json.dumps(people_activity(int(qs.get("days", ["30"])[0]))).encode(),
                           "application/json")
                return
            if path == "/api/pr-friction":
                self._send(json.dumps(pr_friction_report()).encode(), "application/json")
                return
            if path == "/api/releases":
                self._send(json.dumps(releases_report()).encode(), "application/json")
                return
            if path == "/api/docs":
                self._send(json.dumps(docs_report()).encode(), "application/json")
                return
            if path == "/api/meetings":
                self._send(json.dumps(meetings_report()).encode(), "application/json")
                return
            if path == "/api/ask-answer":
                self._send(json.dumps(ask_answer()).encode(), "application/json")
                return
            if path == "/api/topics":
                self._send(json.dumps(topics_report(scope=qs.get("scope", ["active"])[0])).encode(),
                           "application/json")
                return
            if path == "/api/velocity":
                self._send(json.dumps(velocity_report()).encode(), "application/json")
                return
            if path == "/api/services":
                self._send(json.dumps(services_report()).encode(), "application/json")
                return
            if path == "/api/timeline":
                self._send(json.dumps(timeline_report(qs.get("q", [""])[0])).encode(),
                           "application/json")
                return
            if path in PAGES:
                active, builder = PAGES[path]
                html = synapse_nav.inject_html(builder(), active)
                self._send(html.encode())
                return
            self._send(b"not found", code=404)
        except Exception as e:
            self._send(json.dumps({"error": str(e)}).encode(), "application/json", 500)

    def do_POST(self):
        if urlparse(self.path).path == "/api/ask-dump":
            try:
                n = int(self.headers.get("Content-Length", 0))
                q = json.loads(self.rfile.read(n) or b"{}").get("question", "").strip()
                with open(os.path.join(ROOT, "derived", "ask-request.json"), "w") as f:
                    json.dump({"question": q}, f)
                self._send(json.dumps({"ok": True}).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"error": str(e)}).encode(), "application/json", 500)
            return
        self._send(b"not found", code=404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("SYNAPSE_PAGES_PORT", 8768))
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

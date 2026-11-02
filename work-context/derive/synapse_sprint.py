"""Synapse /reviews/sprints: sprint-over-sprint productivity per developer, with trend.

Reuses retro_engine.sprint_retro per sprint (delivered SP vs leave/on-call adjusted capacity,
judged against the SDE level band). Closed sprints are cached to derived/sprint-trend/<id>.json
(immutable); the active sprint is recomputed when older than 6 hours. The first build runs in a
background thread and the page polls.

Read-only on Jira. Hot-reloaded by synapse_pages._fresh().
"""
import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "derive"))
sys.path.insert(0, ROOT)
CACHE = os.path.join(ROOT, "derived", "sprint-trend")
FROM = "2025-10-01"
# The project key comes from config like everywhere else in this tree. It was hardcoded,
# which is both a leak (it named the org's board in source that publishes) and a lie:
# every other module reads it, so this one disagreed with them the moment it changed.
def _prefix():
    import capacity_engine as ce
    return ce.JIRA_PROJECT or ""


PREFIX = _prefix()
ACTIVE_TTL = 6 * 3600

_state = {"running": False, "done": 0, "total": 0, "error": None, "started": 0}
_lock = threading.Lock()


def _team():
    import yaml
    with open(os.path.join(ROOT, "config", "people.yaml")) as f:
        ppl = yaml.safe_load(f).get("people", [])
    return {p["canonical"]: {"name": p.get("name"), "role": p.get("role", "")}
            for p in ppl if p.get("canonical") and p.get("scope") == "team" and p.get("role")}


def _sprints():
    import retro_engine as r
    ss = [s for s in r.board_sprints(limit=200)
          if s["name"].startswith(PREFIX) and s["start"] and s["start"] >= FROM]
    ss.sort(key=lambda s: s["start"])
    return ss


def _path(sid):
    return os.path.join(CACHE, f"{sid}.json")


def _cached(s):
    p = _path(s["id"])
    if not os.path.exists(p):
        return None
    if s["state"] != "closed" and time.time() - os.path.getmtime(p) > ACTIVE_TTL:
        return None
    with open(p) as f:
        d = json.load(f)
    return d if "flow" in d else None


IN_PROGRESS = {"In Progress", "Work In Progress"}


def _ts(x):
    from datetime import datetime
    return datetime.strptime(x[:23] + x[23:].replace(":", ""), "%Y-%m-%dT%H:%M:%S.%f%z") if x else None


def _acct():
    import yaml
    with open(os.path.join(ROOT, "config", "people.yaml")) as f:
        return {str(p["jira_id"]): p["canonical"] for p in yaml.safe_load(f).get("people", [])
                if p.get("jira_id") and p.get("canonical")}


def _flow(s, ends):
    """Per dev for one sprint, from the tickets' Jira changelog:
    planned (in the sprint by the end of its first day) vs added later (unplanned), what closed here vs
    carried out, work carried in from earlier sprints, and cycle time (first In Progress -> Done)."""
    import statistics
    from datetime import date, timedelta
    import capacity_engine as ce
    import retro_engine as r
    SPRINT, _ = r._fields()
    acct = _acct()
    cutoff = date.fromisoformat(s["start"]) + timedelta(days=1)
    issues, tok = [], None
    for _ in range(20):
        body = {"jql": f"sprint = {s['id']} and issuetype in (Story,Task,Bug)", "maxResults": 100,
                "fields": [ce.SP_FIELD, "assignee", "created", "resolutiondate", "status", SPRINT],
                "expand": "changelog"}
        if tok:
            body["nextPageToken"] = tok
        d = ce._jira("POST", "/rest/api/3/search/jql", body)
        issues += d.get("issues", [])
        tok = d.get("nextPageToken")
        if d.get("isLast") or not tok:
            break
    out = {}
    for i in issues:
        f = i["fields"]
        who = acct.get(str(((f.get("assignee") or {}) or {}).get("accountId") or ""))
        status = ((f.get("status") or {}) or {}).get("name") or ""
        if not who or status == "Cancelled":
            continue
        sp = float(f.get(ce.SP_FIELD) or 0)
        sprints = f.get(SPRINT) or []
        last = max(sprints, key=lambda x: ends.get(x.get("id"), "") or "") if sprints else {}
        done_here = status == "Done" and last.get("id") == s["id"]
        hist = sorted(((i.get("changelog") or {}).get("histories") or []), key=lambda h: h["created"])
        added, started, done_at = None, None, None
        for h in hist:
            for it in h["items"]:
                if it["field"] == "Sprint" and added is None and s["name"] in (it.get("toString") or "") \
                        and s["name"] not in (it.get("fromString") or ""):
                    added = _ts(h["created"])
                if it["field"] == "status" and it.get("toString") in IN_PROGRESS and started is None:
                    started = _ts(h["created"])
                if it["field"] == "status" and it.get("toString") == "Done":
                    done_at = _ts(h["created"])
        added = added or _ts(f.get("created"))
        planned = added is None or added.date() <= cutoff
        prior = any(ends.get(x.get("id"), "9999") < s["start"] for x in sprints if x.get("id") != s["id"])
        o = out.setdefault(who, {"committedSP": 0.0, "addedSP": 0.0, "donePlannedSP": 0.0, "doneAddedSP": 0.0,
                                 "carriedOutSP": 0.0, "carriedInSP": 0.0, "cycle": [], "spans": [],
                                 "addedTickets": 0, "doneTickets": 0})
        o["committedSP" if planned else "addedSP"] += sp
        if not planned:
            o["addedTickets"] += 1
        if planned and prior:
            o["carriedInSP"] += sp
        if done_here:
            o["donePlannedSP" if planned else "doneAddedSP"] += sp
            o["doneTickets"] += 1
            o["spans"].append(len(sprints))
            if started and done_at and done_at > started:
                o["cycle"].append(round((done_at - started).total_seconds() / 86400, 1))
        else:
            o["carriedOutSP"] += sp
    for o in out.values():
        o["cycleMedian"] = round(statistics.median(o["cycle"]), 1) if o["cycle"] else None
        o["multiSprint"] = sum(1 for x in o["spans"] if x > 1)
        for k in ("committedSP", "addedSP", "donePlannedSP", "doneAddedSP", "carriedOutSP", "carriedInSP"):
            o[k] = round(o[k], 2)
        del o["spans"]
    return out


def _compute(s, ends=None):
    import retro_engine as r
    d = r.sprint_retro([s["id"]])
    if d.get("__error__"):
        raise RuntimeError(d["__error__"])
    keep = {"sprint": s, "totals": d.get("totals"), "people": [
        {k: p.get(k) for k in ("name", "canonical", "role", "capacitySP", "netDays", "leave", "oncallDays",
                               "delivered", "oncallSP", "carried", "cancelled", "tickets", "unestimated",
                               "efficiency", "vsLevel", "band")}
        for p in (d.get("people") or []) if not p.get("offRoster")]}
    if ends is None:
        ends = {x["id"]: x["end"] for x in r.board_sprints(limit=300)}
    keep["flow"] = _flow(s, ends)
    os.makedirs(CACHE, exist_ok=True)
    with open(_path(s["id"]), "w") as f:
        json.dump(keep, f)
    return keep


def _build(force=False):
    try:
        import retro_engine as r
        ss = _sprints()
        ends = {x["id"]: x["end"] for x in r.board_sprints(limit=300)}
        _state.update(total=len(ss), done=0, error=None)
        for s in ss:
            if force and s["state"] != "closed" or not _cached(s):
                _compute(s, ends)
            _state["done"] += 1
    except Exception as e:
        _state["error"] = str(e)
    finally:
        _state["running"] = False


def _start(force=False):
    with _lock:
        if _state["running"]:
            return
        _state.update(running=True, started=time.time())
        threading.Thread(target=_build, args=(force,), daemon=True).start()


def api(qs):
    if (qs.get("refresh") or [""])[0] == "1":
        _start(force=True)
    if _state["running"]:
        return {"status": "building", "done": _state["done"], "total": _state["total"]}
    try:
        ss = _sprints()
    except Exception as e:
        return {"status": "error", "error": f"Jira: {e}"}
    rows = [c for c in (_cached(s) for s in ss)]
    if any(r is None for r in rows):
        _start()
        return {"status": "building", "done": sum(1 for r in rows if r), "total": len(ss)}
    team = _team()
    sprints = [r["sprint"] for r in rows]
    devs = []
    for c, p in team.items():
        per = []
        for r in rows:
            x = next((q for q in r["people"] if q.get("canonical") == c), None)
            per.append(x and {k: x.get(k) for k in ("capacitySP", "netDays", "leave", "oncallDays", "delivered",
                                                    "oncallSP", "carried", "tickets", "efficiency", "vsLevel")}
                       | {"low": (x.get("band") or {}).get("low"), "high": (x.get("band") or {}).get("high")}
                       | ((r.get("flow") or {}).get(c) or {}))
        if not any(per):
            continue
        # Sprints before the dev's first ticket on this board are not measurable here (they were on
        # another team or board): capacity is counted but nothing can land. Leave them out.
        first = next((i for i, x in enumerate(per) if x and (x["tickets"] or x["carried"] or x["delivered"])), None)
        if first is None:
            continue
        per = [None if i < first else x for i, x in enumerate(per)]
        devs.append({"id": c, "name": p["name"], "role": p["role"], "sprints": per,
                     "firstSprint": rows[first]["sprint"]["name"]})
    return {"status": "ok", "sprints": sprints, "devs": devs,
            "team": [r["totals"] for r in rows], "error": _state["error"]}


CSS = r"""
:root{--bg:#f3f6fa;--surface:#ffffff;--surface2:#eef2f8;--border:#e4e9f2;--text:#15202e;--muted:#5b6675;--hint:#9aa6b4;
--accent:#3a55d9;--accent-2:#2c41b8;--accent-soft:rgba(58,85,217,.07);--ok:#3b6b3f;--ok-bg:#e9f1e8;--warn:#866113;--warn-bg:#f6efdd;
--danger:#b23a4a;--danger-bg:#f8edef;--shadow:0 1px 2px rgba(20,32,46,.04),0 2px 6px rgba(20,32,46,.05);--radius:14px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:'Hanken Grotesk',-apple-system,system-ui,sans-serif;font-size:14px;line-height:1.45}
.wrap{max-width:1180px;margin:0 auto;padding:6px 24px 60px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:12px}
h2{font-size:15px;font-weight:700;margin:0}.sub{color:var(--muted);font-size:12.5px}
.ctl{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.seg{display:inline-flex;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:3px}
.seg button{font:inherit;font-size:12.5px;border:0;background:none;padding:6px 12px;border-radius:7px;cursor:pointer;color:var(--muted)}
.seg button.on{background:var(--accent-soft);color:var(--accent-2);font-weight:600}
select,.btn{font:inherit;font-size:12.5px;border:1px solid var(--border);border-radius:8px;padding:5px 9px;background:var(--surface);cursor:pointer}
.cards{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));margin-bottom:16px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:12px 16px}
.card .l{font-size:10px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.card .v{font-size:22px;font-weight:700;font-family:'IBM Plex Mono',monospace;margin-top:2px}.card .h{font-size:11.5px;color:var(--hint)}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:14px 18px;margin-bottom:16px}
.how{font-size:12px;color:var(--muted);margin:4px 0 10px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--muted)}
.legend i{display:inline-block;width:12px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.mini{font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);margin:10px 0 2px}
.tgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(520px,1fr));gap:14px}
.tcard{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:14px 16px}
.nm{font-weight:600}.role{color:var(--hint);font-size:11px;margin-left:6px}
.delta{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:6px 0 6px}
.delta b{font-family:'IBM Plex Mono',monospace;color:var(--text)}
.up{color:var(--ok);font-weight:600}.down{color:var(--danger);font-weight:600}.flat{color:var(--hint)}
table{width:100%;border-collapse:collapse}
th{font-size:10px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);font-weight:600;padding:7px 4px;border-bottom:1px solid var(--border);white-space:nowrap;text-align:center}
th.l,td.l{text-align:left}
td{padding:5px 4px;border-bottom:1px solid var(--border);text-align:center;font-family:'IBM Plex Mono',monospace;font-size:12px}
td.cell{border-radius:6px}
.mid{background:#eef1fb}
svg text{font-family:'IBM Plex Mono',monospace;font-size:10px;fill:#5b6675}
#tip{position:fixed;pointer-events:none;background:var(--text);color:#fff;font-size:12px;padding:7px 10px;border-radius:8px;max-width:320px;display:none;z-index:50}
.empty{color:var(--hint);padding:30px;text-align:center}
.zm-bg{position:fixed;inset:0;background:rgba(21,32,46,.45);display:flex;align-items:center;justify-content:center;z-index:40}
.zm{background:#fff;border-radius:14px;padding:16px 20px 18px;max-width:96vw;max-height:92vh;overflow:auto;box-shadow:0 10px 40px rgba(20,32,46,.25)}
.zm-h{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:8px}
.zm-h b{font-size:15px}.zm-x{font:inherit;font-size:18px;line-height:1;border:0;background:var(--surface2);border-radius:8px;width:30px;height:30px;cursor:pointer;color:var(--muted)}
.zm table{margin-top:12px}.zm td,.zm th{font-size:12px}
.zoomable{cursor:zoom-in}

.bar{height:6px;background:var(--surface2);border-radius:4px;overflow:hidden;margin-top:10px}.bar>i{display:block;height:100%;background:var(--accent)}
"""

BODY = r"""
<div class="top">
  <div><h2>Sprint productivity</h2>
    <div class="sub">Story points each dev closed per sprint, against their capacity after leave and on-call, and the band expected at their level. Sprints from Oct 2025.</div></div>
  <div class="ctl">
    <div class="seg" id="view"><button data-v="cards" class="on">Per dev trend</button><button data-v="flow">Flow and interruptions</button><button data-v="matrix">Sprint matrix</button></div>
    Sort <select id="sort"><option value="name">Name</option><option value="eff">Mid-year efficiency</option><option value="improve">Most improved</option><option value="sp">Mid-year SP delivered</option><option value="carry">Carry-over rate (lowest first)</option><option value="unpl">Unplanned share (highest first)</option><option value="cycle">Cycle time (fastest first)</option></select>
    <button class="btn" id="refresh" title="Recompute the active sprint from Jira">Refresh</button>
  </div>
</div>
<div class="cards" id="cards"></div>
<div id="main"><div class="panel empty">Loading…</div></div>
<div id="tip"></div>
"""

JS = r"""
let D=null, V='cards', SORT='name';
function zoom(title,html,after){ closeZoom();
  const bg=document.createElement('div'); bg.className='zm-bg'; bg.id='zm';
  bg.innerHTML=`<div class="zm"><div class="zm-h"><b>${title}</b><button class="zm-x" title="Close (Esc)">×</button></div>${html}</div>`;
  bg.onclick=e=>{if(e.target===bg||e.target.closest('.zm-x'))closeZoom();};
  document.body.appendChild(bg); placeZoom(bg); after&&after(bg); }
// Inside the Reviews tab the page is an iframe sized to its full height, so position:fixed centres on the
// whole frame. Pin the overlay to the slice of the frame the user can see and freeze the parent's scroll.
function placeZoom(bg){ const fe=window.frameElement; if(!fe) return;
  try{ const r=fe.getBoundingClientRect(), ph=parent.innerHeight, top=Math.max(0,-r.top), h=Math.min(ph, r.bottom)-Math.max(0,r.top);
    Object.assign(bg.style,{position:'absolute',top:top+'px',left:'0',right:'0',bottom:'auto',height:h+'px'});
    document.body.style.position='relative'; bg.querySelector('.zm').style.maxHeight=(h-30)+'px';
    parent.document.documentElement.style.overflow='hidden'; }catch(e){} }
function closeZoom(){const z=document.getElementById('zm'); z&&z.remove(); try{if(window.frameElement)parent.document.documentElement.style.overflow='';}catch(e){} const t=document.getElementById('tip'); if(t)t.style.display='none';}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeZoom();});
const ZW=()=>Math.max(680,Math.min(1150,innerWidth-110));

const HX={del:'#2a78d6',onc:'#1baf7a',band:'#e7ecf8',bandline:'#9fb0e6',border:'#e4e9f2',mid:'#f3f5fd',cap:'#15202e'};
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const tip=document.getElementById('tip');
document.addEventListener('mousemove',e=>{const t=e.target.closest('[data-tip]'); if(!t){tip.style.display='none';return;}
  tip.innerHTML=t.dataset.tip; tip.style.display='block'; tip.style.left=Math.min(e.clientX+14,innerWidth-330)+'px'; tip.style.top=(e.clientY+14)+'px';});
const parts=n=>{const m=String(n).match(/([A-Z]{3})\s*'?\s*(\d\d)\s*S(\d+)/); return m?{q:m[1]+" '"+m[2],s:'S'+m[3]}:{q:'',s:n};};
const short=n=>{const x=parts(n); return x.q?x.q+' '+x.s:n;};
const inH1=s=>s.start>='2026-04-01'&&s.start<='2026-09-30';
const EARLY=s=>s.start>='2026-04-01'&&s.start<'2026-07-01', LATE=s=>s.start>='2026-07-01'&&s.start<='2026-09-30';
function agg(d,pred){let del=0,cap=0,onc=0,n=0,within=0,carried=0; D.sprints.forEach((s,i)=>{const x=d.sprints[i]; if(!x||!pred(s))return;
  del+=x.delivered||0; cap+=x.capacitySP||0; onc+=x.oncallSP||0; n++; if(x.vsLevel==='within'||x.vsLevel==='above')within++; carried=x.carried||0;});
  return {del:+del.toFixed(1),cap:+cap.toFixed(1),onc:+onc.toFixed(1),n,within,eff:cap?Math.round(100*del/cap):null};}
function arrow(a,b){ if(a==null||b==null) return '<span class="flat">not enough data</span>';
  const d=b-a; return `<b>${a}%</b> → <b>${b}%</b> `+(Math.abs(d)<5?'<span class="flat">steady</span>':`<span class="${d>0?'up':'down'}">${d>0?'improved':'dropped'}</span>`);}

async function load(refresh){
  const r=await (await fetch('/api/reviews/sprints'+(refresh?'?refresh=1':''))).json();
  if(r.status==='building'){document.getElementById('main').innerHTML=`<div class="panel empty">Reading sprints from Jira… ${r.done} of ${r.total||'?'}<div class="bar"><i style="width:${r.total?100*r.done/r.total:5}%"></i></div></div>`; setTimeout(()=>load(false),2500); return;}
  if(r.status==='error'){document.getElementById('main').innerHTML=`<div class="panel empty">${esc(r.error)}</div>`;return;}
  D=r; render();
}
function render(){
  const H=D.sprints.map((s,i)=>[s,D.team[i]]).filter(([s])=>inH1(s));
  const A=D.devs.map(d=>agg(d,inH1)), del=A.reduce((a,x)=>a+x.del,0), cap=A.reduce((a,x)=>a+x.cap,0), car=H.length?H[H.length-1][1].carriedSP:0;
  const within=D.devs.reduce((a,d)=>a+agg(d,inH1).within,0), slots=D.devs.reduce((a,d)=>a+agg(d,inH1).n,0);
  document.getElementById('cards').innerHTML=[['Mid-year sprints',H.length,'Apr to Sep'],['SP delivered',Math.round(del),'team, excluding on-call'],
    ['Team efficiency',cap?Math.round(100*del/cap)+'%':'n/a','delivered vs capacity'],['At or above level band',slots?Math.round(100*within/slots)+'%':'n/a','of dev-sprints'],
    ['Carried at end',Math.round(car||0)+' SP','open in the last mid-year sprint']]
    .map(([l,v,h])=>`<div class="card"><div class="l">${l}</div><div class="v">${v}</div><div class="h">${h}</div></div>`).join('');
  V==='cards'?cards():V==='flow'?flow():matrix();
}
function sorted(){
  const k={name:d=>d.name, eff:d=>-(agg(d,inH1).eff??-1), sp:d=>-agg(d,inH1).del,
    improve:d=>{const a=agg(d,EARLY).eff,b=agg(d,LATE).eff; return a==null||b==null?999:-(b-a);},
    carry:d=>fagg(d,inH1).carry??999, unpl:d=>-(fagg(d,inH1).unpl??-1), cycle:d=>fagg(d,inH1).cycle??999}[SORT];
  return [...D.devs].sort((a,b)=>{const x=k(a),y=k(b); return typeof x==='string'?x.localeCompare(y):x-y;});
}
function chart(d,w=500,h=150,big=false){
  const S=D.sprints, m={l:30,r:6,t:10,b:34}, n=S.length, cw=(w-m.l-m.r)/n, bw=Math.max(6,cw-(big?14:6));
  const vals=d.sprints.map(x=>x?Math.max((x.delivered||0)+(x.oncallSP||0),x.high||0,x.capacitySP||0):0);
  const ymax=Math.max(5,...vals)*1.1, Y=v=>h-m.b-v/ymax*(h-m.t-m.b), X=i=>m.l+i*cw+cw/2;
  let s=`<svg width="${w}" height="${h}" style="display:block">`;
  S.forEach((sp,i)=>{ if(inH1(sp)) s+=`<rect x="${m.l+i*cw}" y="${m.t}" width="${cw}" height="${h-m.t-m.b}" fill="${HX.mid}"/>`; });
  [0,Math.round(ymax/2),Math.floor(ymax)].forEach(v=>s+=`<line x1="${m.l}" x2="${w-m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${HX.border}"/><text x="${m.l-4}" y="${Y(v)+3}" text-anchor="end">${v}</text>`);
  d.sprints.forEach((x,i)=>{ const sp=S[i];
    if(!x){ s+=`<text x="${X(i)}" y="${h-m.b-3}" text-anchor="middle" data-tip="${esc(sp.name)}: no tickets on the board yet (first: ${esc(d.firstSprint)})">·</text>`; return; }
    if(x.low!=null&&x.high!=null) s+=`<rect x="${X(i)-bw/2-2}" y="${Y(x.high)}" width="${bw+4}" height="${Math.max(1,Y(x.low)-Y(x.high))}" fill="${HX.band}" stroke="${HX.bandline}" stroke-dasharray="2 2"/>`;
    const tipx=`<b>${esc(sp.name)}</b> (${sp.start.slice(5)} to ${sp.end.slice(5)})<br>Delivered ${x.delivered} SP${x.oncallSP?' + on-call '+x.oncallSP:''}<br>Capacity ${x.capacitySP} SP (${x.netDays} days; leave ${x.leave}, on-call ${x.oncallDays})<br>Level band ${x.low??'?'} to ${x.high??'?'} SP: <b>${x.vsLevel||'n/a'}</b><br>Efficiency ${x.efficiency??'n/a'}% · ${x.tickets} tickets · carried ${x.carried} SP`;
    const y0=h-m.b, hd=y0-Y(x.delivered||0), ho=y0-Y(x.oncallSP||0);
    if(hd>0) s+=`<rect x="${X(i)-bw/2}" y="${y0-hd}" width="${bw}" height="${hd}" rx="2" fill="${HX.del}" data-tip="${tipx}"/>`;
    if(ho>0) s+=`<rect x="${X(i)-bw/2}" y="${y0-hd-ho}" width="${bw}" height="${Math.max(1,ho-1)}" rx="2" fill="${HX.onc}" data-tip="${tipx}"/>`;
    if(x.capacitySP) s+=`<line x1="${X(i)-bw/2-3}" x2="${X(i)+bw/2+3}" y1="${Y(x.capacitySP)}" y2="${Y(x.capacitySP)}" stroke="${HX.cap}" stroke-width="2" data-tip="${tipx}"/>`;
    s+=`<rect x="${X(i)-cw/2}" y="${m.t}" width="${cw}" height="${h-m.t-m.b}" fill="transparent" data-tip="${tipx}"/>`;
  });
  let lastQ='';
  S.forEach((sp,i)=>{ const x=parts(sp.name); if(big||cw>=20||i%2===0) s+=`<text x="${X(i)}" y="${h-m.b+13}" text-anchor="middle">${esc(x.s)}</text>`;
    if(x.q!==lastQ){ s+=`<text x="${m.l+i*cw+2}" y="${h-m.b+27}" style="font-weight:600">${esc(x.q)}</text>`; lastQ=x.q; } });
  return s+'</svg>';
}

function zoomDev(d){
  const rows=D.sprints.map((sp,i)=>{const x=d.sprints[i]; if(!x) return '';
    return `<tr class="${inH1(sp)?'mid':''}"><td class="l">${esc(short(sp.name))}</td><td>${sp.start.slice(5)} to ${sp.end.slice(5)}</td><td>${x.netDays}</td><td>${x.leave}</td><td>${x.oncallDays}</td><td>${x.capacitySP}</td><td>${x.low??''} to ${x.high??''}</td><td><b>${x.delivered}</b></td><td>${x.oncallSP||0}</td><td>${x.efficiency??''}%</td><td>${x.vsLevel||''}</td><td>${x.tickets}</td><td>${x.carried}</td></tr>`;}).join('');
  zoom(`${esc(d.name)} <span class="role">${esc(d.role)}</span>`, chart(d,ZW(),380,true)
    +`<table><thead><tr><th class="l">Sprint</th><th>Dates</th><th>Net days</th><th>Leave</th><th>On-call days</th><th>Capacity SP</th><th>Level band SP</th><th>Delivered</th><th>On-call SP</th><th>Efficiency</th><th>vs level</th><th>Tickets</th><th>Carried SP</th></tr></thead><tbody>${rows}</tbody></table>`);
}
function cards(){
  const html=sorted().map(d=>{const h=agg(d,inH1), e=agg(d,EARLY), l=agg(d,LATE);
    return `<div class="tcard"><div><span class="nm">${esc(d.name)}</span><span class="role">${esc(d.role)}</span>${d.firstSprint&&d.firstSprint!==D.sprints[0].name?`<span class="role">· on the board from ${esc(short(d.firstSprint))}</span>`:''}</div>
    <div class="delta"><span>Mid-year: <b>${h.del}</b> SP, efficiency <b>${h.eff??'n/a'}%</b>, at or above band in <b>${h.within}/${h.n}</b> sprints</span>
    <span>Efficiency Apr to Jun vs Jul to Sep: ${arrow(e.eff,l.eff)}</span></div><div class="zoomable" data-zoom="${d.id}" title="Click to enlarge">${chart(d)}</div></div>`;}).join('');
  document.getElementById('main').innerHTML=`<div class="panel" style="padding:12px 18px"><div class="legend">
    <span><i style="background:${HX.del}"></i>Delivered SP</span><span><i style="background:${HX.onc}"></i>On-call SP</span>
    <span><i style="background:${HX.cap};height:3px;vertical-align:3px"></i>Capacity (after leave and on-call)</span>
    <span><i style="background:${HX.band};border:1px dashed ${HX.bandline}"></i>Expected band for their level</span>
    <span><i style="background:${HX.mid};border:1px solid ${HX.border}"></i>Mid-year window</span></div>
    <p class="how" style="margin:8px 0 0">A bar inside or above its band is on track for the level. Sprints before a dev's first ticket on the board are left out (shown as a dot). Points count only when a ticket closes, so long tickets show as a gap then a spike; look at the trend across several sprints, not one. Hover a sprint for capacity, leave, on-call and carried work.</p></div>
    <div class="tgrid">${html}</div>`;
  document.querySelectorAll('[data-zoom]').forEach(z=>z.onclick=()=>zoomDev(D.devs.find(x=>x.id===z.dataset.zoom)));
}

const FX={plan:'#2a78d6',add:'#eb6834',out:'#cfd6e2',line:'#3a55d9'};
const med=a=>{if(!a.length)return null; const b=[...a].sort((x,y)=>x-y), m=Math.floor(b.length/2); return +(b.length%2?b[m]:(b[m-1]+b[m])/2).toFixed(1);};
function fagg(d,pred){let c=0,a=0,dp=0,da=0,co=0,ci=0,cyc=[],dt=0,ms=0;
  D.sprints.forEach((s,i)=>{const x=d.sprints[i]; if(!x||!pred(s)||x.committedSP==null)return;
    c+=x.committedSP; a+=x.addedSP; dp+=x.donePlannedSP; da+=x.doneAddedSP; co+=x.carriedOutSP; ci+=x.carriedInSP; cyc=cyc.concat(x.cycle||[]); dt+=x.doneTickets||0; ms+=x.multiSprint||0;});
  const tot=c+a, done=dp+da;
  return {committed:+c.toFixed(1),added:+a.toFixed(1),done:+done.toFixed(1),carried:+co.toFixed(1),
    carry:tot?Math.round(100*co/tot):null, unpl:done?Math.round(100*da/done):null, cycle:med(cyc), multi:dt?Math.round(100*ms/dt):null,
    planHit:c?Math.round(100*dp/c):null};}
function arrowLow(a,b,unit,tol){ if(a==null||b==null) return '<span class="flat">not enough data</span>';
  const d=b-a; return `<b>${a}${unit}</b> → <b>${b}${unit}</b> `+(Math.abs(d)<tol?'<span class="flat">steady</span>':`<span class="${d<0?'up':'down'}">${d<0?'improved':'worse'}</span>`);}
function flowCharts(d,w=500,h=130,big=false){
  const S=D.sprints, m={l:30,r:6,t:10,b:34}, n=S.length, cw=(w-m.l-m.r)/n, bw=Math.max(6,cw-(big?14:6)), X=i=>m.l+i*cw+cw/2;
  const tot=x=>x&&x.committedSP!=null?x.donePlannedSP+x.doneAddedSP+x.carriedOutSP:0;
  const ymax=Math.max(5,...d.sprints.map(tot))*1.08, Y=v=>h-m.b-v/ymax*(h-m.t-m.b);
  let lastQ='', axis='';
  S.forEach((sp,i)=>{const x=parts(sp.name); if(big||cw>=20||i%2===0) axis+=`<text x="${X(i)}" y="${h-m.b+13}" text-anchor="middle">${esc(x.s)}</text>`;
    if(x.q!==lastQ){axis+=`<text x="${m.l+i*cw+2}" y="${h-m.b+27}" style="font-weight:600">${esc(x.q)}</text>`; lastQ=x.q;}});
  const shade=S.map((sp,i)=>inH1(sp)?`<rect x="${m.l+i*cw}" y="${m.t}" width="${cw}" height="${h-m.t-m.b}" fill="${HX.mid}"/>`:'').join('');
  let a=`<svg width="${w}" height="${h}" style="display:block">${shade}`;
  [0,Math.round(ymax/2),Math.floor(ymax)].forEach(v=>a+=`<line x1="${m.l}" x2="${w-m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${HX.border}"/><text x="${m.l-4}" y="${Y(v)+3}" text-anchor="end">${v}</text>`);
  d.sprints.forEach((x,i)=>{ if(!x||x.committedSP==null){a+=`<text x="${X(i)}" y="${h-m.b-3}" text-anchor="middle">·</text>`;return;}
    const sp=S[i], tp=`<b>${esc(S[i].name)}</b><br>Planned at start ${x.committedSP} SP, added later ${x.addedSP} SP (${x.addedTickets} tickets)<br>Closed: ${x.donePlannedSP} planned + ${x.doneAddedSP} added<br>Carried out ${x.carriedOutSP} SP · carried in ${x.carriedInSP} SP<br>Cycle time median ${x.cycleMedian??'n/a'} days`;
    let y=h-m.b; [['donePlannedSP',FX.plan],['doneAddedSP',FX.add],['carriedOutSP',FX.out]].forEach(([k,c])=>{const hh=(h-m.b)-Y(x[k]||0); if(hh<=0)return; y-=hh;
      a+=`<rect x="${X(i)-bw/2}" y="${y+1}" width="${bw}" height="${Math.max(1,hh-2)}" rx="2" fill="${c}" data-tip="${tp}"/>`;});
    a+=`<rect x="${X(i)-cw/2}" y="${m.t}" width="${cw}" height="${h-m.t-m.b}" fill="transparent" data-tip="${tp}"/>`;});
  a+=axis+'</svg>';
  const hc=Math.round(h*.75), cmax=Math.max(10,...d.sprints.map(x=>x&&x.cycleMedian||0)), YC=v=>hc-m.b-v/cmax*(hc-m.t-m.b);
  let c=`<svg width="${w}" height="${hc}" style="display:block">${S.map((sp,i)=>inH1(sp)?`<rect x="${m.l+i*cw}" y="${m.t}" width="${cw}" height="${hc-m.t-m.b}" fill="${HX.mid}"/>`:'').join('')}`;
  [0,Math.round(cmax/2),Math.floor(cmax)].forEach(v=>c+=`<line x1="${m.l}" x2="${w-m.r}" y1="${YC(v)}" y2="${YC(v)}" stroke="${HX.border}"/><text x="${m.l-4}" y="${YC(v)+3}" text-anchor="end">${v}d</text>`);
  let path='',pen=false; d.sprints.forEach((x,i)=>{ if(!x||x.cycleMedian==null){pen=false;return;} path+=(pen?'L':'M')+X(i)+' '+YC(x.cycleMedian)+' '; pen=true;});
  c+=`<path d="${path}" fill="none" stroke="${FX.line}" stroke-width="2"/>`;
  d.sprints.forEach((x,i)=>{ if(x&&x.cycleMedian!=null) c+=`<circle cx="${X(i)}" cy="${YC(x.cycleMedian)}" r="4" fill="${FX.line}" stroke="#fff" stroke-width="2" data-tip="<b>${esc(S[i].name)}</b><br>Median ${x.cycleMedian} days from In Progress to Done<br>${(x.cycle||[]).length} tickets: ${(x.cycle||[]).join(', ')} days"/>`;});
  c+=axis.replace(new RegExp('y="'+(h-m.b+13)+'"','g'),'y="'+(hc-m.b+13)+'"').replace(new RegExp('y="'+(h-m.b+27)+'"','g'),'y="'+(hc-m.b+27)+'"')+'</svg>';
  return `<div class="mini">Sprint work: closed as planned, closed but added mid-sprint, carried out (SP)</div>${a}<div class="mini">Cycle time: median days from In Progress to Done</div>${c}`;
}
function flow(){
  const T=D.devs.map(d=>fagg(d,inH1)), sum=k=>T.reduce((a,x)=>a+(x[k]||0),0);
  const done=sum('done'), allc=D.devs.flatMap(d=>D.sprints.flatMap((s,i)=>inH1(s)&&d.sprints[i]?(d.sprints[i].cycle||[]):[]));
  const addDone=D.devs.reduce((a,d)=>a+D.sprints.reduce((b,s,i)=>b+(inH1(s)&&d.sprints[i]?d.sprints[i].doneAddedSP||0:0),0),0);
  document.getElementById('cards').innerHTML=[['Carry-over rate',(sum('committed')+sum('added'))?Math.round(100*sum('carried')/(sum('committed')+sum('added')))+'%':'n/a','of sprint SP not closed in that sprint'],
    ['Unplanned share',done?Math.round(100*addDone/done)+'%':'n/a','of closed SP was added mid-sprint'],
    ['Median cycle time',(med(allc)??'n/a')+' days','In Progress to Done'],['Tickets closed',allc.length,'with a cycle time, mid-year']]
    .map(([l,v,h])=>`<div class="card"><div class="l">${l}</div><div class="v">${v}</div><div class="h">${h}</div></div>`).join('');
  const html=sorted().map(d=>{const h=fagg(d,inH1), e=fagg(d,EARLY), l=fagg(d,LATE);
    return `<div class="tcard"><div><span class="nm">${esc(d.name)}</span><span class="role">${esc(d.role)}</span></div>
    <div class="delta"><span>Mid-year: carry-over <b>${h.carry??'n/a'}%</b>, unplanned <b>${h.unpl??'n/a'}%</b> of closed SP, cycle <b>${h.cycle??'n/a'}</b> days, planned work closed <b>${h.planHit??'n/a'}%</b></span>
    <span>Carry-over: ${arrowLow(e.carry,l.carry,'%',5)}</span><span>Cycle time: ${arrowLow(e.cycle,l.cycle,'d',1)}</span></div>
    <div class="zoomable" data-zoomf="${d.id}" title="Click to enlarge">${flowCharts(d)}</div></div>`;}).join('');
  document.getElementById('main').innerHTML=`<div class="panel" style="padding:12px 18px"><div class="legend">
    <span><i style="background:${FX.plan}"></i>Closed, planned at sprint start</span><span><i style="background:${FX.add}"></i>Closed, added mid-sprint (unplanned)</span>
    <span><i style="background:${FX.out}"></i>Carried out to a later sprint</span><span><i style="background:${HX.mid};border:1px solid ${HX.border}"></i>Mid-year window</span></div>
    <p class="how" style="margin:8px 0 0"><b>Carry-over</b>: share of a dev's sprint SP not closed in that sprint (lower is better). <b>Unplanned</b>: share of closed SP added after the sprint's first day, which shows who absorbs interruptions. <b>Cycle time</b>: median days from first In Progress to Done. A ticket taken to Done the same day it started usually means the board was updated after the work, not that the work took hours. From the Jira changelog of every ticket in each sprint.</p></div>
    <div class="tgrid">${html}</div>`;
  document.querySelectorAll('[data-zoomf]').forEach(z=>z.onclick=()=>zoomFlow(D.devs.find(x=>x.id===z.dataset.zoomf)));
}
function zoomFlow(d){
  const rows=D.sprints.map((sp,i)=>{const x=d.sprints[i]; if(!x||x.committedSP==null) return '';
    const tot=x.committedSP+x.addedSP;
    return `<tr class="${inH1(sp)?'mid':''}"><td class="l">${esc(short(sp.name))}</td><td>${x.committedSP}</td><td>${x.addedSP}</td><td>${x.carriedInSP}</td><td><b>${x.donePlannedSP}</b></td><td>${x.doneAddedSP}</td><td>${x.carriedOutSP}</td><td>${tot?Math.round(100*x.carriedOutSP/tot)+'%':''}</td><td>${x.cycleMedian??''}</td><td>${x.doneTickets}</td><td>${x.multiSprint}</td></tr>`;}).join('');
  zoom(`${esc(d.name)} <span class="role">${esc(d.role)}</span> · flow`, flowCharts(d,ZW(),300,true)
    +`<table><thead><tr><th class="l">Sprint</th><th>Planned SP</th><th>Added SP</th><th>Carried in</th><th>Closed planned</th><th>Closed added</th><th>Carried out</th><th>Carry-over</th><th>Cycle days</th><th>Tickets closed</th><th>Spanned 2+ sprints</th></tr></thead><tbody>${rows}</tbody></table>`);
}
function matrix(){
  const S=D.sprints, col=v=>v==null?'':v>=100?'#c9dcf5':v>=75?'#dce8f8':v>=50?'#eef3fb':v>=25?'#f8f0df':'#f8e6e8';
  const head='<th class="l">Developer</th>'+S.map(s=>`<th class="${inH1(s)?'mid':''}" title="${esc(s.name)} ${s.start} to ${s.end}">${esc(short(s.name))}</th>`).join('')+'<th>Mid-year</th>';
  const rows=sorted().map(d=>{const h=agg(d,inH1);
    return `<tr><td class="l"><span class="nm">${esc(d.name)}</span><span class="role">${esc(d.role)}</span></td>`+d.sprints.map((x,i)=>x?`<td class="cell" style="background:${col(x.efficiency)}" data-tip="<b>${esc(S[i].name)}</b><br>Delivered ${x.delivered} of ${x.capacitySP} SP capacity<br>Band ${x.low??'?'} to ${x.high??'?'}: ${x.vsLevel||'n/a'}<br>Leave ${x.leave} · on-call ${x.oncallDays} days · carried ${x.carried} SP">${x.delivered}${x.vsLevel==='below'?'':x.vsLevel==='above'?'▲':'•'}</td>`:'<td style="color:#cbd2dc">·</td>').join('')
      +`<td><b>${h.eff??'n/a'}%</b></td></tr>`;}).join('');
  document.getElementById('main').innerHTML=`<div class="panel"><h2>Delivered SP per sprint</h2>
    <p class="how">Cell colour is efficiency (delivered against capacity): darker blue is higher, amber and red are under half. • means within the level band, ▲ above it, no mark means below. The last column is mid-year efficiency.</p>
    <div style="overflow-x:auto"><table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div></div>`;
}
document.querySelectorAll('#view button').forEach(b=>b.onclick=()=>{document.querySelectorAll('#view button').forEach(x=>x.classList.toggle('on',x===b));V=b.dataset.v;D&&render();});
document.getElementById('sort').onchange=e=>{SORT=e.target.value;D&&render();};
document.getElementById('refresh').onclick=()=>load(true);
load(false);
"""


def page():
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sprint productivity · Synapse</title>
<style>@import url('https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');{CSS}</style>
</head><body><div class="wrap">{BODY}</div><script>{JS}</script></body></html>"""

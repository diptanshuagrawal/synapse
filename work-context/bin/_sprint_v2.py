#!/usr/bin/env python3
"""Generate sprint-planner-v2.html — a RESTRUCTURED workspace, not a scroll.

Built from sprint-planner.html (v1). All data/API logic in the core <script> is kept
verbatim; the only JS changes are presentational (planSignalsHTML → compact rows, and
plan-gantt work labels linkified to Jira for keys that exist in the loaded data). The body
is re-laid into a 4-tab workspace (Capacity · Initiatives · Backlog · Plan) with a sticky
step rail; the Plan tab carries an As-specified ⇄ Rebalance segmented toggle so the two
plan gantts no longer stack. Tabs/toggle/expand are wired by a small APPENDED script that
never touches the core logic. Served at /sprint-planner-v2.html, same origin + localStorage.

Re-run to regenerate. Local-only artifact.
"""
import re
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from derive.sources_config import team_title  # noqa: E402

DERIVED = ROOT / "derived"
SRC = DERIVED / "sprint-planner.html"
OUT = DERIVED / "sprint-planner-v2.html"

# v1's title/masthead carry the team display name; mirror it from config so this
# generator holds no hardcoded org identity (the .replace target must match v1 exactly).
TEAM = team_title()

NEW_CSS = """
  @import url('https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');
  :root{
    --bg:#f3f6fa; --surface:#ffffff; --surface2:#eef2f8; --border:#e4e9f2;
    --text:#15202e; --muted:#5b6675; --hint:#9aa6b4;
    --accent:#3a55d9; --accent-2:#2c41b8; --accent-soft:rgba(58,85,217,.07);
    --avail:#e9f1e8; --leave:#f6e9ec; --leave-t:#a23b4e; --wfh:#f6efdd; --wfh-t:#866113;
    --onc:#e7eefa; --onc-t:#2b57a8; --hol:#eceaf9; --hol-t:#4742ab; --we:#eef1f5;
    --info:#2b57a8; --danger:#b23a4a; --danger-bg:#f8edef; --warn:#866113; --ok:#3b6b3f; --sp-bg:#e4eede;
    --shadow:0 1px 2px rgba(20,32,46,.04),0 2px 6px rgba(20,32,46,.05);
    --radius:14px;
  }
  :root[data-theme="dark"]{
    --bg:#10151c; --surface:#171f2a; --surface2:#1e2835; --border:#2a3543;
    --text:#e6ecf3; --muted:#97a3b2; --hint:#697483;
    --accent:#7e8df2; --accent-2:#6f7eea; --accent-soft:rgba(126,141,242,.15);
    --avail:#1b2a17; --leave:#3a1e24; --leave-t:#f0a3b0; --wfh:#352a13; --wfh-t:#e8c074;
    --onc:#162840; --onc-t:#8fb4ea; --hol:#221f4a; --hol-t:#b3adf2; --we:#1e2835;
    --info:#8fb4ea; --danger:#f0a3b0; --danger-bg:#3a1e24; --warn:#e8c074; --ok:#9fce7e; --sp-bg:#23311a;
    --shadow:0 1px 2px rgba(0,0,0,.32);
  }
  @media (prefers-color-scheme: dark){
    :root[data-theme="auto"]{
      --bg:#10151c; --surface:#171f2a; --surface2:#1e2835; --border:#2a3543;
      --text:#e6ecf3; --muted:#97a3b2; --hint:#697483;
      --accent:#7e8df2; --accent-2:#6f7eea; --accent-soft:rgba(126,141,242,.15);
      --avail:#1b2a17; --leave:#3a1e24; --leave-t:#f0a3b0; --wfh:#352a13; --wfh-t:#e8c074;
      --onc:#162840; --onc-t:#8fb4ea; --hol:#221f4a; --hol-t:#b3adf2; --we:#1e2835;
      --info:#8fb4ea; --danger:#f0a3b0; --danger-bg:#3a1e24; --warn:#e8c074; --ok:#9fce7e; --sp-bg:#23311a;
      --shadow:0 1px 2px rgba(0,0,0,.32);
    }
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{margin:0;background:var(--bg);color:var(--text);
    font-family:'Hanken Grotesk',-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    font-size:14px;line-height:1.55;padding:0;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;}
  .wrap{max-width:1180px;margin:0 auto;padding:22px 26px 96px;}
  .topbar{display:flex;justify-content:space-between;align-items:center;gap:12px;}
  h1{font-size:22px;font-weight:700;letter-spacing:-.02em;margin:0;}
  h1 .tag{font:500 11px/1 'IBM Plex Mono',monospace;color:var(--accent);background:var(--accent-soft);
    padding:3px 7px;border-radius:6px;margin-left:9px;vertical-align:middle;letter-spacing:0;}
  .sub{color:var(--muted);font-size:12.5px;margin:8px 0;}
  #theme{font-size:12px;padding:7px 13px;white-space:nowrap;}
  /* step rail */
  .rail{position:sticky;top:0;z-index:30;display:flex;gap:4px;background:var(--bg);
    padding:12px 0 10px;margin-top:10px;border-bottom:1px solid var(--border);}
  .rail .tab{background:none;border:none;border-radius:9px;padding:8px 16px;font-weight:600;font-size:13.5px;
    color:var(--muted);position:relative;}
  .rail .tab:hover{background:var(--surface2);color:var(--text);}
  .rail .tab.on{color:var(--accent);}
  .rail .tab.on::after{content:"";position:absolute;left:14px;right:14px;bottom:-11px;height:2px;background:var(--accent);border-radius:2px;}
  .rail .tab .k{font:500 10px/1 'IBM Plex Mono',monospace;color:var(--hint);margin-right:7px;}
  .rail .tab.on .k{color:var(--accent);}
  /* panels */
  .panels{margin-top:18px;}
  .panel{display:none;}
  .panel.active{display:block;animation:fade .18s ease;}
  @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
  h2{display:flex;align-items:baseline;gap:10px;font-size:17px;font-weight:600;letter-spacing:-.01em;margin:4px 0 4px;}
  h2 span{font-weight:400;font-size:12px;color:var(--muted);letter-spacing:0;}
  /* cards */
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
    padding:18px 20px;overflow-x:auto;box-shadow:var(--shadow);}
  table{border-collapse:collapse;font-size:13px;}
  th{text-align:left;font-weight:500;color:var(--muted);font-size:10.5px;letter-spacing:.045em;text-transform:uppercase;
    padding:6px 8px;border-bottom:1px solid var(--border);white-space:nowrap;}
  td{padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:middle;}
  tbody tr:last-child td{border-bottom:none;}
  input,select{font:inherit;font-size:13px;color:var(--text);background:var(--surface);border:1px solid var(--border);
    border-radius:8px;padding:6px 9px;transition:border-color .12s,box-shadow .12s;}
  input.num{width:64px;}
  input:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);}
  input.errin{border-color:var(--danger);background:var(--danger-bg);}
  input::placeholder{color:var(--hint);}
  button{font:inherit;font-size:13px;font-weight:500;color:var(--text);background:var(--surface);
    border:1px solid var(--border);border-radius:9px;padding:8px 15px;cursor:pointer;
    transition:background .12s,border-color .12s,transform .04s;}
  button:hover{background:var(--surface2);border-color:var(--hint);}
  button:active{transform:translateY(.5px);}
  button:focus-visible{outline:none;box-shadow:0 0 0 3px var(--accent-soft);}
  button.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600;}
  button.primary:hover{background:var(--accent-2);border-color:var(--accent-2);}
  .del{color:var(--danger);border:none;background:none;padding:2px 7px;cursor:pointer;font-size:16px;border-radius:6px;}
  .del:hover{background:var(--danger-bg);}
  .toolbar{display:flex;gap:9px;flex-wrap:wrap;margin:12px 0;align-items:center;}
  .cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:14px 0 6px;max-width:800px;}
  .metric{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:13px 16px;box-shadow:var(--shadow);}
  .metric .l{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;}
  .metric .v{font:600 26px/1.1 'IBM Plex Mono',monospace;margin-top:6px;letter-spacing:-.02em;}
  /* gantt — the hero: a precise, calm capacity ribbon */
  .gantt-card{display:block;width:100%;background:var(--surface);border:1px solid var(--border);
    border-radius:var(--radius);padding:14px 16px;box-shadow:var(--shadow);margin-top:10px;}
  .g{width:100%;table-layout:fixed;border-collapse:collapse;}
  .g td,.g th{border:1px solid var(--border);text-align:center;padding:0;}
  .g .nm{text-align:left;padding:9px 13px;white-space:nowrap;font-size:13px;font-weight:500;width:22%;background:var(--surface);}
  .g .cell{height:38px;font-size:12px;font-weight:600;}
  .g .wk{font:500 11px/1.15 'Hanken Grotesk';color:var(--text);padding:0 7px;white-space:normal;}
  .g thead th{background:var(--surface2);}
  .g .dh{font:500 11px/1.2 'IBM Plex Mono',monospace;color:var(--text);padding:5px 2px;}
  .g .dh small{display:block;font-size:9px;color:var(--muted);font-weight:400;}
  .g .dh.hol{color:var(--hol-t);}
  .g .net{width:50px;font:600 12px/1 'IBM Plex Mono',monospace;background:var(--avail);min-width:38px;}
  .g .net.sp{background:var(--sp-bg);}
  .c-avail{background:var(--avail);} .c-L{background:var(--leave);color:var(--leave-t);}
  .c-W{background:var(--wfh);color:var(--wfh-t);} .c-O{background:var(--onc);color:var(--onc-t);}
  .c-H{background:var(--hol);color:var(--hol-t);} .c-WE{background:var(--we);}
  .g .wk a{color:inherit;text-decoration:none;border-bottom:1px dotted var(--hint);}
  .g .wk a:hover{color:var(--accent);border-bottom-color:var(--accent);}
  .legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:11px;font-size:11px;color:var(--muted);}
  .legend span{display:flex;align-items:center;gap:6px;}
  .sw{width:11px;height:11px;border-radius:3px;display:inline-block;border:1px solid rgba(20,32,46,.05);}
  /* plan bits */
  .chip{display:inline-block;font:500 11px/1.4 'IBM Plex Mono',monospace;padding:2px 8px;border-radius:7px;
    background:var(--surface2);color:var(--muted);margin:2px 3px 2px 0;}
  .chip.r{background:var(--onc);color:var(--onc-t);} .chip.cont{color:var(--muted);}
  .rationale{background:var(--accent-soft);border-radius:10px;padding:12px 15px;font-size:13px;line-height:1.55;
    margin:10px 0;border-left:3px solid var(--accent);}
  .pick{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 13px;margin-bottom:7px;
    font-size:13px;box-shadow:var(--shadow);}
  .pick a,.tview a{color:var(--info);text-decoration:none;font-weight:500;} .pick a:hover,.tview a:hover{text-decoration:underline;}
  .tlink{font-size:11px;}
  .tview{background:var(--surface);border:1px solid var(--accent);border-radius:10px;padding:12px 15px;margin:9px 0;
    font-size:13px;box-shadow:var(--shadow);}
  .empty{color:var(--hint);font-size:13px;padding:10px 2px;}
  code{background:var(--surface2);padding:1px 6px;border-radius:5px;font:500 12px 'IBM Plex Mono',monospace;}
  details summary{cursor:pointer;color:var(--muted);}
  /* segmented plan toggle */
  .seg{display:inline-flex;gap:3px;background:var(--surface2);border:1px solid var(--border);border-radius:11px;padding:3px;margin:8px 0 6px;}
  .seg button{background:none;border:none;border-radius:8px;padding:7px 16px;font-weight:600;font-size:13px;color:var(--muted);}
  .seg button.on{background:var(--surface);color:var(--text);box-shadow:var(--shadow);}
  /* compact signals */
  .sigbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:12px 0 10px;}
  .pill{font:600 11px/1 'IBM Plex Mono',monospace;padding:5px 10px;border-radius:20px;letter-spacing:.02em;}
  .pill.danger{background:var(--danger-bg);color:var(--danger);} .pill.warn{background:var(--wfh);color:var(--wfh-t);}
  .pill.ok{background:var(--avail);color:var(--ok);} .pill.info{background:var(--onc);color:var(--onc-t);}
  .sighint{font-size:11px;color:var(--hint);}
  .sig{display:flex;gap:11px;align-items:flex-start;padding:10px 13px;border:1px solid var(--border);border-radius:10px;
    margin-bottom:6px;background:var(--surface);cursor:pointer;transition:border-color .12s;box-shadow:var(--shadow);}
  .sig:hover{border-color:var(--hint);}
  .sig .dot{width:8px;height:8px;border-radius:50%;margin-top:6px;flex:none;}
  .dot.danger{background:var(--danger);} .dot.warn{background:var(--warn);} .dot.ok{background:var(--ok);} .dot.info{background:var(--info);}
  .sigtext{font-size:13px;line-height:1.5;color:var(--text);
    display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden;}
  .sig.open .sigtext{-webkit-line-clamp:unset;overflow:visible;}
  .sig.danger{border-left:3px solid var(--danger);} .sig.warn{border-left:3px solid var(--warn);}
  .sig.ok{border-left:3px solid var(--ok);} .sig.info{border-left:3px solid var(--info);}
  .manualwrap{margin-top:20px;border-top:1px dashed var(--border);padding-top:6px;}
  @media (max-width:640px){
    .wrap{padding:16px 14px 64px;} .cards{grid-template-columns:repeat(2,1fr);}
    .rail{overflow-x:auto;} .rail .tab{padding:8px 12px;}
  }
  @media (prefers-reduced-motion:reduce){ *{transition:none!important;scroll-behavior:auto!important;animation:none!important;} }
"""

# compact, severity-sorted, expandable signals — replaces v1's stack of full-width boxes
OLD_SIGFN = '''function planSignalsHTML(d){
  return (d.signals||[]).map(s=>{
    const lvl=["danger","warn","ok","info"].includes(s.level)?s.level:"info";
    return `<div class="callout ${lvl==="info"?"warn":lvl}">${s.text}</div>`;}).join("")||`<div class="callout ok">No risks flagged.</div>`;
}'''
NEW_SIGFN = '''function planSignalsHTML(d){
  const sig=(d.signals||[]);
  if(!sig.length)return `<div class="sigbar"><span class="pill ok">all clear</span></div>`;
  const rank={danger:0,warn:1,info:2,ok:3};
  const lab={danger:"risk",warn:"watch",info:"note",ok:"ok"};
  const cnt={}; sig.forEach(s=>cnt[s.level]=(cnt[s.level]||0)+1);
  const summary=["danger","warn","ok","info"].filter(l=>cnt[l]).map(l=>`<span class="pill ${l}">${cnt[l]} ${lab[l]}${cnt[l]>1&&l!=="ok"?"s":""}</span>`).join("");
  const rows=sig.slice().sort((a,b)=>(rank[a.level]??2)-(rank[b.level]??2)).map(s=>{
    const lvl=["danger","warn","ok","info"].includes(s.level)?s.level:"info";
    return `<div class="sig ${lvl}"><span class="dot ${lvl}"></span><div class="sigtext">${s.text}</div></div>`;}).join("");
  return `<div class="sigbar">${summary}<span class="sighint">click a line to expand</span></div>${rows}`;
}'''

# linkify ticket refs in plan-gantt work labels — only keys that exist in the loaded
# data (spillover / backlog pool / initiative epics / plan picks). Full keys like
# "ABC-123" link directly; bare 3-5 digit numbers link only when they resolve to a
# known key, so plain words/labels never turn into dead links. No org identity here:
# the key prefix comes from the data at runtime, the base URL from v1's JIRA const.
OLD_CELL = '''      html+=`<td colspan="${j-i}" class="cell ${cls}${isWork?' wk':''}">${txt}</td>`; i=j;'''
NEW_CELL = '''      html+=`<td colspan="${j-i}" class="cell ${cls}${isWork?' wk':''}">${isWork?linkifyTix(txt):txt}</td>`; i=j;'''
LINK_HELPER = '''function tixKnown(){
  const s=new Set();
  try{
    ((CAP&&CAP.people)||[]).forEach(p=>(p.spillover||[]).forEach(t=>{if(t.key)s.add(t.key);}));
    ((CAP&&CAP.backlogPool)||[]).forEach(t=>{if(t.key)s.add(t.key);});
    ((typeof S!=="undefined"&&S&&S.initiatives)||[]).forEach(i=>{const k=(i.epic||"").trim();if(k)s.add(k);});
    ((typeof LAST_PLAN!=="undefined"&&LAST_PLAN&&LAST_PLAN.backlogPicks)||[]).forEach(t=>{if(t.key)s.add(t.key);});
  }catch(_){}
  return s;
}
function linkifyTix(txt){
  if(!txt||txt.indexOf("<")>=0)return txt;
  const known=tixKnown();
  const bare={}; known.forEach(k=>{const m=k.match(/-(\\d+)$/); if(m)bare[m[1]]=k;});
  const A=(k,l)=>`<a href="${JIRA}${k}" target="_blank" rel="noopener">${l}</a>`;
  let out=txt.replace(/\\b[A-Z][A-Z0-9]+-\\d+\\b/g,m=>A(m,m));
  out=out.replace(/(^|[\\s(+\\u00b7])(\\d{3,5})(?=$|[\\s)+\\u00b7,])/g,(m,pre,num)=>bare[num]?pre+A(bare[num],num):m);
  return out;
}
'''

RAIL = '''<nav class="rail" id="rail">
    <button class="tab" data-step="cap"><span class="k">1</span>Capacity</button>
    <button class="tab" data-step="init"><span class="k">2</span>Initiatives</button>
    <button class="tab" data-step="backlog"><span class="k">3</span>Backlog</button>
    <button class="tab on" data-step="plan"><span class="k">4</span>Plan</button>
  </nav>
  <div class="panels">
  <section class="panel" data-step="cap">'''

SEG = '''<div class="seg" id="planseg">
      <button class="on" data-pv="pv-as">As-specified</button>
      <button data-pv="pv-rebalance">Rebalance</button>
    </div>'''

TAB_SCRIPT = '''
<script>
(function(){
  var rail=document.getElementById('rail');
  if(rail) rail.addEventListener('click',function(e){
    var b=e.target.closest('.tab'); if(!b) return;
    document.querySelectorAll('.rail .tab').forEach(t=>t.classList.toggle('on',t===b));
    document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active',p.dataset.step===b.dataset.step));
    window.scrollTo({top:0,behavior:'auto'});
  });
  var seg=document.getElementById('planseg');
  if(seg) seg.addEventListener('click',function(e){
    var b=e.target.closest('button[data-pv]'); if(!b) return;
    seg.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));
    document.querySelectorAll('.pv').forEach(v=>{ v.style.display = v.classList.contains(b.dataset.pv) ? '' : 'none'; });
  });
  document.addEventListener('click',function(e){ var s=e.target.closest('.sig'); if(s) s.classList.toggle('open'); });
})();
</script>
'''


def main():
    html = SRC.read_text()

    # 1) visual layer
    html = re.sub(r"<style>.*?</style>", "<style>" + NEW_CSS + "</style>", html, count=1, flags=re.S)
    html = html.replace(f"<title>Sprint Planner — {TEAM}</title>", "<title>Sprint Planner · v2</title>")

    # 2) compact signals (the only logic-adjacent change — presentational)
    assert OLD_SIGFN in html, "planSignalsHTML anchor not found — v1 changed"
    html = html.replace(OLD_SIGFN, NEW_SIGFN)

    # 2b) clickable tickets in plan-gantt work cells (presentational; real keys only)
    assert OLD_CELL in html, "plan-gantt cell anchor not found — v1 changed"
    html = html.replace(OLD_CELL, NEW_CELL)
    assert "function planGanttHTML(d){" in html
    html = html.replace("function planGanttHTML(d){", LINK_HELPER + "function planGanttHTML(d){", 1)

    # 3) masthead
    html = html.replace(
        '<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">\n'
        f'    <h1>Sprint planner — {TEAM}</h1>',
        '<header class="topbar">\n    <h1>Sprint planner<span class="tag">v2</span></h1>')
    html = html.replace(
        '<button id="theme" style="font-size:12px;padding:6px 12px;white-space:nowrap">◐ Auto</button>\n  </div>',
        '<button id="theme">◐ Auto</button>\n  </header>')

    # 4) workspace: rail + panels (split at the step h2 anchors — order matters)
    html = html.replace('<h2>1 · Capacity', RAIL + '<h2>1 · Capacity', 1)
    html = html.replace('<h2>2 · Initiatives', '</section>\n  <section class="panel" data-step="init"><h2>2 · Initiatives', 1)
    html = html.replace('<h2>3 · Backlog candidates', '</section>\n  <section class="panel" data-step="backlog"><h2>3 · Backlog candidates', 1)
    html = html.replace('<h2>4 · Plan', '</section>\n  <section class="panel active" data-step="plan">' + SEG + '<div class="pv pv-as"><h2>4 · Plan', 1)
    html = html.replace('<h2 id="brainHead"', '</div>\n  <div class="pv pv-rebalance" style="display:none"><h2 id="brainHead"', 1)
    html = html.replace('<h2>6 · Manual plan', '</div>\n  <details class="manualwrap"><summary class="sub">Manual plan — load an edited JSON to compare</summary><h2>6 · Manual plan', 1)
    html = html.replace('<div class="toolbar" style="margin-top:18px;">',
                        '</details></section>\n  </div><!--/panels-->\n  <div class="toolbar" style="margin-top:18px;">', 1)

    # 5) step-number chips inside h2s (the leading "N · ")
    html = re.sub(r'(<h2[^>]*>)(\d+)\s*·\s*', r'\1', html)  # rail carries the numbers; drop them from h2 titles

    # 6) tab/toggle/expand wiring (appended; never touches the core script)
    html = html.replace('</body>', TAB_SCRIPT + '</body>', 1)

    OUT.write_text(html)
    print(f"wrote {OUT}  ({len(html)} bytes)")


if __name__ == "__main__":
    main()

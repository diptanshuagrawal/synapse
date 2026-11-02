#!/usr/bin/env python3
"""Shared Synapse sidebar nav — one source of truth for every server.

The dashboard, planner, and pages servers all inject the SAME hamburger sidebar so
the whole ecosystem is one navigable app. Links are relative: under the desktop app
everything is one origin (the proxy routes each path to the right child), so they all
resolve. Add a page in ONE place here and it appears everywhere.
"""

# (href, active-key, label). Grouped for the sidebar.
GROUPS = [
    ("Overview", [
        ("/", "home", "\U0001F4CA Dashboard"),
        ("/people", "people", "\U0001F465 People"),
        ("/ask", "ask", "\U0001F50E Ask"),
    ]),
    ("Planning", [
        ("/sprint", "sprint", "\U0001F5D3️ Sprint"),
        ("/monthly", "monthly", "\U0001F4C6 Monthly"),
        ("/deps", "deps", "\U0001F517 Dependencies"),
        ("/plan", "plan", "\U0001F9ED Plan"),
        ("/sprint-board", "sprintboard", "\U0001F4CB Sprint board"),
        ("/sprint-check", "sprintcheck", "\U0001F50E Sprint check"),
        ("/jira-push", "jirapush", "\U0001F4E4 Push to Jira"),
        ("/retro", "retro", "\U0001F501 Retro"),
        ("/leaves", "leaves", "\U0001F334 Leaves"),
    ]),
    ("Delivery", [
        ("/releases", "releases", "\U0001F680 Releases"),
        ("/docs", "docs", "\U0001F4DD Doc drift"),
        ("/meetings", "meetings", "\U0001F5E3️ Meetings"),
    ]),
    ("Insight", [
        ("/topics", "topics", "\U0001F9E9 Topics"),
        ("/velocity", "velocity", "\U0001F4C8 Velocity"),
        ("/services", "services", "\U0001F9F1 Services"),
        ("/timeline", "timeline", "\U0001F9F5 Timeline"),
        ("/reviews", "reviews", "\U0001F4CB Reviews"),
    ]),
]

_CSS_JS = """
<style>
  body{padding-top:58px !important;}
  #cnavBtn{position:fixed;top:12px;left:14px;z-index:9998;width:38px;height:38px;border-radius:10px;
    border:1px solid #e4e9f2;background:#fff;color:#15202e;font-size:18px;cursor:pointer;
    box-shadow:0 1px 4px rgba(20,32,46,.16);display:flex;align-items:center;justify-content:center;}
  #cnavBtn:hover,.cnavAct:hover{border-color:#ff6a5b;}
  .cnavAct{position:fixed;top:12px;z-index:9998;width:38px;height:38px;border-radius:10px;border:1px solid #e4e9f2;
    background:#fff;color:#15202e;font-size:17px;cursor:pointer;box-shadow:0 1px 4px rgba(20,32,46,.16);
    display:flex;align-items:center;justify-content:center;}
  #cnavBack{left:58px;} #cnavReload{left:102px;} #cnavZout{left:150px;} #cnavZlvl{left:194px;width:56px;font-size:13px;font-weight:600;} #cnavZin{left:256px;}
  #cnavOv{position:fixed;inset:0;background:rgba(10,15,20,.3);z-index:9998;opacity:0;pointer-events:none;transition:opacity .15s;}
  #cnav{position:fixed;top:0;left:0;height:100%;width:250px;background:#fff;z-index:9999;overflow-y:auto;
    box-shadow:2px 0 16px rgba(20,32,46,.14);transform:translateX(-100%);transition:transform .18s ease;
    font-family:'Hanken Grotesk',-apple-system,system-ui,sans-serif;padding:18px 14px;}
  body.cnav-open #cnav{transform:none;} body.cnav-open #cnavOv{opacity:1;pointer-events:auto;}
  #cnav .brand{display:flex;align-items:center;gap:9px;font-size:15px;font-weight:700;color:#15202e;margin:2px 8px 16px;}
  #cnav .brand .sp{width:20px;height:20px;border-radius:6px;background:linear-gradient(160deg,#ffb14a,#ff6a5b);display:inline-block;}
  #cnav .t{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#8a94a6;font-weight:700;margin:14px 8px 6px;}
  #cnav a{display:block;text-decoration:none;color:#15202e;font-size:14px;font-weight:600;
    padding:9px 12px;border-radius:10px;margin-bottom:2px;}
  #cnav a:hover{background:#f4f2ee;} #cnav a.active{background:rgba(255,106,91,.10);color:#c2452f;}
</style>
<button id="cnavBtn" title="Menu">&#9776;</button>
<button id="cnavBack" class="cnavAct" title="Back (Cmd+[)">&#8592;</button>
<button id="cnavReload" class="cnavAct" title="Refresh (Cmd+R)">&#8635;</button>
<button id="cnavZout" class="cnavAct" title="Zoom out (Cmd+-)">&#8722;</button>
<button id="cnavZlvl" class="cnavAct" title="Reset zoom (Cmd+0)">100%</button>
<button id="cnavZin" class="cnavAct" title="Zoom in (Cmd+=)">+</button>
<div id="cnavOv"></div>
<nav id="cnav">__NAV__</nav>
<script>(function(){
  var b=document.getElementById('cnavBtn'),o=document.getElementById('cnavOv');
  function close(){document.body.classList.remove('cnav-open');}
  b.addEventListener('click',function(){document.body.classList.toggle('cnav-open');});
  o.addEventListener('click',close);
  var Z=[0.67,0.75,0.8,0.9,1,1.1,1.25,1.4,1.5,1.75,2],zl=document.getElementById('cnavZlvl');
  function getZ(){try{return parseFloat(localStorage.getItem('synapse-zoom'))||1;}catch(e){return 1;}}
  function setZ(z){z=Math.min(2,Math.max(0.67,z));document.documentElement.style.zoom=z;zl.textContent=Math.round(z*100)+'%';
    try{localStorage.setItem('synapse-zoom',String(z));}catch(e){}}
  function step(d){var z=getZ(),i=Z.findIndex(function(x){return x>=z-0.001;});if(i<0)i=Z.length-1;
    if(d>0&&Z[i]<=z+0.001)i++; else if(d<0)i--; setZ(Z[Math.min(Z.length-1,Math.max(0,i))]);}
  setZ(getZ());
  document.getElementById('cnavZin').addEventListener('click',function(){step(1);});
  document.getElementById('cnavZout').addEventListener('click',function(){step(-1);});
  zl.addEventListener('click',function(){setZ(1);});
  document.getElementById('cnavBack').addEventListener('click',function(){history.back();});
  document.getElementById('cnavReload').addEventListener('click',function(){location.reload();});
  document.addEventListener('keydown',function(e){if(e.key==='Escape')close();
    var m=e.metaKey||e.ctrlKey;
    if((m&&(e.key==='r'||e.key==='R'))||e.key==='F5'){e.preventDefault();location.reload();}
    else if((m&&e.key==='[')||(e.altKey&&e.key==='ArrowLeft')){e.preventDefault();history.back();}
    else if(m&&(e.key==='='||e.key==='+')){e.preventDefault();step(1);}
    else if(m&&(e.key==='-'||e.key==='_')){e.preventDefault();step(-1);}
    else if(m&&e.key==='0'){e.preventDefault();setZ(1);}
    else if((m&&e.key===']')||(e.altKey&&e.key==='ArrowRight')){e.preventDefault();history.forward();}});
})();</script>
"""


_FIND = """
<style>
  #sfind{position:fixed;top:12px;right:16px;z-index:10000;display:none;align-items:center;gap:6px;
    background:#fff;border:1px solid #e4e9f2;border-radius:10px;padding:5px 7px;
    box-shadow:0 2px 10px rgba(20,32,46,.18);
    font-family:'Hanken Grotesk',-apple-system,system-ui,sans-serif;}
  body.sfind-on #sfind{display:flex;}
  #sfindIn{border:0;outline:0;background:none;color:#15202e;font-size:13px;width:190px;padding:3px 2px;}
  #sfindN{font:600 11px/1 'IBM Plex Mono',monospace;color:#8a94a6;min-width:52px;text-align:right;}
  #sfind button{border:1px solid transparent;background:none;color:#5b6675;cursor:pointer;
    border-radius:7px;width:24px;height:24px;font-size:13px;line-height:1;}
  #sfind button:hover{background:#f4f2ee;color:#15202e;}
  #sfind button.on{background:rgba(255,106,91,.12);color:#c2452f;border-color:rgba(255,106,91,.3);}
  #sfindN.none{color:#b23a4a;}
  ::highlight(sfind){background:#ffe8a3;color:#15202e;}
  ::highlight(sfindcur){background:#ff9f45;color:#15202e;}
</style>
<div id="sfind">
  <input id="sfindIn" placeholder="Find on page" autocomplete="off" spellcheck="false">
  <span id="sfindN"></span>
  <button id="sfindAa" title="Match case">Aa</button>
  <button id="sfindP" title="Previous (Shift+Enter)">&#8593;</button>
  <button id="sfindNx" title="Next (Enter)">&#8595;</button>
  <button id="sfindX" title="Close (Esc)">&#10005;</button>
</div>
<script>(function(){
  // Find on page, for every Synapse page at once. It never touches the DOM: matches are
  // drawn with the CSS Custom Highlight API, because these pages carry drag handlers and
  // re-render themselves, and wrapping matches in <mark> would break both.
  var BAR='sfind', bar=document.getElementById(BAR), box=document.getElementById('sfindIn'),
      num=document.getElementById('sfindN');
  var SKIP='#sfind, #cnav, #cnavOv, script, style, noscript, template';
  var idx=null, hits=[], at=0, q='', cased=false, timer=null;

  // Flatten the page into one string, remembering where every character came from, so a
  // phrase that runs across elements ("TEAM-1" + " rollback") still matches - which is the
  // whole difference between this and searching each text node on its own.
  // Hidden text must not match, but offsetParent is null for anything position:fixed, so
  // asking that question would skip half the chrome on the page. Ask the style instead,
  // once per element.
  function visible(el, seen){
    if(seen.has(el)) return seen.get(el);
    var ok=true;
    if(el.hidden) ok=false;
    else {
      var cs=window.getComputedStyle ? getComputedStyle(el) : null;
      if(cs && (cs.display==='none' || cs.visibility==='hidden')) ok=false;
      else if(el.parentElement) ok=visible(el.parentElement, seen);
    }
    seen.set(el, ok);
    return ok;
  }

  function buildIndex(root){
    var w=document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null), text='', map=[], sp=true, n;
    var seen=new Map();
    while((n=w.nextNode())){
      var p=n.parentElement;
      if(!p || p.closest(SKIP)) continue;
      if(!visible(p, seen)) continue;
      var v=n.nodeValue||'';
      for(var i=0;i<v.length;i++){
        var ch=v[i];
        if(ch===' '||ch==='\\n'||ch==='\\t'||ch==='\\r'){
          if(sp) continue;
          text+=' '; map.push([n,i]); sp=true;
        } else { text+=ch; map.push([n,i]); sp=false; }
      }
    }
    return {text:text, map:map};
  }

  // Pure: where the needle sits in the flattened text. Overlapping matches are not a thing
  // a reader wants to step through, so each hit starts after the last one ends.
  function scanText(text, needle, caseSensitive){
    var out=[];
    if(!needle) return out;
    var h=caseSensitive?text:text.toLowerCase(), q=caseSensitive?needle:needle.toLowerCase();
    var i=h.indexOf(q);
    while(i>=0){ out.push([i,i+q.length]); i=h.indexOf(q,i+q.length); }
    return out;
  }

  function rangeFor(hit){
    var a=idx.map[hit[0]], b=idx.map[hit[1]-1];
    if(!a||!b) return null;
    var r=document.createRange();
    try{ r.setStart(a[0],a[1]); r.setEnd(b[0],b[1]+1); }catch(e){ return null; }
    return r;
  }

  function paint(){
    if(!window.CSS||!CSS.highlights) return;
    CSS.highlights.delete('sfind'); CSS.highlights.delete('sfindcur');
    if(!hits.length) return;
    var all=[], cur=null;
    for(var i=0;i<hits.length;i++){
      var r=rangeFor(hits[i]);
      if(!r) continue;
      all.push(r);
      if(i===at) cur=r;
    }
    if(all.length) CSS.highlights.set('sfind', new Highlight(...all));
    if(cur) CSS.highlights.set('sfindcur', new Highlight(cur));
  }

  function show(){
    num.textContent = q ? (hits.length ? (at+1)+'/'+hits.length : '0/0') : '';
    num.className = (q && !hits.length) ? 'none' : '';
  }

  function run(keepAt){
    idx=buildIndex(document.body);
    hits=scanText(idx.text, q, cased);
    at = hits.length ? Math.min(keepAt?at:0, hits.length-1) : 0;
    paint(); show(); scrollToHit();
  }

  function scrollToHit(){
    if(!hits.length) return;
    var a=idx.map[hits[at][0]];
    var el=a && a[0] ? a[0].parentElement : null;
    if(el && el.scrollIntoView) el.scrollIntoView({block:'center', inline:'nearest'});
  }

  function step(d){
    if(!hits.length) return;
    at=(at+d+hits.length)%hits.length;
    paint(); show(); scrollToHit();
  }

  function open(){
    document.body.classList.add('sfind-on');
    box.focus(); box.select();
    if(q) run(true);
  }
  function close(){
    document.body.classList.remove('sfind-on');
    if(window.CSS&&CSS.highlights){ CSS.highlights.delete('sfind'); CSS.highlights.delete('sfindcur'); }
    hits=[]; show();
  }

  box.addEventListener('input', function(){ q=box.value; clearTimeout(timer);
    timer=setTimeout(function(){ run(false); }, 90); });
  box.addEventListener('keydown', function(e){
    if(e.key==='Enter'){ e.preventDefault(); step(e.shiftKey?-1:1); }
    else if(e.key==='Escape'){ e.preventDefault(); close(); }
  });
  document.getElementById('sfindNx').addEventListener('click', function(){ step(1); });
  document.getElementById('sfindP').addEventListener('click', function(){ step(-1); });
  document.getElementById('sfindX').addEventListener('click', close);
  document.getElementById('sfindAa').addEventListener('click', function(){
    cased=!cased; this.classList.toggle('on', cased); run(true); box.focus(); });

  // Capture, so a page that handles its own keys cannot swallow it first.
  document.addEventListener('keydown', function(e){
    var m=e.metaKey||e.ctrlKey;
    if(m && (e.key==='f'||e.key==='F')){ e.preventDefault(); e.stopPropagation(); open(); }
    else if(m && (e.key==='g'||e.key==='G') && document.body.classList.contains('sfind-on')){
      e.preventDefault(); step(e.shiftKey?-1:1); }
    else if(e.key==='Escape' && document.body.classList.contains('sfind-on')){ close(); }
  }, true);

  // These pages redraw themselves - a sprint board repaints on every drag. Matches are
  // ranges into nodes that no longer exist after that, so rebuild rather than leave stale
  // highlights pointing at nothing.
  if(window.MutationObserver){
    var mo=new MutationObserver(function(recs){
      if(!document.body.classList.contains('sfind-on') || !q) return;
      // The bar writes its own match count into the page, which the observer sees, which
      // would schedule another run, forever. Ignore anything we did to ourselves.
      for(var i=0;i<recs.length;i++){
        var t=recs[i].target;
        var el=t.nodeType===1 ? t : t.parentElement;
        if(el && el.closest && el.closest('#'+BAR)) continue;
        clearTimeout(timer); timer=setTimeout(function(){ run(true); }, 180);
        return;
      }
    });
    mo.observe(document.body, {childList:true, subtree:true, characterData:true});
  }

  window.synapseFind = {buildIndex:buildIndex, scanText:scanText, open:open, close:close,
                        step:step, run:run,
                        state:function(){ return {q:q, at:at, hits:hits.length, cased:cased}; },
                        set:function(v){ box.value=v; q=v; run(false); }};
})();</script>
"""

def build_nav(active: str) -> str:
    parts = ['<div class="brand"><span class="sp"></span>Synapse</div>']
    for title, links in GROUPS:
        parts.append(f'<div class="t">{title}</div>')
        for href, key, label in links:
            cls = ' class="active"' if key == active else ""
            parts.append(f'<a href="{href}"{cls}>{label}</a>')
    return _CSS_JS.replace("__NAV__", "\n".join(parts)) + _FIND


def active_from_path(path: str) -> str:
    p = (path or "").split("?")[0]
    table = [
        ("people", "people"), ("/ask", "ask"), ("sprint", "sprint"),
        ("monthly", "monthly"), ("deps", "deps"), ("sprint-board", "sprintboard"), ("sprint-check", "sprintcheck"),
        ("retro", "retro"), ("pr-friction", "pr"),
        ("releases", "releases"), ("docs", "docs"), ("meetings", "meetings"),
        ("leaves", "leaves"), ("plan", "plan"), ("topics", "topics"),
        ("velocity", "velocity"), ("services", "services"), ("timeline", "timeline"),
        ("/jira-push", "jirapush"),
        ("/reviews", "reviews"),
    ]
    for needle, key in table:
        if needle in p:
            return key
    if p in ("/", "/index.html", "/v1", "/v2", "/v3", "/v4", "/v5", "/channels"):
        return "home"
    return ""


def inject_html(html: str, active: str) -> str:
    nav = build_nav(active)
    return html.replace("</body>", nav + "</body>", 1) if "</body>" in html else html + nav


def inject_bytes(html_bytes: bytes, active: str) -> bytes:
    nav = build_nav(active).encode()
    if b"</body>" in html_bytes:
        return html_bytes.replace(b"</body>", nav + b"</body>", 1)
    return html_bytes + nav

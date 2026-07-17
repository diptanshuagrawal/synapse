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
        ("/plan", "plan", "\U0001F9ED Plan"),
        ("/retro", "retro", "\U0001F501 Retro"),
        ("/leaves", "leaves", "\U0001F334 Leaves"),
    ]),
    ("Delivery", [
        ("/pr-friction", "pr", "\U0001F500 PR friction"),
        ("/releases", "releases", "\U0001F680 Releases"),
        ("/docs", "docs", "\U0001F4DD Doc drift"),
        ("/meetings", "meetings", "\U0001F5E3️ Meetings"),
    ]),
    ("Insight", [
        ("/topics", "topics", "\U0001F9E9 Topics"),
        ("/velocity", "velocity", "\U0001F4C8 Velocity"),
        ("/services", "services", "\U0001F9F1 Services"),
        ("/timeline", "timeline", "\U0001F9F5 Timeline"),
    ]),
]

_CSS_JS = """
<style>
  body{padding-top:58px !important;}
  #cnavBtn{position:fixed;top:12px;left:14px;z-index:9998;width:38px;height:38px;border-radius:10px;
    border:1px solid #e4e9f2;background:#fff;color:#15202e;font-size:18px;cursor:pointer;
    box-shadow:0 1px 4px rgba(20,32,46,.16);display:flex;align-items:center;justify-content:center;}
  #cnavBtn:hover{border-color:#ff6a5b;}
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
<div id="cnavOv"></div>
<nav id="cnav">__NAV__</nav>
<script>(function(){
  var b=document.getElementById('cnavBtn'),o=document.getElementById('cnavOv');
  function close(){document.body.classList.remove('cnav-open');}
  b.addEventListener('click',function(){document.body.classList.toggle('cnav-open');});
  o.addEventListener('click',close);
  document.addEventListener('keydown',function(e){if(e.key==='Escape')close();});
})();</script>
"""


def build_nav(active: str) -> str:
    parts = ['<div class="brand"><span class="sp"></span>Synapse</div>']
    for title, links in GROUPS:
        parts.append(f'<div class="t">{title}</div>')
        for href, key, label in links:
            cls = ' class="active"' if key == active else ""
            parts.append(f'<a href="{href}"{cls}>{label}</a>')
    return _CSS_JS.replace("__NAV__", "\n".join(parts))


def active_from_path(path: str) -> str:
    p = (path or "").split("?")[0]
    table = [
        ("people", "people"), ("/ask", "ask"), ("sprint", "sprint"),
        ("monthly", "monthly"), ("retro", "retro"), ("pr-friction", "pr"),
        ("releases", "releases"), ("docs", "docs"), ("meetings", "meetings"),
        ("leaves", "leaves"), ("plan", "plan"), ("topics", "topics"),
        ("velocity", "velocity"), ("services", "services"), ("timeline", "timeline"),
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

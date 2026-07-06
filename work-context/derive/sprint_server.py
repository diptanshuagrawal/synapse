#!/usr/bin/env python3
"""Tiny local server for the sprint planner.

  GET /                      -> sprint-planner.html
  GET /sprint-planner.html   -> the UI
  GET /api/capacity          -> live capacity model (runs capacity_engine.build())

Static files are served from derived/. Run:
  OPSGENIE_API_KEY=... python3 derive/sprint_server.py [port]
"""
import os, sys, json
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DERIVED = os.path.join(ROOT, "derived")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "derive"))
import capacity_engine
import plan_brain

# Leaves lives on the cron dashboard (separate server/port); link out to it.
LEAVES_URL = "http://127.0.0.1:8765/leaves"


def _inject_nav(html_bytes, clean_path):
    """Inject a hamburger slide-out sidebar (Sprint / Monthly / Leaves) into a page."""
    active = ("sprint" if "sprint-planner" in clean_path
              else "monthly" if "monthly" in clean_path else "")
    def a(href, key, label):
        cls = ' class="active"' if key == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'
    nav = f"""
<style>
  body{{padding-top:58px !important;}}   /* reserve a strip so the menu button never overlaps content */
  #cnavBtn{{position:fixed;top:12px;left:14px;z-index:9998;width:38px;height:38px;border-radius:10px;
    border:1px solid #e4e9f2;background:#fff;color:#15202e;font-size:18px;cursor:pointer;
    box-shadow:0 1px 4px rgba(20,32,46,.12);display:flex;align-items:center;justify-content:center;}}
  #cnavBtn:hover{{border-color:#3a55d9;}}
  #cnavOv{{position:fixed;inset:0;background:rgba(10,15,20,.28);z-index:9998;opacity:0;pointer-events:none;transition:opacity .15s;}}
  #cnav{{position:fixed;top:0;left:0;height:100%;width:250px;background:#fff;z-index:9999;
    box-shadow:2px 0 16px rgba(20,32,46,.14);transform:translateX(-100%);transition:transform .18s ease;
    font-family:'Hanken Grotesk',-apple-system,system-ui,sans-serif;padding:18px 14px;}}
  body.cnav-open #cnav{{transform:none;}} body.cnav-open #cnavOv{{opacity:1;pointer-events:auto;}}
  #cnav .t{{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#5b6675;font-weight:700;margin:6px 8px 12px;}}
  #cnav a{{display:block;text-decoration:none;color:#15202e;font-size:14px;font-weight:600;
    padding:10px 12px;border-radius:10px;margin-bottom:4px;}}
  #cnav a:hover{{background:#eef2f8;}} #cnav a.active{{background:rgba(58,85,217,.08);color:#2c41b8;}}
</style>
<button id="cnavBtn" title="Menu">&#9776;</button>
<div id="cnavOv"></div>
<nav id="cnav">
  <div class="t">Planning</div>
  {a('/sprint','sprint','🗓️ Sprint Planner')}
  {a('/monthly','monthly','📆 Monthly Planner')}
  {a(LEAVES_URL,'leaves','🌴 Team Leaves')}
</nav>
<script>(function(){{
  var b=document.getElementById('cnavBtn'),o=document.getElementById('cnavOv');
  function close(){{document.body.classList.remove('cnav-open');}}
  b.addEventListener('click',function(){{document.body.classList.toggle('cnav-open');}});
  o.addEventListener('click',close);
  document.addEventListener('keydown',function(e){{if(e.key==='Escape')close();}});
}})();</script>
</body>"""
    if b"</body>" in html_bytes:
        return html_bytes.replace(b"</body>", nav.encode(), 1)
    return html_bytes + nav.encode()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DERIVED, **k)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/epic_sp":
            from urllib.parse import urlparse, parse_qs
            keys = parse_qs(urlparse(self.path).query).get("keys", [""])[0].split(",")
            try:
                body = json.dumps(capacity_engine.epic_remaining_sp(keys)).encode()
                self.send_response(200)
            except Exception as e:
                body = json.dumps({"__error__": str(e)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?")[0] == "/api/ticket":
            from urllib.parse import urlparse, parse_qs
            key = parse_qs(urlparse(self.path).query).get("key", [""])[0]
            body = json.dumps(capacity_engine.get_ticket(key)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?")[0] == "/api/capacity":
            from urllib.parse import urlparse, parse_qs
            fresh = parse_qs(urlparse(self.path).query).get("fresh", ["0"])[0] == "1"
            cachef = os.path.join(DERIVED, "capacity.json")
            try:
                if not fresh and os.path.exists(cachef):
                    with open(cachef, "rb") as f:
                        body = f.read()
                else:
                    model = capacity_engine.build()
                    body = json.dumps(model).encode()
                    with open(cachef, "wb") as f:
                        f.write(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path.split("?")[0] == "/api/monthly":
            from urllib.parse import urlparse, parse_qs
            fresh = parse_qs(urlparse(self.path).query).get("fresh", ["0"])[0] == "1"
            cachef = os.path.join(DERIVED, "monthly.json")
            try:
                if not fresh and os.path.exists(cachef):
                    with open(cachef, "rb") as f:
                        body = f.read()
                else:
                    model = capacity_engine.build_monthly()
                    body = json.dumps(model).encode()
                    with open(cachef, "wb") as f:
                        f.write(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path.split("?")[0] == "/api/month":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            ym = q.get("ym", [""])[0]
            fresh = q.get("fresh", ["0"])[0] == "1"
            try:
                y, m = (int(x) for x in ym.split("-"))
                if not (1 <= m <= 12):
                    raise ValueError("month out of range")
                cachef = os.path.join(DERIVED, f"month-{y:04d}-{m:02d}.json")
                if not fresh and os.path.exists(cachef):
                    with open(cachef, "rb") as f:
                        body = f.read()
                else:
                    body = json.dumps(capacity_engine.month_capacity(y, m)).encode()
                    with open(cachef, "wb") as f:
                        f.write(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(400 if "ym" not in q else 500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"bad ?ym= (want YYYY-MM): {e}"}).encode())
            return
        if self.path.split("?")[0] == "/api/budgets":
            from urllib.parse import urlparse, parse_qs
            fresh = parse_qs(urlparse(self.path).query).get("fresh", ["0"])[0] == "1"
            cachef = os.path.join(DERIVED, "budgets.json")
            try:
                if not fresh and os.path.exists(cachef):
                    with open(cachef, "rb") as f:
                        body = f.read()
                else:
                    body = json.dumps(capacity_engine.epic_budgets()).encode()
                    with open(cachef, "wb") as f:
                        f.write(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path.split("?")[0] == "/api/initiative":
            from urllib.parse import urlparse, parse_qs
            key = parse_qs(urlparse(self.path).query).get("key", [""])[0]
            try:
                body = json.dumps(capacity_engine.initiative_detail(key)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path.split("?")[0] == "/api/pods":
            from urllib.parse import urlparse, parse_qs
            fresh = parse_qs(urlparse(self.path).query).get("fresh", ["0"])[0] == "1"
            cachef = os.path.join(DERIVED, "pods.json")
            try:
                if not fresh and os.path.exists(cachef):
                    with open(cachef, "rb") as f:
                        body = f.read()
                else:
                    body = json.dumps({"pods": capacity_engine.pod_options()}).encode()
                    with open(cachef, "wb") as f:
                        f.write(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path.split("?")[0] == "/api/initiatives":
            import hashlib
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            fresh = q.get("fresh", ["0"])[0] == "1"
            pods = [p for p in q.get("pods", [""])[0].split(",") if p] or None
            tag = "default" if not pods else hashlib.md5(",".join(sorted(pods)).encode()).hexdigest()[:8]
            cachef = os.path.join(DERIVED, f"initiatives-{tag}.json")
            try:
                if not fresh and os.path.exists(cachef):
                    with open(cachef, "rb") as f:
                        body = f.read()
                else:
                    body = json.dumps(capacity_engine.pod_initiatives(pods)).encode()
                    with open(cachef, "wb") as f:
                        f.write(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        if self.path in ("/", ""):
            self.path = "/sprint-planner-v2.html"      # entry = sprint planner; nav via sidebar
        elif self.path.split("?")[0] in ("/sprint", "/sprint/"):
            self.path = "/sprint-planner-v2.html"      # 4-tab workspace (bin/_sprint_v2.py)
        elif self.path.split("?")[0] in ("/monthly", "/monthly/"):
            self.path = "/monthly.html"                # per-month capacity + planned budget view
        # Serve our HTML pages with the nav sidebar injected and NO caching (so edits/JS
        # fixes always take effect on reload — stale-cache was causing "stuck" pages).
        clean = self.path.split("?")[0]
        if clean.endswith(".html"):
            fp = os.path.join(DERIVED, clean.lstrip("/"))
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    html = _inject_nav(f.read(), clean)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)
                return
        return super().do_GET()

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.split("?")[0] == "/api/link-epic":
            # body: {initiative, mode: preview|auto|link|create, epicKey?}
            try:
                p = self._json_body()
                r = capacity_engine.resolve_epic(p.get("initiative", ""),
                                                 mode=p.get("mode", "preview"),
                                                 epic_key=p.get("epicKey"))
                self._reply(500 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return
        if self.path.split("?")[0] == "/api/submit-budgets":
            # body: {epicBudgets:{key:{Month:sp}}, months:[...], dryRun:bool}
            try:
                p = self._json_body()
                r = capacity_engine.submit_budgets(p.get("epicBudgets", {}),
                                                   p.get("months", []),
                                                   dry_run=bool(p.get("dryRun", True)))
                self._reply(500 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return
        if self.path.split("?")[0] == "/api/save_initiatives":
            try:
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n) or b"{}"
                json.loads(raw)
                with open(os.path.join(DERIVED, "initiatives-in.json"), "wb") as f:
                    f.write(raw)
                body = json.dumps({"ok": True, "path": "work-context/derived/initiatives-in.json"}).encode()
                self.send_response(200)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?")[0] == "/api/dump":
            try:
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n) or b"{}"
                json.loads(raw)  # validate
                with open(os.path.join(DERIVED, "sprint-dump.json"), "wb") as f:
                    f.write(raw)
                body = json.dumps({"ok": True, "path": "work-context/derived/sprint-dump.json"}).encode()
                self.send_response(200)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?")[0] == "/api/plan":
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                result = plan_brain.analyze(payload)
                body = json.dumps(result).encode()
                self.send_response(200)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?")[0] == "/api/accept":
            # Snapshot the accepted plan so `/sprint-apply` can execute it in-session,
            # even after the plan files are regenerated. Body = {_accepted, source,
            # label, sprint, plan}.
            try:
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n) or b"{}"
                json.loads(raw)  # validate
                with open(os.path.join(DERIVED, "sprint-plan-accepted.json"), "wb") as f:
                    f.write(raw)
                body = json.dumps({"ok": True, "path": "work-context/derived/sprint-plan-accepted.json"}).encode()
                self.send_response(200)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    print(f"sprint planner: http://127.0.0.1:{port}/")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()

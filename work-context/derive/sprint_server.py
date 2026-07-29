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

# Shared nav sidebar — one source of truth for the whole ecosystem.
import synapse_nav


def _inject_nav(html_bytes, clean_path):
    return synapse_nav.inject_bytes(html_bytes, synapse_nav.active_from_path(clean_path))


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
            q = parse_qs(urlparse(self.path).query)
            fresh = q.get("fresh", ["0"])[0] == "1"
            start_raw = q.get("start", [""])[0]
            cachef = os.path.join(DERIVED, "capacity.json")
            try:
                start_override = None
                if start_raw:
                    import datetime as _dt
                    start_override = _dt.date.fromisoformat(start_raw)
                # a custom start always computes live — the cached model is a
                # different window; the result still lands in the cache so a
                # reload keeps showing the chosen window
                if not (fresh or start_override) and os.path.exists(cachef):
                    with open(cachef, "rb") as f:
                        body = f.read()
                else:
                    model = capacity_engine.build(start_override=start_override)
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
        if self.path.split("?")[0] == "/api/retro-notes":
            from urllib.parse import urlparse, parse_qs
            months = [m for m in parse_qs(urlparse(self.path).query).get("months", [""])[0].split(",") if m]
            try:
                body = json.dumps(capacity_engine.retro_notes(months)).encode()
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
        if self.path.split("?")[0] == "/api/retro":
            import hashlib
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            months = [m for m in q.get("months", [""])[0].split(",") if m]
            fresh = q.get("fresh", ["0"])[0] == "1"
            tag = hashlib.md5(",".join(sorted(months)).encode()).hexdigest()[:8] if months else "none"
            cachef = os.path.join(DERIVED, f"retro-{tag}.json")
            try:
                if not fresh and os.path.exists(cachef):
                    with open(cachef, "rb") as f:
                        body = f.read()
                else:
                    body = json.dumps(capacity_engine.retro_summary(months)).encode()
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
        elif self.path.split("?")[0] in ("/plan", "/plan/"):
            self.path = "/plan.html"                   # roadmap sandbox (scratch; doesn't touch budgets)
        elif self.path.split("?")[0] in ("/retro", "/retro/"):
            self.path = "/retro.html"                  # retro: highs/lows + planned-vs-actual SP by epic
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
        if self.path.split("?")[0] == "/api/plan-dump":
            # Persist the /plan roadmap sandbox's dump for the /monthly-plan chat skill.
            try:
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n) or b"{}"
                json.loads(raw)  # validate
                with open(os.path.join(DERIVED, "plan-dump.json"), "wb") as f:
                    f.write(raw)
                body = json.dumps({"ok": True, "path": "work-context/derived/plan-dump.json"}).encode()
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

#!/usr/bin/env python3
"""Tiny local server for the sprint planner.

  GET /                      -> sprint-planner.html
  GET /sprint-planner.html   -> the UI
  GET /api/capacity          -> live capacity model (runs capacity_engine.build())

Static files are served from derived/. Run:
  OPSGENIE_API_KEY=... python3 derive/sprint_server.py [port]
"""
import os, sys, json
from http.server import HTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DERIVED = os.path.join(ROOT, "derived")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "derive"))
import capacity_engine
import plan_brain


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
        if self.path in ("/", ""):
            self.path = "/sprint-planner.html"
        return super().do_GET()

    def do_POST(self):
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
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    print(f"sprint planner: http://localhost:{port}/sprint-planner.html")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()

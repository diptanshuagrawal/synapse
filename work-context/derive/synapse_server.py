#!/usr/bin/env python3
"""Synapse — one origin for the whole suite.

Starts the cron dashboard and the planner server on internal-only ports, then serves
a single public port that reverse-proxies to both by path. This is what the Synapse
desktop app (bin/synapse-app) points its window at, and it also works in a plain
browser:

    .venv/bin/python derive/synapse_server.py            # http://127.0.0.1:8765
    .venv/bin/python derive/synapse_server.py --port 8790

Routing: the dashboard owns a small, fixed set of routes (home / v1-v5 / channels /
leaves / its /api/*). Those go to the dashboard; EVERYTHING else (the planner pages,
planner /api/*, and all of derived/ static) goes to the planner. Both children get
SYNAPSE_*_BASE="" so their nav links render same-origin.
"""
import os, sys, signal, subprocess, time, urllib.request, urllib.error, argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "bin", "dashboard.py")
SPRINT = os.path.join(ROOT, "derive", "sprint_server.py")
PAGES = os.path.join(ROOT, "derive", "synapse_pages.py")

# Paths the DASHBOARD serves (fixed set). Anything not here → planner.
DASH_ROUTES = {
    "/", "/index.html", "/v1", "/v2", "/v3", "/v4", "/v5", "/channels",
    "/leaves", "/leaves-v1", "/leaves-v2",
    "/api/cadence", "/api/insights", "/api/leaves", "/api/holidays",
    "/api/snapshot", "/api/slack-channels", "/api/identity-timeseries",
    "/api/discover", "/api/clusters", "/api/logs", "/api/log-tail",
}
# Path prefixes the ECOSYSTEM PAGES server owns. Checked before falling to planner.
PAGES_PREFIXES = ("/people", "/pr-friction", "/releases", "/docs", "/meetings", "/ask",
                  "/topics", "/velocity", "/services", "/timeline",
                  "/api/people", "/api/pr-friction", "/api/releases", "/api/docs",
                  "/api/meetings", "/api/ask", "/api/topics", "/api/velocity",
                  "/api/services", "/api/timeline")
_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer",
        "upgrade", "proxy-authorization", "proxy-authenticate", "content-length"}

_children = []


def _spawn(port_dash, port_sprint, port_pages):
    """Launch all child servers on internal ports, nav links relative."""
    env = dict(os.environ, SYNAPSE_PLANNER_BASE="", SYNAPSE_DASHBOARD_BASE="")
    _children.append(subprocess.Popen(
        [sys.executable, DASHBOARD, "--host", "127.0.0.1", "--port", str(port_dash)], env=env))
    _children.append(subprocess.Popen(
        [sys.executable, SPRINT, str(port_sprint)], env=env))
    _children.append(subprocess.Popen(
        [sys.executable, PAGES, str(port_pages)], env=env))


def _wait_up(port, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
            return True
        except urllib.error.HTTPError:
            return True   # any HTTP response means it's listening
        except Exception:
            time.sleep(0.3)
    return False


def _shutdown(*_):
    for p in _children:
        try:
            p.terminate()
        except Exception:
            pass
    sys.exit(0)


def make_handler(port_dash, port_sprint, port_pages):
    def upstream_for(path):
        clean = path.split("?", 1)[0]
        if clean in DASH_ROUTES:
            return port_dash
        if clean.startswith(PAGES_PREFIXES):
            return port_pages
        return port_sprint

    class Proxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a, **k):
            pass

        def _relay(self, method):
            port = upstream_for(self.path)
            url = f"http://127.0.0.1:{port}{self.path}"
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else None
            req = urllib.request.Request(url, data=body, method=method)
            for k, v in self.headers.items():
                if k.lower() not in _HOP and k.lower() != "host":
                    req.add_header(k, v)
            try:
                resp = urllib.request.urlopen(req, timeout=120)
                status, hdrs, data = resp.status, resp.headers, resp.read()
            except urllib.error.HTTPError as e:            # 4xx/5xx from a child — relay as-is
                status, hdrs, data = e.code, e.headers, e.read()
            except Exception as e:
                self.send_error(502, f"upstream error: {e}")
                return
            self.send_response(status)
            for k, v in hdrs.items():
                if k.lower() not in _HOP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._relay("GET")

        def do_POST(self):
            self._relay("POST")

    return Proxy


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("SYNAPSE_PORT", 8765)))
    ap.add_argument("--dash-port", type=int, default=int(os.environ.get("SYNAPSE_DASH_PORT", 8766)))
    ap.add_argument("--sprint-port", type=int, default=int(os.environ.get("SYNAPSE_SPRINT_PORT", 8767)))
    ap.add_argument("--pages-port", type=int, default=int(os.environ.get("SYNAPSE_PAGES_PORT", 8768)))
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    import atexit
    atexit.register(_shutdown)

    _spawn(args.dash_port, args.sprint_port, args.pages_port)
    ok_d = _wait_up(args.dash_port)
    ok_s = _wait_up(args.sprint_port)
    ok_p = _wait_up(args.pages_port)
    sys.stderr.write(f"[synapse] dashboard :{args.dash_port} {'up' if ok_d else 'DOWN'} · "
                     f"planner :{args.sprint_port} {'up' if ok_s else 'DOWN'} · "
                     f"pages :{args.pages_port} {'up' if ok_p else 'DOWN'}\n")

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port),
                                make_handler(args.dash_port, args.sprint_port, args.pages_port))
    sys.stderr.write(f"[synapse] serving http://127.0.0.1:{args.port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown()


if __name__ == "__main__":
    main()

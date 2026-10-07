#!/usr/bin/env python3
"""Tiny local server for the sprint planner.

  GET /                      -> sprint-planner.html
  GET /sprint-planner.html   -> the UI
  GET /api/capacity          -> live capacity model (runs capacity_engine.build())

Static files are served from derived/. Run:
  OPSGENIE_API_KEY=... python3 derive/sprint_server.py [port]
"""
import os, sys, json, re, glob
import datetime as dt
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DERIVED = os.path.join(ROOT, "derived")
STATE = os.path.join(ROOT, "state")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "derive"))
import capacity_engine
import plan_brain

# Shared nav sidebar — one source of truth for the whole ecosystem.
import synapse_nav


def _inject_nav(html_bytes, clean_path):
    return synapse_nav.inject_bytes(html_bytes, synapse_nav.active_from_path(clean_path))


# The per-pod caches written by /api/initiatives are initiatives-default.json and
# initiatives-<8 hex>.json. initiatives-in.json and initiatives-out.json share the prefix but
# belong to the /plan sandbox and resolve-initiatives and have a different schema — matching
# those with a bare initiatives-*.json glob would corrupt them.
_CACHE_NAME = re.compile(r"^initiatives-(default|[0-9a-f]{8})(-arch)?\.json$")


def _initiative_cache_files():
    return [p for p in glob.glob(os.path.join(DERIVED, "initiatives-*.json"))
            if _CACHE_NAME.match(os.path.basename(p))]


def _edit_initiative_caches(mutate):
    """Apply `mutate(initiatives_list) -> bool` to every planner cache, saving those it changed."""
    for path in _initiative_cache_files():
        try:
            with open(path) as fh:
                doc = json.load(fh)
            if mutate(doc.get("initiatives") or []):
                with open(path, "w") as fh:
                    json.dump(doc, fh)
        except Exception as e:
            sys.stderr.write(f"[initiatives cache] {path}: {e}\n")


def _patch_epic_budgets_cache(result):
    """Fold a budget submit into the caches.

    Without this the caches keep the pre-submit budgets, so after a reload the planner
    diffs the plan against a stale number and shows an already-submitted row as pending."""
    written = {a["epic"] for a in (result.get("applied") or []) if a.get("ok")}
    by_epic = {}
    for d in result.get("diffs") or []:
        if d["epic"] in written:
            by_epic.setdefault(d["epic"], {})[d["month"]] = d["to"]
    if not by_epic:
        return

    def mutate(inits):
        hit = False
        for init in inits:
            epic = init.get("epic")
            if not isinstance(epic, dict) or epic.get("key") not in by_epic:
                continue
            init.setdefault("budgets", {}).update(by_epic[epic["key"]])
            hit = True
        return hit

    _edit_initiative_caches(mutate)


# planner column id -> where its display value lives in a cached initiative row
_COLUMN_CACHE_PATH = {
    "status":     ("init", "status"),       "orgPri":     ("init", "orgPriority"),
    "engDri":     ("init", "engDri"),       "prodDri":    ("init", "prodDri"),
    "cycles":     ("init", "cycles"),      "pods":       ("init", "podTags"),
    "epicSumm":   ("epic", "summary"),      "epicDue":    ("epic", "dueDate"),
    "epicStatus": ("epic", "status"),       "epicOwner":  ("epic", "assignee"),
    "epicHealth": ("epic", "health"),       "epicCycle":  ("epic", "cycles"),
    "epicPri":    ("epic", "priority"),     "epicLabels": ("epic", "labels"),
    "epicChal":   ("epic", "challenges"),
}


def _clear_initiative_epic_cache(initiative_key):
    """Drop the cached epic for one initiative after its link is removed, so a reload does
    not keep showing an epic the initiative no longer points at."""
    def mutate(inits):
        hit = False
        for init in inits:
            if init.get("key") == initiative_key and init.get("epic"):
                init["epic"] = None
                init["budgets"] = {}
                hit = True
        return hit
    _edit_initiative_caches(mutate)


def _patch_initiative_cache_column(issue_key, column, display, raw):
    """Fold an inline column edit into the planner caches, for the same reason as above."""
    where = _COLUMN_CACHE_PATH.get(column)
    if not where:
        return
    target, attr = where
    # multi-value fields are cached as lists; everything else as a display string
    value = ([t.strip() for t in (display or "").split(",") if t.strip()]
             if attr in ("cycles", "podTags") else display)

    def mutate(inits):
        hit = False
        for init in inits:
            if target == "init":
                if init.get("key") == issue_key:
                    init[attr] = value
                    hit = True
            else:
                epic = init.get("epic")
                if isinstance(epic, dict) and epic.get("key") == issue_key:
                    epic[attr] = value
                    hit = True
        return hit
    _edit_initiative_caches(mutate)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DERIVED, **k)

    def end_headers(self):
        """Never let a browser cache a planner page.

        These pages are hand-edited constantly, and the static handler's default
        Last-Modified caching meant a plain reload kept serving yesterday's copy: changes
        looked like they had not shipped until someone thought to hard-reload. API routes
        already set this per response; this covers the HTML.
        """
        if self.path.split("?")[0].endswith(".html"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

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
        if self.path.split("?")[0] == "/api/handover":
            # The program team's readiness CLI is slow (it walks every tagged team's epics
            # across projects), so it is cached on disk and refreshed with ?fresh=1 or once
            # a day. Every successful run also appends a snapshot, because the average on
            # its own is not a trend: the denominator moves constantly.
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            fresh = q.get("fresh", ["0"])[0] == "1"
            cachef = os.path.join(DERIVED, "handover.json")
            histf = os.path.join(STATE, "handover_history.jsonl")
            try:
                doc = None
                if not fresh and os.path.exists(cachef):
                    try:
                        with open(cachef) as fh:
                            cached = json.load(fh)
                        same_day = (cached.get("fetched") or "")[:10] == dt.date.today().isoformat()
                        if cached.get("v") == capacity_engine.HANDOVER_SCHEMA and same_day:
                            doc = cached
                    except Exception:
                        doc = None
                if doc is None:
                    doc = capacity_engine.handover_readiness()
                    if not doc.get("__error__"):
                        with open(cachef, "w") as fh:
                            json.dump(doc, fh)
                        capacity_engine.handover_snapshot(doc, histf)
                hist = []
                if os.path.exists(histf):
                    with open(histf) as fh:
                        hist = [json.loads(l) for l in fh if l.strip()]
                out = {"readiness": doc, "history": hist,
                       "acceptance": capacity_engine.em_acceptance(doc)
                                     if not doc.get("__error__") else {"__error__": doc["__error__"]}}
                body = json.dumps(out).encode()
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
                # an explicit window from the planner's date pickers; cached separately
                # from the default so switching back and forth stays cheap
                win, suffix = None, ""
                ws, we = q.get("start", [""])[0].strip(), q.get("end", [""])[0].strip()
                if ws and we:
                    a_, b_ = dt.date.fromisoformat(ws), dt.date.fromisoformat(we)
                    if b_ < a_:
                        raise ValueError("end is before start")
                    if (b_ - a_).days > 200:
                        raise ValueError("window longer than 200 days")
                    win, suffix = (a_, b_), f"-{ws}_{we}"
                cachef = os.path.join(DERIVED, f"month-{y:04d}-{m:02d}{suffix}.json")
                body = None
                if not fresh and os.path.exists(cachef):
                    with open(cachef, "rb") as f:
                        cached = f.read()
                    try:
                        ok = json.loads(cached).get("v") == capacity_engine.MONTH_SCHEMA
                    except Exception:
                        ok = False
                    if ok:
                        body = cached
                if body is None:
                    body = json.dumps(
                        capacity_engine.month_capacity(y, m, window=win)).encode()
                    with open(cachef, "wb") as f:
                        f.write(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except ValueError as e:
                # bad ?ym= or an impossible ?start=/?end= window: the caller's fault
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(
                    {"error": f"bad window (ym=YYYY-MM, optional start/end as YYYY-MM-DD): {e}"}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
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
        if self.path.split("?")[0] == "/api/dependencies":
            # Cached like /api/initiatives: the register walks every initiative AND every
            # counterpart pod's epics, so a cold build is slow. fresh=1 forces a rebuild.
            import hashlib
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            pods = [x for x in q.get("pods", [""])[0].split(",") if x] or None
            months = [x for x in q.get("months", [""])[0].split(",") if x] or None
            dri = q.get("dri", ["me"])[0] != "all"
            fresh = q.get("fresh", ["0"])[0] == "1"
            sig = "|".join([",".join(sorted(pods or [])), ",".join(sorted(months or [])), str(dri)])
            cachef = os.path.join(DERIVED, f"deps-{hashlib.md5(sig.encode()).hexdigest()[:8]}.json")
            try:
                body = None
                if not fresh and os.path.exists(cachef):
                    with open(cachef, "rb") as f:
                        cached = f.read()
                    try:
                        ok = json.loads(cached).get("v") == capacity_engine.DEPENDENCIES_SCHEMA
                    except Exception:
                        ok = False
                    if ok:
                        body = cached
                if body is None:
                    r = capacity_engine.pod_dependencies(pods, months, dri_only=dri)
                    if "__error__" in r:
                        self._reply(500, r)
                        return
                    body = json.dumps(r).encode()
                    with open(cachef, "wb") as f:
                        f.write(body)
                else:
                    body = json.dumps({**json.loads(body), "cached": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return
        if self.path.split("?")[0] == "/api/me":
            try:
                r = capacity_engine.current_user()
                self._reply(500 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return
        if self.path.split("?")[0] == "/api/planner-fields":
            # which planner columns are backed by a Jira field at all (config-driven)
            try:
                self._reply(200, {"fields": capacity_engine.planner_editable_fields()})
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return
        if self.path.split("?")[0] == "/api/user-search":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            try:
                r = capacity_engine.search_users(q.get("q", [""])[0])
                self._reply(500 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
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
        if self.path.split("?")[0] == "/api/retro-editmeta":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            key = q.get("key", [""])[0]
            months = [m for m in q.get("months", [""])[0].split(",") if m]
            try:
                r = capacity_engine.retro_editmeta(key, months)
                self._reply(500 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return
        if self.path.split("?")[0] == "/api/retro-notes":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            months = [m for m in q.get("months", [""])[0].split(",") if m]
            frm = q.get("from", [""])[0]
            to = q.get("to", [""])[0]
            try:
                body = json.dumps(capacity_engine.retro_notes(
                    months, start=frm or None, end=to or None)).encode()
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
            frm = q.get("from", [""])[0]
            to = q.get("to", [""])[0]
            fresh = q.get("fresh", ["0"])[0] == "1"
            period = f"{frm}..{to}" if (frm and to) else ",".join(sorted(months))
            tag = hashlib.md5(period.encode()).hexdigest()[:8] if period else "none"
            cachef = os.path.join(DERIVED, f"retro-{tag}.json")
            try:
                if not fresh and os.path.exists(cachef):
                    with open(cachef, "rb") as f:
                        body = f.read()
                else:
                    r = capacity_engine.retro_summary(months, start=frm or None, end=to or None)
                    body = json.dumps(r).encode()
                    if "__error__" not in r:        # never cache errors — they'd stick until fresh=1
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
            archived = q.get("archived", ["0"])[0] == "1"
            # archived rows are a different row SET, not a display filter, so they need their
            # own cache file — otherwise the two views overwrite each other
            sig = ",".join(sorted(pods)) if pods else ""
            tag = ("default" if not pods else hashlib.md5(sig.encode()).hexdigest()[:8]) + ("-arch" if archived else "")
            cachef = os.path.join(DERIVED, f"initiatives-{tag}.json")
            try:
                body = None
                if not fresh and os.path.exists(cachef):
                    with open(cachef, "rb") as f:
                        cached = f.read()
                    # drop caches written before the current payload shape, else the UI shows
                    # blanks for fields an older build never fetched
                    try:
                        ok = json.loads(cached).get("v") == capacity_engine.INITIATIVES_SCHEMA
                    except Exception:
                        ok = False
                    if ok:
                        body = cached
                if body is None:
                    body = json.dumps(
                        capacity_engine.pod_initiatives(pods, include_archived=archived)).encode()
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
        elif self.path.split("?")[0] in ("/deps", "/deps/"):
            self.path = "/deps.html"                   # cross-pod dependency coordination view
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
        if self.path.split("?")[0] == "/api/retro-update":
            # body: {key, fields?:{health|cycle|orgPriority|challenges: optionId(s)},
            #        budgets?:{Month:sp}, transition?:id} — writes to Jira, then purges
            # the retro caches so every window recomputes with the fresh values.
            try:
                p = self._json_body()
                r = capacity_engine.retro_update(p.get("key", ""), fields=p.get("fields"),
                                                 budgets=p.get("budgets"),
                                                 transition=p.get("transition"))
                if r.get("ok"):
                    import glob
                    for f in glob.glob(os.path.join(DERIVED, "retro-*.json")):
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                self._reply(500 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return
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
        if self.path.split("?")[0] == "/api/link-audit":
            # body: {pods?:[...], fix:bool} — report (fix=false) or repair reversed links
            try:
                p = self._json_body()
                r = capacity_engine.audit_epic_links(p.get("pods"), fix=bool(p.get("fix")))
                self._reply(500 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return
        if self.path.split("?")[0] == "/api/unlink-epic":
            # body: {initiative, epicKey?} — drops the issue link only; the epic survives
            try:
                p = self._json_body()
                r = capacity_engine.unlink_epic(p.get("initiative", ""), p.get("epicKey"))
                if r.get("ok"):
                    _clear_initiative_epic_cache(r["initiative"])
                self._reply(500 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return
        if self.path.split("?")[0] == "/api/field-meta":
            # body: {key, columns:[planner column ids]} -> what may be edited + options
            try:
                p = self._json_body()
                r = capacity_engine.field_editmeta(p.get("key", ""), p.get("columns", []))
                self._reply(500 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return
        if self.path.split("?")[0] == "/api/set-field":
            # body: {key, column, value} -> writes one planner column to Jira
            try:
                p = self._json_body()
                r = capacity_engine.set_issue_field(p.get("key", ""), p.get("column", ""),
                                                    p.get("value"))
                if r.get("ok"):
                    _patch_initiative_cache_column(p.get("key", ""), p.get("column", ""),
                                                   r.get("display", ""), r.get("to"))
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
                if not r.get("dryRun", True) and "__error__" not in r:
                    _patch_epic_budgets_cache(r)
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

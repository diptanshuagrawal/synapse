#!/usr/bin/env python3
"""Tiny local server for the sprint planner.

  GET /                      -> sprint-planner.html
  GET /sprint-planner.html   -> the UI
  GET /api/capacity          -> live capacity model (runs capacity_engine.build())

Static files are served from derived/. Run:
  OPSGENIE_API_KEY=... python3 derive/sprint_server.py [port]
"""
import os, sys, json, re, glob, time
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


# Suggestions are expensive to work out and cheap to keep: the ticket scan alone reads
# over a thousand issues. Cached on disk rather than in the page, so they are still there
# after you navigate away, and the same in every browser rather than in whichever one
# happened to run them.
_SUGGEST_CACHE = os.path.join(ROOT, "state", "jira_push_suggest.json")
_SUGGEST_TTL = 6 * 60 * 60     # a working half-day; Jira does not change that fast


def _suggest_cache_read():
    try:
        with open(_SUGGEST_CACHE) as fh:
            doc = json.load(fh)
    except Exception:
        return {}
    if time.time() - float(doc.get("at") or 0) > _SUGGEST_TTL:
        return {}
    return doc if isinstance(doc.get("candidates"), dict) else {}


def _suggest_cache_write(candidates, links):
    try:
        os.makedirs(os.path.dirname(_SUGGEST_CACHE), exist_ok=True)
        tmp = _SUGGEST_CACHE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"at": time.time(), "candidates": candidates, "links": links}, fh)
        os.replace(tmp, _SUGGEST_CACHE)
    except Exception as e:
        sys.stderr.write(f"[jira-push suggest cache] {e}\n")


def _with_derived(plan, people, window):
    """The plan plus the tiles nobody draws.

    Oncall comes out of the capacity calendar rather than off the board, so it is added
    here, with whatever was decided about it last time applied on top. Returns a COPY:
    the board store is not the place to materialise something derived, or it stops
    following the rota the moment the rota changes.
    """
    import jira_push, retro_engine
    extra = jira_push.oncall_tiles(people, window)
    if not extra:
        return plan
    saved = retro_engine.push_decisions()
    merged = []
    for t in extra:
        d = saved.get(t["id"]) or {}
        merged.append({**t, **{k: v for k, v in d.items() if v not in (None, "")}})
    out = dict(plan or {})
    out["tasks"] = list(plan.get("tasks") or []) + merged
    return out


def _task_type():
    """The issue type created tickets get, and the one existing ones are checked against.

    From config. There is no fallback on purpose: a wrong guess here produced fifteen
    Stories where the team wanted Tasks, and nothing on the page said so.
    """
    import capacity_engine as ce
    cfg = ce._optional_yaml("sprint_planning.yaml") or {}
    jf = cfg.get("jira_fields") or {}
    return str(cfg.get("task_issuetype_id") or jf.get("task_issuetype_id") or "").strip()


_TYPE_NAMES = {}


def _task_type_name(tid):
    """The name of that type, so the preview reads "Story -> Task" and not "Story -> 10002"."""
    if not tid:
        return ""
    if tid not in _TYPE_NAMES:
        import capacity_engine as ce
        try:
            d = ce._jira("GET", f"/rest/api/3/project/{ce.JIRA_PROJECT}", None)
            for t in d.get("issueTypes") or []:
                _TYPE_NAMES[str(t.get("id"))] = t.get("name") or ""
        except Exception:
            return tid
    return _TYPE_NAMES.get(tid) or tid


def _standing_rules():
    """Work that exists whether or not it is planned, from config/sprint_planning.yaml."""
    import capacity_engine as ce
    cfg = ce._optional_yaml("sprint_planning.yaml") or {}
    rules = ((cfg.get("push") or {}).get("standing_work")) or []
    return [r for r in rules if isinstance(r, dict) and r.get("match")]


def _ticket_matches(titles):
    """Existing tickets each tile may already BE. Read-only.

    One scan of the project's recent issues, then `jira_push.match_tickets` per tile.
    Linking beats guessing an epic, because a real ticket already carries its own.
    """
    import capacity_engine as ce
    import jira_push

    issues, page = [], None
    while len(issues) < 1500:
        body = {"jql": f"project = {ce.JIRA_PROJECT} AND issuetype != Epic "
                       f"AND created >= -270d",
                "fields": ["summary", "parent", "status"], "maxResults": 100}
        if page:
            body["nextPageToken"] = page
        d = ce._jira("POST", "/rest/api/3/search/jql", body)
        for i in d.get("issues") or []:
            f = i.get("fields") or {}
            issues.append({"key": i["key"], "summary": f.get("summary") or "",
                           "epic": ((f.get("parent") or {}).get("key") or ""),
                           "status": ((f.get("status") or {}).get("name") or "")})
        page = d.get("nextPageToken")
        if d.get("isLast") or not page:
            break
    # Finished work is not a candidate for a sprint being planned. It was offered
    # dimmed for a while; in practice it was only ever noise to scroll past.
    live = [i for i in issues
            if str(i.get("status") or "").lower() not in jira_push.DEAD]
    uniq = sorted({str(t or "").strip() for t in titles if str(t or "").strip()})
    return {t: jira_push.match_tickets(t, live) for t in uniq}


def _epic_candidates(titles, all_epics=None):
    """Ask Jira which epic each tile looks like, the way /resolve-initiatives does.

    Two text searches per tile (epic titles, then ticket summaries), fanned out, then
    `jira_push.rank_epics` counts the votes. Read-only, and the result is a SUGGESTION:
    nothing here is written to the board or to Jira.
    """
    import capacity_engine as ce
    import jira_push
    from concurrent.futures import ThreadPoolExecutor

    def one(text):
        safe = str(text or "").replace('"', "").strip()
        if len(safe) < 3:
            return []
        try:
            eps = ce._jira("POST", "/rest/api/3/search/jql",
                           {"jql": f'project = {ce.JIRA_PROJECT} AND issuetype = Epic '
                                   f'AND summary ~ "{safe}"',
                            "fields": ["summary", "status"], "maxResults": 20})
            tks = ce._jira("POST", "/rest/api/3/search/jql",
                           {"jql": f'project = {ce.JIRA_PROJECT} AND issuetype != Epic '
                                   f'AND summary ~ "{safe}"',
                            "fields": ["summary", "status", "parent"], "maxResults": 50})
        except Exception:
            return []
        # Jira's own hits, plus the epics whose NAME looks like this tile. Both vote.
        ep_hits = list(jira_push.title_matches(safe, all_epics or []))
        ep_hits += [{"key": i["key"],
                    "summary": (i.get("fields") or {}).get("summary") or "",
                    "status": (((i.get("fields") or {}).get("status")) or {}).get("name") or ""}
                   for i in eps.get("issues") or []]
        tk_hits = []
        for i in tks.get("issues") or []:
            f = i.get("fields") or {}
            par = f.get("parent") or {}
            tk_hits.append({"key": i["key"], "summary": f.get("summary") or "",
                            "status": ((f.get("status")) or {}).get("name") or "",
                            "parent": par.get("key") or "",
                            "parentSummary": ((par.get("fields") or {}).get("summary") or "")})
        return jira_push.rank_epics(ep_hits, tk_hits)

    uniq = sorted({str(t or "").strip() for t in titles if str(t or "").strip()})
    with ThreadPoolExecutor(max_workers=6) as ex:
        found = list(ex.map(one, uniq))
    return dict(zip(uniq, found))


def _jira_snapshot(plan):
    """Everything the decision layer needs to know about Jira, fetched once.

    Deliberately read-only and deliberately separate: `jira_push.plan_push` is pure, so
    this is the only part of the preview that can fail for a reason outside the plan.
    """
    import capacity_engine as ce, retro_engine
    try:
        epics, page = [], None
        while True:
            body = {"jql": f"project = {ce.JIRA_PROJECT} AND issuetype = Epic "
                           f"AND statusCategory != Done",
                    "fields": ["summary", "status"], "maxResults": 100}
            if page:
                body["nextPageToken"] = page
            d = ce._jira("POST", "/rest/api/3/search/jql", body)
            epics += [{"key": i["key"], "summary": (i.get("fields") or {}).get("summary") or ""}
                      for i in d.get("issues") or []]
            page = d.get("nextPageToken")
            if d.get("isLast") or not page:
                break

        want = sorted({str(t.get("epic") or "").strip() for t in plan.get("tasks") or []}
                      | {str(v or "").strip() for v in (plan.get("epics") or {}).values()})
        want = [w for w in want if w]
        by_epic = {}
        if want:
            jql = "parent in (" + ", ".join(want) + ")"
            d = ce._jira("POST", "/rest/api/3/search/jql",
                         {"jql": jql, "fields": ["summary", "parent"], "maxResults": 200})
            for i in d.get("issues") or []:
                par = ((i.get("fields") or {}).get("parent") or {}).get("key")
                by_epic.setdefault(par, []).append(
                    {"key": i["key"], "summary": (i.get("fields") or {}).get("summary") or ""})

        keys = [str(t.get("key") or "").strip() for t in plan.get("tasks") or []]
        keys = [k for k in keys if k]
        issues = {}
        if keys:
            sp = ce.SP_FIELD
            d = ce._jira("POST", "/rest/api/3/search/jql",
                         {"jql": "key in (" + ", ".join(keys) + ")",
                          "fields": ["summary", "parent", "assignee", sp, "issuetype",
                                     retro_engine.SPRINT_FIELD],
                          "maxResults": 200})
            for i in d.get("issues") or []:
                f = i.get("fields") or {}
                issues[i["key"]] = {
                    "summary": f.get("summary") or "",
                    "epic": ((f.get("parent") or {}) or {}).get("key") or "",
                    "assignee": ((f.get("assignee") or {}) or {}).get("accountId") or "",
                    "assigneeName": ((f.get("assignee") or {}) or {}).get("displayName") or "",
                    "sp": f.get(sp) or 0,
                    "issuetype": ((f.get("issuetype") or {}) or {}).get("id") or "",
                    "issuetypeName": ((f.get("issuetype") or {}) or {}).get("name") or "",
                    "sprintIds": [s.get("id") for s in (f.get(retro_engine.SPRINT_FIELD) or [])
                                  if isinstance(s, dict)]}

        return {"epics": epics, "sprints": retro_engine.board_sprints(40, retro_engine.ALL_STATES),
                "issues": issues, "byEpic": by_epic}
    except Exception as e:
        return {"__error__": str(e)}


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
                # This cache had NO freshness rule: once written it was served forever, and
                # the page never asks for fresh=1. A pod RENAMED in Jira therefore never
                # reached the filter — the dropdown kept offering the dead option and could
                # not offer the live one, while the board itself loaded the new pod fine.
                # Only the dropdown was frozen, which is what made it hard to spot. Rebuild
                # daily, like /api/handover; `pod_options()` is a whole-project scan, which
                # is why it is cached at all.
                body = None
                if not fresh and os.path.exists(cachef):
                    try:
                        with open(cachef, "rb") as f:
                            raw = f.read()
                        cached = json.loads(raw)
                        if (cached.get("generated") or "") == dt.date.today().isoformat():
                            body = raw
                    except Exception:
                        body = None
                if body is None:
                    body = json.dumps({"generated": dt.date.today().isoformat(),
                                       "pods": capacity_engine.pod_options()}).encode()
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
        # ---- sprint retro v2: pickers + the planned-vs-actual / productivity doc ----
        # The pickers are cheap and change rarely, so they are rebuilt daily like /api/pods
        # (an undated cache is what froze the pod filter through a rename). The retro doc
        # itself is cached per sprint-set + project-set and versioned on retro_engine.SCHEMA,
        # so a payload shape change can never be served from a stale file.
        if self.path.split("?")[0] in ("/api/sprints", "/api/projects"):
            import retro_engine
            which = self.path.split("?")[0].rsplit("/", 1)[1]
            cachef = os.path.join(DERIVED, f"{which}.json")
            try:
                body = None
                if os.path.exists(cachef):
                    try:
                        raw = open(cachef, "rb").read()
                        if json.loads(raw).get("generated") == dt.date.today().isoformat():
                            body = raw
                    except Exception:
                        body = None
                if body is None:
                    data = (retro_engine.board_sprints() if which == "sprints"
                            else retro_engine.jira_projects())
                    body = json.dumps({"generated": dt.date.today().isoformat(),
                                       which: data}).encode()
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

        # Mid-sprint check. Deliberately NOT cached by date like /api/retro2: the whole point
        # is the state of a sprint that is still moving, and a day-old answer is the thing
        # this view exists to stop you acting on.
        # EM leave override: read the current overrides so the page can render provenance.
        if self.path.split("?")[0] == "/api/leave":
            try:
                body = json.dumps({"manual": capacity_engine.manual_leaves()}).encode()
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

        if self.path.split("?")[0] == "/api/jira-push/row":
            # One tile re-evaluated. Same decision layer as the full preview, scoped to
            # a single task, so accepting a link does not cost a Jira round trip for
            # every other tile on the board.
            import retro_engine, jira_push
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            tid = q.get("id", [""])[0].strip()
            frm, to = q.get("from", [""])[0].strip(), q.get("to", [""])[0].strip()
            sprints = [x for x in q.get("sprints", [""])[0].split(",") if x.strip()]
            try:
                win = (frm, to) if (frm and to) else None
                board = retro_engine.sprint_board(sprints, None, window=win)
                if "__error__" in board:
                    self._reply(400, board)
                    return
                plan = board.get("plan") or {}
                w = [board["window"]["start"], board["window"]["end"]]
                people = board.get("people") or []
                plan = _with_derived(plan, people, w)     # before the snapshot, as above
                opts = {"standing": _standing_rules(), "issuetype": _task_type(),
                        "issuetypeName": _task_type_name(_task_type())}
                # The whole board is still evaluated, because the counts and the
                # ready gate are properties of all of it; only Jira is asked about
                # the one tile's ticket.
                one = [t for t in (plan.get("tasks") or []) if str(t.get("id")) == tid]
                snap = _jira_snapshot(plan)
                if "__error__" in snap:
                    self._reply(502, snap)
                    return
                pv = jira_push.plan_push(plan, people, w, snap, opts)
                row = next((r for r in pv.get("rows") or [] if str(r.get("id")) == tid), None)
                if row is None and one:
                    self._reply(404, {"__error__": f"{tid} is not on the board in this window"})
                    return
                self._reply(200, {"row": row, "counts": pv.get("counts"),
                                  "ready": pv.get("ready"), "unmapped": pv.get("unmapped")})
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return

        if self.path.split("?")[0] == "/api/jira-push":
            import retro_engine, jira_push
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            frm, to = q.get("from", [""])[0].strip(), q.get("to", [""])[0].strip()
            sprints = [x for x in q.get("sprints", [""])[0].split(",") if x.strip()]
            try:
                win = (frm, to) if (frm and to) else None
                board = retro_engine.sprint_board(sprints, None, window=win)
                if "__error__" in board:
                    self._reply(400, board)
                    return
                plan = board.get("plan") or {}
                import capacity_engine as ce
                w = [board["window"]["start"], board["window"]["end"]]
                people = board.get("people") or []
                # BEFORE the snapshot. The derived tiles carry keys of their own, and a
                # snapshot taken without them never looks those keys up, so the rows came
                # back as "not found in Jira" and were skipped on every publish.
                plan = _with_derived(plan, people, w)
                snap = _jira_snapshot(plan)
                if "__error__" in snap:
                    self._reply(502, snap)
                    return
                pv = jira_push.plan_push(plan, people, w, snap,
                                         {"fallbackEpic": q.get("fallback", [""])[0].strip(),
                                          "overwritePoints": q.get("points", [""])[0] == "1",
                                          "standing": _standing_rules(),
                                          "issuetype": _task_type(),
                                          "issuetypeName": _task_type_name(_task_type())})
                # Without this every "open" link on the page pointed at https://jira/.
                pv["host"] = ce.JIRA_HOST
                pv["epics"] = snap.get("epics") or []
                pv["mapping"] = plan.get("epics") or {}
                pv["window"] = w
                # Whatever was worked out last time, so coming back to the page does not
                # mean clicking Suggest and waiting again.
                cached = _suggest_cache_read()
                pv["suggest"] = cached.get("candidates") or {}
                pv["links"] = cached.get("links") or {}
                pv["suggestAt"] = cached.get("at") or 0
                self._reply(200, pv)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return

        if self.path.split("?")[0] in ("/api/monthly-plan", "/api/monthly-plan/history"):
            import plan_store
            if self.path.split("?")[0].endswith("/history"):
                self._reply(200, {"versions": plan_store.history()})
                return
            doc = plan_store.read_plan()
            # `has` is what the page uses to decide whether to adopt the stored plan or to
            # offer to seed it from this browser. An empty order is not a plan.
            self._reply(200, {"plan": doc, "has": bool(doc.get("order"))})
            return

        if self.path.split("?")[0] == "/api/sprint-board":
            import retro_engine
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            sprints = [x for x in q.get("sprints", [""])[0].split(",") if x.strip()]
            projects = [x for x in q.get("projects", [""])[0].split(",") if x.strip()]
            frm, to = q.get("from", [""])[0].strip(), q.get("to", [""])[0].strip()
            win = (frm, to) if (frm and to) else None
            if not sprints and not win:
                self._reply(400, {"error": "pick a sprint, or a from/to date range"})
                return
            try:
                r = retro_engine.sprint_board(sprints, projects or None, window=win)
                self._reply(500 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"error": str(e)})
            return

        if self.path.split("?")[0] == "/api/sprint-check":
            import retro_engine
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            sprints = [x for x in q.get("sprints", [""])[0].split(",") if x.strip()]
            projects = [x for x in q.get("projects", [""])[0].split(",") if x.strip()]
            try:
                stale = int(q.get("stale", ["3"])[0])
            except ValueError:
                stale = 3
            if not sprints:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "no sprints selected"}).encode())
                return
            try:
                r = retro_engine.sprint_check(sprints, projects or None, stale_days=stale)
                if "__error__" not in r:
                    r = retro_engine.sprint_insights(r)
                    # The narrative layer is produced by a Claude session (the /sprint-check
                    # skill), not by this server, and is optional: the page renders without it.
                    nf = os.path.join(DERIVED, "sprint-check-notes.json")
                    if os.path.exists(nf):
                        try:
                            with open(nf) as fh:
                                r["notes"] = json.load(fh)
                        except Exception:
                            pass
                    with open(os.path.join(DERIVED, "sprint-check-dump.json"), "w") as fh:
                        json.dump(r, fh, indent=1)
                body = json.dumps(r).encode()
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

        if self.path.split("?")[0] == "/api/retro2":
            import hashlib
            import retro_engine
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            sprints = [s for s in q.get("sprints", [""])[0].split(",") if s.strip()]
            projects = [p for p in q.get("projects", [""])[0].split(",") if p.strip()]
            fresh = q.get("fresh", ["0"])[0] == "1"
            if not sprints:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "no sprints selected"}).encode())
                return
            tag = hashlib.md5((",".join(sorted(sprints)) + "|"
                               + ",".join(sorted(projects))).encode()).hexdigest()[:8]
            cachef = os.path.join(DERIVED, f"retro2-{tag}.json")
            try:
                body = None
                if not fresh and os.path.exists(cachef):
                    try:
                        raw = open(cachef, "rb").read()
                        c = json.loads(raw)
                        if (c.get("v") == retro_engine.SCHEMA
                                and c.get("generated") == dt.date.today().isoformat()):
                            body = raw
                    except Exception:
                        body = None
                if body is None:
                    r = retro_engine.sprint_insights(
                        retro_engine.sprint_retro(sprints, projects or None))
                    body = json.dumps(r).encode()
                    if "__error__" not in r:    # never cache an error — it would stick
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
        elif self.path.split("?")[0] in ("/retro", "/retro/", "/sprint-retro", "/retro2"):
            # /retro IS the sprint retro now. The previous page kept highs/lows notes, which
            # the new one does not replace, so it stays reachable at /retro-classic rather
            # than being deleted — those notes are saved per period in the user's browser.
            self.path = "/retro2.html"
        elif self.path.split("?")[0] in ("/jira-push", "/jira-push/", "/push"):
            self.path = "/jira-push.html"              # board to a real Jira sprint
        elif self.path.split("?")[0] in ("/sprint-board", "/sprint-board/", "/board"):
            self.path = "/sprint-board.html"           # manual per-dev sprint gantt
        elif self.path.split("?")[0] in ("/sprint-check", "/sprint-check/"):
            self.path = "/sprint-check.html"           # mid-sprint: hygiene, pace, why
        elif self.path.split("?")[0] in ("/retro-classic", "/retro-classic/"):
            self.path = "/retro.html"                  # highs/lows + planned-vs-actual by epic
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
        # body: {canonical, dates:[iso], code: "L"|"W"|"P"|"", by?, note?}
        # Marks or clears leave the team never posted. Capacity is cached per window, so
        # every month cache is purged after a write — otherwise the grid would show the new
        # marker while the SP total beside it still reflected the old day count.
        # Save the manual board. Scratch only: never writes assignee or sprint to Jira.
        if self.path.split("?")[0] == "/api/jira-push/suggest":
            # Suggestions only. This route reads Jira and writes nothing, anywhere.
            try:
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length") or 0)) or b"{}")
            except Exception as e:
                self._reply(400, {"__error__": f"bad request: {e}"})
                return
            titles = [str(t) for t in (body.get("titles") or [])][:120]
            try:
                import capacity_engine as ce
                eps, page = [], None
                while True:
                    b = {"jql": f"project = {ce.JIRA_PROJECT} AND issuetype = Epic "
                                f"AND statusCategory != Done",
                         "fields": ["summary", "status"], "maxResults": 100}
                    if page:
                        b["nextPageToken"] = page
                    d = ce._jira("POST", "/rest/api/3/search/jql", b)
                    for i in d.get("issues") or []:
                        f = i.get("fields") or {}
                        eps.append({"key": i["key"], "summary": f.get("summary") or "",
                                    "status": ((f.get("status")) or {}).get("name") or ""})
                    page = d.get("nextPageToken")
                    if d.get("isLast") or not page:
                        break
                links = _ticket_matches(titles)
                cands = _epic_candidates(titles, eps)
                _suggest_cache_write(cands, links)
                self._reply(200, {"candidates": cands, "links": links})
            except Exception as e:
                self._reply(502, {"__error__": str(e)})
            return

        if self.path.split("?")[0] == "/api/jira-push/mapping":
            import retro_engine
            try:
                p = self._json_body()
                r = retro_engine.save_board_epics(p.get("epics") or {}, {})
                if isinstance(r, dict) and "__error__" in r:
                    self._reply(400, r)
                    return
                # Everything decided per tile: the ticket it is linked to, an epic for
                # this one tile, a skip, a hand-set estimate. Saved as you go, so leaving
                # the page does not throw the afternoon away.
                d = retro_engine.save_push_decisions(p.get("decisions") or {})
                if isinstance(d, dict) and "__error__" in d:
                    self._reply(400, d)
                    return
                r["tiles"] = d.get("saved", 0)
                self._reply(200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return

        # Writes to Jira. Never reached without `confirm`, and jira_push refuses again on
        # its own account - two gates, because this is the one thing here with no undo.
        if self.path.split("?")[0] == "/api/jira-push/apply":
            import capacity_engine as ce, retro_engine, jira_push
            try:
                p = self._json_body()
                if not p.get("confirm"):
                    self._reply(400, {"__error__": "not confirmed"})
                    return
                import yaml
                cfg = {}
                try:
                    with open(os.path.join(DERIVED, "..", "config",
                                           "sprint_planning.yaml")) as fh:
                        cfg = yaml.safe_load(fh) or {}
                except Exception:
                    cfg = {}
                jf = cfg.get("jira_fields") or {}
                fields = {"story_points": jf.get("story_points"),
                          "issuetype_id": _task_type()}
                pv = p.get("preview") or {}
                # The name the page showed in the box, not the one derived from dates.
                # Creating "2026-10-21 to 2026-11-03" next to "TEAM OND 26 S1" leaves a
                # sprint on the board that nobody recognises.
                want = str(p.get("sprintName") or "").strip()
                if want and not (pv.get("sprint") or {}).get("exists"):
                    pv = dict(pv)
                    pv["sprint"] = {**(pv.get("sprint") or {}), "name": want}
                r = jira_push.apply_push(
                    pv, ce._jira, ce._board_id(), ce.JIRA_PROJECT,
                    fields, confirm=True,
                    on_key=lambda tid, key: retro_engine.set_task_key(tid, key))
                self._reply(400 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return

        if self.path.split("?")[0] == "/api/monthly-plan":
            import plan_store
            try:
                p = self._json_body()
                r = plan_store.write_plan(p, by=p.get("savedBy") or "",
                                          base_rev=p.get("rev"))
                # 409 for a conflict: the write was refused because another window moved
                # the plan on, which is a different thing from malformed input.
                code = 200 if "__error__" not in r else (409 if r.get("conflict") else 400)
                self._reply(code, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return

        if self.path.split("?")[0] == "/api/monthly-plan/restore":
            import plan_store
            try:
                p = self._json_body()
                r = plan_store.restore(p.get("file") or "")
                self._reply(400 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return

        if self.path.split("?")[0] == "/api/sprint-board":
            import retro_engine
            try:
                p = self._json_body()
                r = retro_engine.save_board_plan(
                    p.get("tasks") or [], cats=p.get("cats") or [],
                    effort=p.get("effort"))
                self._reply(400 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return

        if self.path.split("?")[0] == "/api/leave":
            try:
                p = self._json_body()
                r = capacity_engine.set_manual_leave(
                    p.get("canonical", ""), p.get("dates") or [], p.get("code", ""),
                    by=p.get("by", ""), note=p.get("note", ""))
                if r.get("ok"):
                    import glob
                    for f in glob.glob(os.path.join(DERIVED, "month-*.json")):
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                self._reply(400 if "__error__" in r else 200, r)
            except Exception as e:
                self._reply(500, {"__error__": str(e)})
            return

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

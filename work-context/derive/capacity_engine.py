#!/usr/bin/env python3
"""capacity_engine.py — compute sprint capacity from live + config data.

Sources (all already in the platform — no manual entry):
  - sprint window : Jira active sprint (board from config) -> next Wed-to-Wed window
  - roster        : config/people.yaml (scope == team, minus owner + leavers)
  - efficiency    : config/tier_expectations.yaml (per-tier band midpoint)
  - holidays      : config/holidays-<year>.yaml (mandatory cut capacity; optional shaded)
  - leaves        : team_leaves table (wfh counts as working; rest reduce)
  - on-call       : Opsgenie schedule, per working day (subtracted from capacity)

Emits a JSON capacity model (stdout + derived/capacity.json) consumed by the
sprint planner UI and, later, the /sprint-capacity skill.
"""
import os, sys, json, subprocess, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import yaml
from ingest.common import get_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "config")
SECRETS = os.path.expanduser("~/.secrets")
# Org-specific values load from config (gitignored) / env — never hardcoded.
# See config/sprint_planning.example.yaml, config/sources.yaml, config/oncall.yaml.
JIRA_HOST = JIRA_PROJECT = OWNER_EMAIL = OPSGENIE_SCHEDULE = ""
JIRA_BOARD = None
EXCLUDE_CANONICAL = set()
ROLE_OVERRIDE = {}


def _yaml(name):
    with open(os.path.join(CFG, name)) as f:
        return yaml.safe_load(f)


def _secret(name):
    p = os.path.join(SECRETS, name)
    return open(p).read().strip() if os.path.exists(p) else None


def _optional_yaml(name):
    try:
        return _yaml(name) or {}
    except Exception:
        return {}


def _load_cfg():
    """Populate org-specific globals from config + env (nothing hardcoded)."""
    global JIRA_HOST, JIRA_PROJECT, OWNER_EMAIL, OPSGENIE_SCHEDULE, JIRA_BOARD, EXCLUDE_CANONICAL, ROLE_OVERRIDE
    src = _optional_yaml("sources.yaml")
    onc = _optional_yaml("oncall.yaml")
    sp = _optional_yaml("sprint_planning.yaml")
    JIRA_HOST = os.environ.get("JIRA_DOMAIN") or (src.get("atlassian") or {}).get("host", "")
    OWNER_EMAIL = (src.get("org") or {}).get("owner_email", "")
    pk = (src.get("jira") or {}).get("project_keys") or []
    JIRA_PROJECT = pk[0] if pk else ""
    OPSGENIE_SCHEDULE = (onc.get("opsgenie") or {}).get("schedule", "")
    JIRA_BOARD = sp.get("board_id")
    EXCLUDE_CANONICAL = set(sp.get("exclude_canonical") or [])
    ROLE_OVERRIDE = sp.get("role_overrides") or {}


_load_cfg()


def _board_id():
    """Configured board, else discover the project's first scrum board."""
    if JIRA_BOARD:
        return JIRA_BOARD
    email, token = _secret("atlassian_email"), _secret("atlassian_token")
    import urllib.request, base64
    try:
        url = f"https://{JIRA_HOST}/rest/agile/1.0/board?projectKeyOrId={JIRA_PROJECT}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode())
        data = json.load(urllib.request.urlopen(req, timeout=20))
        vals = data.get("values", [])
        for b in vals:
            if b.get("type") == "scrum":
                return b["id"]
        return vals[0]["id"] if vals else None
    except Exception as e:
        sys.stderr.write(f"[board] {e}\n")
        return None


def eff_by_role():
    tiers = _yaml("tier_expectations.yaml")["tiers"]
    out = {}
    for tier, v in tiers.items():
        out[tier] = round((v["sp_efficiency_low"] + v["sp_efficiency_high"]) / 2, 2)
    return out


def roster():
    people = _yaml("people.yaml")["people"]
    team = []
    seen = set()
    for p in people:
        if p.get("scope") != "team":
            continue
        canon = p.get("canonical")
        email = p.get("email", "")
        if not canon or canon in seen:
            continue
        if email == OWNER_EMAIL or canon in EXCLUDE_CANONICAL:
            continue
        role = ROLE_OVERRIDE.get(canon, p.get("role"))
        if role not in ("SDE1", "SDE2", "SDE3"):
            continue
        seen.add(canon)
        team.append({"name": p["name"], "canonical": canon, "role": role,
                     "email": email})
    return team


def _snap_to_sprint_start(end):
    """Map a current-sprint endDate to the upcoming sprint's start.

    Jira's endDate is the current sprint's last day (a Tuesday, ~18:00 IST); the
    upcoming sprint is Wednesday-anchored, so snap forward to that Wednesday
    (no-op if `end` already lands on a Wednesday)."""
    while end.weekday() != 2:   # Mon=0 … Wed=2
        end += dt.timedelta(days=1)
    return end


def active_sprint():
    """Return (start_date, label, sprint_id) for the UPCOMING sprint (Wed-to-Wed)."""
    email, token = _secret("atlassian_email"), _secret("atlassian_token")
    try:
        import urllib.request, base64
        url = f"https://{JIRA_HOST}/rest/agile/1.0/board/{_board_id()}/sprint?state=active"
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode())
        data = json.load(urllib.request.urlopen(req, timeout=20))
        s = data["values"][0]
        end = dt.datetime.fromisoformat(s["endDate"].replace("Z", "+00:00")).date()
        return _snap_to_sprint_start(end), s.get("name", ""), s.get("id")
    except Exception as e:
        sys.stderr.write(f"[sprint] fallback: {e}\n")
        return dt.date(2026, 6, 24), "active sprint", None


def spillover(sprint_id, email2canon):
    """canonical -> list of not-done current-sprint tickets carried by that person."""
    if not sprint_id:
        return {}
    email, token = _secret("atlassian_email"), _secret("atlassian_token")
    import urllib.request, base64
    out = {}
    try:
        url = (f"https://{JIRA_HOST}/rest/agile/1.0/sprint/{sprint_id}/issue?maxResults=200"
               "&fields=summary,status,assignee,customfield_10051,parent")
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode())
        data = json.load(urllib.request.urlopen(req, timeout=25))
        for i in data.get("issues", []):
            f = i["fields"]
            if f["status"]["statusCategory"]["key"] == "done":
                continue
            asg = (f.get("assignee") or {}).get("emailAddress")
            c = email2canon.get(asg)
            if not c:
                continue
            out.setdefault(c, []).append({
                "key": i["key"], "summary": (f.get("summary") or "")[:60],
                "status": f["status"]["name"], "sp": f.get("customfield_10051") or 0,
                "epic": (f.get("parent") or {}).get("key", "")})
    except Exception as e:
        sys.stderr.write(f"[spillover] {e}\n")
    return out


def oncall_for(days):
    key = os.environ.get("OPSGENIE_API_KEY") or _secret("opsgenie_api_key")
    out = {}
    if not key:
        return out
    import urllib.request
    for d in days:
        if d.weekday() >= 5:
            continue
        try:
            url = (f"https://api.opsgenie.com/v2/schedules/{OPSGENIE_SCHEDULE}/on-calls"
                   f"?scheduleIdentifierType=name&flat=true&date={d.isoformat()}T06:30:00Z")
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"GenieKey {key}")
            data = json.load(urllib.request.urlopen(req, timeout=20))
            rec = data.get("data", {}).get("onCallRecipients", [])
            if rec:
                out[d.isoformat()] = rec[0]
        except Exception as e:
            sys.stderr.write(f"[oncall {d}] {e}\n")
    return out


def leaves_for(window_start, window_end):
    """canonical -> {date_iso: 'W'|'L'} over [window_start, window_end]."""
    conn = get_db()
    rows = conn.execute(
        "SELECT actor, date_start, date_end, reason FROM team_leaves "
        "WHERE date_start <= ? AND (date_end >= ? OR date_end IS NULL)",
        (window_end.isoformat(), window_start.isoformat())).fetchall()
    out = {}
    for r in rows:
        actor = r["actor"]
        ds = dt.date.fromisoformat(r["date_start"])
        de = dt.date.fromisoformat(r["date_end"]) if r["date_end"] else ds
        code = "W" if (r["reason"] or "").lower() == "wfh" else "L"
        d = ds
        while d <= de:
            if window_start <= d <= window_end:
                cur = out.setdefault(actor, {})
                # leave beats wfh if overlapping
                if cur.get(d.isoformat()) != "L":
                    cur[d.isoformat()] = code
            d += dt.timedelta(days=1)
    return out


def holidays_for(year, days):
    try:
        h = _yaml(f"holidays-{year}.yaml")["holidays"]
    except Exception:
        return {}
    inwin = {d.isoformat() for d in days}
    out = {}
    for x in h:
        if x["date"] in inwin:
            out[x["date"]] = {"type": x["type"], "occasion": x["occasion"]}
    return out


def epic_remaining_sp(keys):
    """Sum of story points on not-done children, per epic key."""
    keys = [k.strip() for k in keys if k.strip()]
    if not keys:
        return {}
    email, token = _secret("atlassian_email"), _secret("atlassian_token")
    import urllib.request, base64
    out = {k: 0.0 for k in keys}
    try:
        jql = f"parent in ({','.join(keys)}) AND statusCategory != Done"
        body = json.dumps({"jql": jql, "fields": ["customfield_10051", "parent"],
                           "maxResults": 200}).encode()
        req = urllib.request.Request(f"https://{JIRA_HOST}/rest/api/3/search/jql",
                                     data=body, method="POST")
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode())
        req.add_header("Content-Type", "application/json")
        data = json.load(urllib.request.urlopen(req, timeout=25))
        for i in data.get("issues", []):
            f = i["fields"]
            ep = (f.get("parent") or {}).get("key")
            if ep in out:
                out[ep] += f.get("customfield_10051") or 0
    except Exception as e:
        sys.stderr.write(f"[epic_sp] {e}\n")
        return {"__error__": str(e)}
    return {k: round(v, 1) for k, v in out.items()}


def backlog_pool(cap=300):
    """ALL candidate backlog tickets (paginated): in project, not in any sprint,
    not done, not epics/sub-tasks. Carries summary + description + created for the
    deterministic classifier. Capped at `cap` for dump size."""
    email, token = _secret("atlassian_email"), _secret("atlassian_token")
    import urllib.request, base64
    jql = (f"project = {JIRA_PROJECT} AND sprint is EMPTY AND statusCategory != Done "
           "AND issuetype not in (Epic, Sub-task) ORDER BY Rank ASC")
    out, token_page = [], None
    try:
        while len(out) < cap:
            payload = {"jql": jql, "maxResults": 100,
                       "fields": ["summary", "status", "customfield_10051", "priority",
                                  "issuetype", "parent", "assignee", "created", "description"]}
            if token_page:
                payload["nextPageToken"] = token_page
            req = urllib.request.Request(f"https://{JIRA_HOST}/rest/api/3/search/jql",
                                         data=json.dumps(payload).encode(), method="POST")
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode())
            req.add_header("Content-Type", "application/json")
            data = json.load(urllib.request.urlopen(req, timeout=30))
            for i in data.get("issues", []):
                f = i["fields"]
                desc = _adf_text(f.get("description") or {}).strip()
                out.append({"key": i["key"], "summary": (f.get("summary") or "")[:90],
                            "desc": " ".join(desc.split())[:240],
                            "sp": f.get("customfield_10051"),
                            "priority": (f.get("priority") or {}).get("name", ""),
                            "type": f["issuetype"]["name"],
                            "epic": (f.get("parent") or {}).get("key", ""),
                            "assignee": (f.get("assignee") or {}).get("displayName", ""),
                            "status": f["status"]["name"],
                            "created": f.get("created", "")})
            token_page = data.get("nextPageToken")
            if data.get("isLast") or not token_page:
                break
    except Exception as e:
        sys.stderr.write(f"[backlog] {e}\n")
    return out


# Deterministic backlog classifier — title + description + recency + type/status.
# Reproducible: same tickets + same sprint-start ref → same scores/order.
_KW = [
    (24, "incident/prod", ["production", " prod ", "prod ", "incident", "outage", "sev1", "sev2", "p0 "]),
    (22, "correctness", ["incorrect", "wrong", "mismatch", "balance", "corrupt", "duplicate", "reconcil", "data loss", "missing"]),
    (20, "security/compliance", ["security", "vulnerab", "cve", "compliance", "audit", " pii", "frm", "fraud"]),
    (18, "defect", ["exception", "nullpointer", "npe", "error", "fail", "crash", "timeout", "stuck", "hang", "bug"]),
    (10, "reliability/ops", ["retry", "atomic", "idempoten", "lock", " ha ", "failover", "alert", "monitor", "sla", "latency", "performance", "resilien", "throttl"]),
]
_CHORE = ["doc ", "sop", "cleanup", "rename", "revisit", "nice to have", "tech debt", "cosmetic", "readme"]
_INVEST = ["investigate", "explore", "spike", "poc", "analysis", "analyse", "analyze"]


def _classify_one(t, ref):
    text = (t.get("summary", "") + " " + t.get("desc", "")).lower()
    score, category, reasons = 0, "general", []
    if t.get("type") == "Bug":
        score += 22; reasons.append("type:bug +22")
    elif t.get("type") == "Story":
        score += 4
    st = (t.get("status") or "").lower()
    if any(s in st for s in ("review", "qa", "pending release", "verif")):
        score += 18; reasons.append("near-done +18")
    elif "progress" in st:
        score += 10; reasons.append("in-progress +10")
    pr = (t.get("priority") or "").lower()
    if pr in ("p1", "highest", "high"):
        score += 25; reasons.append("field-pri P1 +25")
    elif pr in ("p2", "medium"):
        score += 8
    best = 0
    for pts, cat, kws in _KW:
        if any(k in text for k in kws):
            reasons.append(cat + " +" + str(pts))
            if pts > best:
                best, category = pts, cat
    score += best
    if any(k in text for k in _CHORE):
        score -= 8; reasons.append("chore -8")
        if category == "general":
            category = "chore"
    if t.get("type") != "Bug" and any(k in text for k in _INVEST):
        score -= 5; reasons.append("uncertain-scope -5")
    sp = t.get("sp")
    if sp is not None and sp <= 2:
        score += 8; reasons.append("quick-win SP≤2 +8")
    elif sp is not None and sp > 8:
        score -= 5; reasons.append("large SP>8 -5")
    elif sp is None:
        score -= 2; reasons.append("unsized -2")
    try:
        created = dt.datetime.fromisoformat(t["created"].replace("Z", "+00:00")).date()
        age = (ref - created).days
        if age <= 30:
            score += 15; reasons.append("fresh ≤30d +15")
        elif age <= 90:
            score += 10; reasons.append("recent ≤90d +10")
        elif age <= 180:
            score += 5
        else:
            reasons.append("stale >180d +0")
    except Exception:
        pass
    return score, category, reasons


def classify_backlog(pool, ref):
    for t in pool:
        s, c, r = _classify_one(t, ref)
        t["score"], t["category"], t["reasons"] = s, c, r
    pool.sort(key=lambda t: (-t["score"], t["key"]))
    return pool


def _adf_text(node):
    """Flatten Atlassian Document Format to plain text."""
    if isinstance(node, list):
        return "".join(_adf_text(c) for c in node)
    if isinstance(node, dict):
        t = node.get("text", "")
        t += _adf_text(node.get("content", []))
        if node.get("type") in ("paragraph", "heading", "listItem"):
            t += "\n"
        return t
    return ""


def get_ticket(key):
    """Fetch a single ticket's detail for the in-page viewer."""
    key = (key or "").strip()
    if not key:
        return {"error": "no key"}
    email, token = _secret("atlassian_email"), _secret("atlassian_token")
    import urllib.request, base64
    try:
        url = (f"https://{JIRA_HOST}/rest/api/3/issue/{key}"
               "?fields=summary,status,assignee,priority,issuetype,description,customfield_10051,parent")
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode())
        d = json.load(urllib.request.urlopen(req, timeout=20))
        f = d["fields"]
        return {"key": key, "summary": f.get("summary", ""),
                "status": f["status"]["name"], "type": f["issuetype"]["name"],
                "assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
                "priority": (f.get("priority") or {}).get("name", ""),
                "sp": f.get("customfield_10051"), "epic": (f.get("parent") or {}).get("key", ""),
                "url": f"https://{JIRA_HOST}/browse/{key}",
                "description": _adf_text(f.get("description") or {}).strip()[:6000]}
    except Exception as e:
        return {"error": str(e)}


def build():
    start, active_label, sprint_id = active_sprint()
    days = [start + dt.timedelta(days=i) for i in range(14)]      # Wed -> Tue+1wk
    working = [d for d in days if d.weekday() < 5]
    year = start.year
    hol = holidays_for(year, days)
    mand_hol = {d for d, v in hol.items() if v["type"] == "holiday"}
    wd = len([d for d in working if d.isoformat() not in mand_hol])

    eff = eff_by_role()
    team = roster()
    oncall = oncall_for(working)
    email2canon = {p["email"]: p["canonical"] for p in team}
    leaves = leaves_for(days[0], days[-1])
    spill = spillover(sprint_id, email2canon)

    # invert on-call email -> canonical per date
    oncall_canon = {}
    for diso, email in oncall.items():
        c = email2canon.get(email)
        if c:
            oncall_canon[diso] = c

    day_meta = [{"date": d.isoformat(),
                 "dow": d.strftime("%a"),
                 "weekend": d.weekday() >= 5,
                 "holiday": hol.get(d.isoformat())} for d in days]

    people = []
    for p in team:
        statuses, leave_n, onc_n, wfh_n = [], 0, 0, 0
        plv = leaves.get(p["canonical"], {})
        for d in days:
            diso = d.isoformat()
            if d.weekday() >= 5:
                statuses.append("WE"); continue
            if diso in mand_hol:
                statuses.append("H"); continue
            st = plv.get(diso, "")
            if st == "L":
                statuses.append("L"); leave_n += 1; continue
            if oncall_canon.get(diso) == p["canonical"]:
                statuses.append("O"); onc_n += 1; continue
            if st == "W":
                statuses.append("W"); wfh_n += 1; continue
            statuses.append("")
        net = wd - leave_n - onc_n
        sp = round(net * eff.get(p["role"], 0), 2)
        people.append({"name": p["name"], "canonical": p["canonical"],
                       "role": p["role"], "eff": eff.get(p["role"], 0),
                       "statuses": statuses, "leave": leave_n, "oncall": onc_n,
                       "wfh": wfh_n, "net": net, "sp": sp,
                       "spillover": spill.get(p["canonical"], [])})

    model = {
        "sprint": {"label": f"Upcoming (after {active_label})",
                   "start": days[0].isoformat(), "end": working[-1].isoformat(),
                   "workingDays": wd, "cadence": "Wed-to-Wed, 2 weeks"},
        "effByRole": eff,
        "days": day_meta,
        "oncall": oncall_canon,
        "people": people,
        "teamNetDays": sum(p["net"] for p in people),
        "teamSP": round(sum(p["sp"] for p in people), 1),
        "nominalSP": round(sum(wd * eff.get(p["role"], 0) for p in people), 1),
        "backlogPool": classify_backlog(backlog_pool(), start),
    }
    return model


if __name__ == "__main__":
    m = build()
    out = os.path.join(ROOT, "derived", "capacity.json")
    with open(out, "w") as f:
        json.dump(m, f, indent=2)
    sys.stderr.write(f"wrote {out}\n")
    print(json.dumps(m, indent=2))

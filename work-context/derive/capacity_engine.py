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
OINT_PROJECT = OINT_POD = ""
# Org-specific Jira field/issuetype/link IDs — populated from config (jira_fields).
INITIATIVE_POD_FIELD = INITIATIVE_ORGPRI_FIELD = INITIATIVE_ENG_DRI_FIELD = ""
INITIATIVE_PROD_DRI_FIELD = INITIATIVE_IMPACT_FIELD = INITIATIVE_LINK_TYPE = ""
BUDGET_OVERALL_FIELD = ""
EPIC_ISSUETYPE_ID = "10000"
BUDGET_FIELDS = {}
SP_FIELD = ""


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
    global OINT_PROJECT, OINT_POD
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
    # Org-initiatives (OINT) planning: project holding org requirements + this team's
    # PODs-field value used to filter them to our pod. Both org-specific → from config.
    OINT_PROJECT = sp.get("oint_project", "")
    OINT_POD = sp.get("oint_pod", "")
    # Org-specific Jira field / issuetype / link IDs (nothing hardcoded in code).
    global INITIATIVE_POD_FIELD, INITIATIVE_ORGPRI_FIELD, INITIATIVE_ENG_DRI_FIELD
    global INITIATIVE_PROD_DRI_FIELD, INITIATIVE_IMPACT_FIELD, INITIATIVE_LINK_TYPE
    global EPIC_ISSUETYPE_ID, BUDGET_OVERALL_FIELD, BUDGET_FIELDS, SP_FIELD
    jf = sp.get("jira_fields") or {}
    SP_FIELD = jf.get("story_points", "")
    INITIATIVE_POD_FIELD = jf.get("pods", "")
    INITIATIVE_ORGPRI_FIELD = jf.get("org_priority", "")
    INITIATIVE_ENG_DRI_FIELD = jf.get("eng_dri", "")
    INITIATIVE_PROD_DRI_FIELD = jf.get("prod_dri", "")
    INITIATIVE_IMPACT_FIELD = jf.get("impact", "")
    INITIATIVE_LINK_TYPE = jf.get("initiative_link_type", "")
    EPIC_ISSUETYPE_ID = str(jf.get("epic_issuetype_id", "10000"))
    BUDGET_OVERALL_FIELD = jf.get("overall_budget", "")
    BUDGET_FIELDS = _budget_fields_from_base(jf.get("monthly_budget_base", ""))


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


def _oncall_email_on(d):
    """Single OpsGenie who's-on-call probe for date `d` at 06:30Z (noon IST, after the
    ~Wed-morning handover). OpsGenie resolves future dates from the rotation, so this
    works for months ahead. Returns the recipient email, or None (no key / miss / error)."""
    key = os.environ.get("OPSGENIE_API_KEY") or _secret("opsgenie_api_key")
    if not key:
        return None
    import urllib.request
    try:
        url = (f"https://api.opsgenie.com/v2/schedules/{OPSGENIE_SCHEDULE}/on-calls"
               f"?scheduleIdentifierType=name&flat=true&date={d.isoformat()}T06:30:00Z")
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"GenieKey {key}")
        data = json.load(urllib.request.urlopen(req, timeout=20))
        rec = data.get("data", {}).get("onCallRecipients", [])
        return rec[0] if rec else None
    except Exception as e:
        sys.stderr.write(f"[oncall {d}] {e}\n")
        return None


def oncall_for(days):
    """Per-weekday on-call map {date_iso: email} — one probe per day. Used for the
    ~10-day sprint window where per-day accuracy is cheap."""
    out = {}
    for d in days:
        if d.weekday() >= 5:
            continue
        email = _oncall_email_on(d)
        if email:
            out[d.isoformat()] = email
    return out


def oncall_by_week(days):
    """Rota-aligned on-call map {date_iso: email} for a long span. The rotation hands
    over every Wednesday, so this probes ONCE per rotation-week (that week's Wednesday)
    and fills the Wed→Tue week — ~13 calls for 3 months instead of ~66 per-day calls."""
    out, seen = {}, {}
    for d in days:
        if d.weekday() >= 5:
            continue
        wed = d - dt.timedelta(days=(d.weekday() - 2) % 7)   # this day's rotation-week Wednesday
        wiso = wed.isoformat()
        if wiso not in seen:
            seen[wiso] = _oncall_email_on(wed)
        if seen[wiso]:
            out[d.isoformat()] = seen[wiso]
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


# Month names for the per-month "<Month> Budget" epic fields (year-agnostic). The
# customfield IDs themselves are org-specific → loaded from config (jira_fields).
BUDGET_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _budget_fields_from_base(base):
    """Map Jan..Dec to consecutive customfield IDs from `base` (e.g. 'customfield_500').
    Returns {} if base is unset/malformed."""
    if not base or "_" not in str(base):
        return {}
    try:
        n = int(str(base).split("_")[1])
    except ValueError:
        return {}
    return {m: f"customfield_{n + i}" for i, m in enumerate(BUDGET_MONTHS)}


def epic_budgets(today=None):
    """Planned SP budget per calendar month, read from the epics' monthly Budget fields.

    Returns every epic that carries a budget in ANY month, with its per-month SP and a
    row total, plus per-month totals across all epics. Budgets are year-agnostic (the
    fields are just Jan..Dec), so a month maps to its field regardless of year."""
    today = today or dt.date.today()
    email, token = _secret("atlassian_email"), _secret("atlassian_token")
    import urllib.request, base64
    fields = list(BUDGET_FIELDS.values()) + [BUDGET_OVERALL_FIELD, "summary", "status"]
    issues, token_page = [], None
    try:
        while True:   # /search/jql caps a page at 100 → follow nextPageToken to get all epics
            payload = {"jql": f"project = {JIRA_PROJECT} AND issuetype = Epic",
                       "fields": fields, "maxResults": 100}
            if token_page:
                payload["nextPageToken"] = token_page
            req = urllib.request.Request(f"https://{JIRA_HOST}/rest/api/3/search/jql",
                                         data=json.dumps(payload).encode(), method="POST")
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode())
            req.add_header("Content-Type", "application/json")
            data = json.load(urllib.request.urlopen(req, timeout=40))
            issues.extend(data.get("issues", []))
            token_page = data.get("nextPageToken")
            if data.get("isLast") or not token_page:
                break
    except Exception as e:
        sys.stderr.write(f"[epic_budgets] {e}\n")
        return {"__error__": str(e)}

    epics, totals = [], {m: 0.0 for m in BUDGET_MONTHS}
    for i in issues:
        f = i["fields"]
        budgets = {m: (f.get(BUDGET_FIELDS[m]) or 0) for m in BUDGET_MONTHS}
        if not any(budgets.values()):
            continue
        for m in BUDGET_MONTHS:
            totals[m] += budgets[m]
        epics.append({
            "key": i["key"],
            "url": f"https://{JIRA_HOST}/browse/{i['key']}",
            "summary": f.get("summary", ""),
            "status": (f.get("status") or {}).get("name", ""),
            "overall": f.get(BUDGET_OVERALL_FIELD),
            "budgets": {m: round(budgets[m], 1) for m in BUDGET_MONTHS},
            "total": round(sum(budgets.values()), 1),
        })
    epics.sort(key=lambda e: -e["total"])
    return {"generated": today.isoformat(),
            "currentMonth": BUDGET_MONTHS[today.month - 1],
            "months": BUDGET_MONTHS,
            "monthTotals": {m: round(totals[m], 1) for m in BUDGET_MONTHS},
            "epics": epics}


# Fields on OINT "Initiative" issues (PODs tag, org priority, DRIs) are org-specific
# customfield IDs → loaded from config (jira_fields) in _load_cfg, not hardcoded here.


def _user_name(v):
    """First display name from a Jira user-picker field value (list or dict)."""
    if isinstance(v, list):
        v = v[0] if v else None
    return v.get("displayName", "") if isinstance(v, dict) else ""


def pod_options():
    """Distinct values of the OINT 'PODs' multi-select field, derived by scanning the
    initiatives (the field-option admin API is 403 for us). Cached by the server; falls
    back to just the configured pod on error."""
    email, token = _secret("atlassian_email"), _secret("atlassian_token")
    import urllib.request, base64
    pods, token_page, pages = set(), None, 0
    try:
        while pages < 20:   # bound the scan; the server caches the result
            payload = {"jql": f"project = {OINT_PROJECT} AND issuetype = Initiative",
                       "fields": [INITIATIVE_POD_FIELD], "maxResults": 100}
            if token_page:
                payload["nextPageToken"] = token_page
            req = urllib.request.Request(f"https://{JIRA_HOST}/rest/api/3/search/jql",
                                         data=json.dumps(payload).encode(), method="POST")
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode())
            req.add_header("Content-Type", "application/json")
            data = json.load(urllib.request.urlopen(req, timeout=40))
            for i in data.get("issues", []):
                v = i["fields"].get(INITIATIVE_POD_FIELD)
                for x in (v if isinstance(v, list) else [v]):
                    if isinstance(x, dict) and x.get("value"):
                        pods.add(x["value"])
            token_page = data.get("nextPageToken")
            pages += 1
            if data.get("isLast") or not token_page:
                break
    except Exception as e:
        sys.stderr.write(f"[pod_options] {e}\n")
    return sorted(pods) or [OINT_POD]


def pod_initiatives(pods=None):
    """Org initiatives (OINT project) for the given pod(s), each with its linked board
    epic (a JIRA_PROJECT issue on either side of an issue link) and that epic's monthly
    budget. `pods` defaults to the configured OINT_POD. Read-only planning input."""
    if not OINT_PROJECT:
        return {"__error__": "oint_project not set in sprint_planning.yaml"}
    pods = [p for p in (pods or [OINT_POD]) if p]
    if not pods:
        return {"__error__": "no pod selected"}
    email, token = _secret("atlassian_email"), _secret("atlassian_token")
    import urllib.request, base64

    def _post(payload):
        req = urllib.request.Request(f"https://{JIRA_HOST}/rest/api/3/search/jql",
                                     data=json.dumps(payload).encode(), method="POST")
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode())
        req.add_header("Content-Type", "application/json")
        return json.load(urllib.request.urlopen(req, timeout=40))

    pod_cf = INITIATIVE_POD_FIELD.split("_")[1]
    pod_list = ", ".join('"' + p.replace('"', '\\"') + '"' for p in pods)
    # Exclude terminal (Done / Cancelled) initiatives — a forward planning board only
    # cares about open, plannable work; finished cycles just add noise.
    jql = (f'project = {OINT_PROJECT} AND issuetype = Initiative '
           f'AND cf[{pod_cf}] in ({pod_list}) AND statusCategory != Done ORDER BY created DESC')
    fields = ["summary", "status", INITIATIVE_ORGPRI_FIELD, "issuelinks",
              INITIATIVE_ENG_DRI_FIELD, INITIATIVE_PROD_DRI_FIELD]
    issues, token_page = [], None
    try:
        while True:
            payload = {"jql": jql, "fields": fields, "maxResults": 100}
            if token_page:
                payload["nextPageToken"] = token_page
            data = _post(payload)
            issues.extend(data.get("issues", []))
            token_page = data.get("nextPageToken")
            if data.get("isLast") or not token_page:
                break
    except Exception as e:
        sys.stderr.write(f"[pod_initiatives] {e}\n")
        return {"__error__": str(e)}

    def _linked_epic(f):
        for l in f.get("issuelinks", []):
            o = l.get("outwardIssue") or l.get("inwardIssue") or {}
            k = o.get("key", "")
            if k.startswith(f"{JIRA_PROJECT}-"):
                return {"key": k, "url": f"https://{JIRA_HOST}/browse/{k}",
                        "summary": (o.get("fields") or {}).get("summary", "")}
        return None

    inits = []
    for i in issues:
        f = i["fields"]
        op = f.get(INITIATIVE_ORGPRI_FIELD)
        inits.append({
            "key": i["key"], "url": f"https://{JIRA_HOST}/browse/{i['key']}",
            "summary": f.get("summary", ""),
            "status": (f.get("status") or {}).get("name", ""),
            "orgPriority": op.get("value", "") if isinstance(op, dict) else "",
            "engDri": _user_name(f.get(INITIATIVE_ENG_DRI_FIELD)),
            "prodDri": _user_name(f.get(INITIATIVE_PROD_DRI_FIELD)),
            "epic": _linked_epic(f),
            "budgets": {m: 0 for m in BUDGET_MONTHS},
        })

    # Attach each linked epic's monthly budget (batch by key).
    epic_keys = sorted({x["epic"]["key"] for x in inits if x["epic"]})
    ebud = {}
    for j in range(0, len(epic_keys), 80):
        chunk = epic_keys[j:j + 80]
        try:
            data = _post({"jql": f"key in ({','.join(chunk)})",
                          "fields": list(BUDGET_FIELDS.values()), "maxResults": 100})
            for it in data.get("issues", []):
                ff = it["fields"]
                ebud[it["key"]] = {m: (ff.get(BUDGET_FIELDS[m]) or 0) for m in BUDGET_MONTHS}
        except Exception as e:
            sys.stderr.write(f"[pod_initiatives budgets] {e}\n")
    for x in inits:
        if x["epic"] and x["epic"]["key"] in ebud:
            x["budgets"] = {m: round(ebud[x["epic"]["key"]][m], 1) for m in BUDGET_MONTHS}

    return {"generated": dt.date.today().isoformat(), "pods": pods,
            "months": BUDGET_MONTHS, "initiatives": inits}


# INITIATIVE_LINK_TYPE + EPIC_ISSUETYPE_ID are org/instance-specific → loaded from
# config (jira_fields) in _load_cfg.


def _jira(method, path, body=None):
    """Authenticated Jira REST call. Returns parsed JSON ({} on empty 204 body)."""
    email, token = _secret("atlassian_email"), _secret("atlassian_token")
    import urllib.request, base64
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"https://{JIRA_HOST}{path}", data=data, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    raw = urllib.request.urlopen(req, timeout=40).read()
    return json.loads(raw) if raw else {}


def _adf(text):
    """Minimal ADF doc from plain text (Jira v3 description field)."""
    text = (text or "").strip()
    para = {"type": "paragraph", "content": ([{"type": "text", "text": text[:30000]}] if text else [])}
    return {"type": "doc", "version": 1, "content": [para]}


def find_matching_epics(summary, limit=6):
    """Board-project epics whose summary text-matches the initiative summary; exact first."""
    s = (summary or "").strip()
    if not s:
        return []
    try:
        d = _jira("POST", "/rest/api/3/search/jql",
                  {"jql": f'project = {JIRA_PROJECT} AND issuetype = Epic AND summary ~ "{s.replace(chr(34), "")}"',
                   "fields": ["summary", "status"], "maxResults": limit})
    except Exception as e:
        sys.stderr.write(f"[find_epics] {e}\n")
        return []
    out = []
    for i in d.get("issues", []):
        sm = i["fields"].get("summary", "")
        out.append({"key": i["key"], "summary": sm, "url": f"https://{JIRA_HOST}/browse/{i['key']}",
                    "status": (i["fields"].get("status") or {}).get("name", ""),
                    "exact": sm.strip().lower() == s.lower()})
    out.sort(key=lambda x: not x["exact"])
    return out


# INITIATIVE_IMPACT_FIELD ("Impact Details") is org-specific → loaded from config.


def _initiative_detail(key):
    d = _jira("GET", f"/rest/api/3/issue/{key}?fields=summary,description,issuelinks,{INITIATIVE_IMPACT_FIELD}")
    f = d["fields"]
    linked = None
    for l in f.get("issuelinks", []):
        o = l.get("outwardIssue") or l.get("inwardIssue") or {}
        if o.get("key", "").startswith(f"{JIRA_PROJECT}-"):
            linked = {"key": o["key"], "url": f"https://{JIRA_HOST}/browse/{o['key']}",
                      "summary": (o.get("fields") or {}).get("summary", "")}
    imp = f.get(INITIATIVE_IMPACT_FIELD)
    if isinstance(imp, dict):
        imp = imp.get("value", "")
    elif isinstance(imp, list):
        imp = ", ".join(x.get("value", str(x)) if isinstance(x, dict) else str(x) for x in imp)
    return {"key": key, "url": f"https://{JIRA_HOST}/browse/{key}",
            "summary": f.get("summary", ""),
            "descriptionText": _adf_text(f.get("description") or {}).strip(),
            "impact": imp or "",
            "linkedEpic": linked}


def initiative_detail(key):
    """Public: an initiative's summary + description + impact (for the expandable row)."""
    try:
        return _initiative_detail(key)
    except Exception as e:
        return {"__error__": str(e)}


def _link_initiative_epic(initiative_key, epic_key):
    """Create the Polaris link initiative←epic. Idempotent: no-op if it already exists."""
    det = _jira("GET", f"/rest/api/3/issue/{initiative_key}?fields=issuelinks")
    for l in det["fields"].get("issuelinks", []):
        o = l.get("outwardIssue") or l.get("inwardIssue") or {}
        if o.get("key") == epic_key:
            return {"already": True}
    _jira("POST", "/rest/api/3/issueLink",
          {"type": {"name": INITIATIVE_LINK_TYPE},
           "inwardIssue": {"key": initiative_key},   # 'is implemented by'
           "outwardIssue": {"key": epic_key}})        # 'implements'
    return {"already": False}


def resolve_epic(initiative_key, mode="preview", epic_key=None):
    """Link/create the board epic for an OINT initiative. Idempotent throughout:
    - already-linked initiative → returns the existing epic, writes nothing;
    - `create` re-uses an exact-summary epic if one exists (no duplicate);
    - links are de-duped before creating.
    mode: preview | auto | link (needs epic_key) | create."""
    try:
        det = _initiative_detail(initiative_key)
    except Exception as e:
        return {"__error__": f"read initiative: {e}"}
    if det["linkedEpic"]:
        return {"status": "already_linked", "epic": det["linkedEpic"], "initiativeSummary": det["summary"]}
    try:
        if mode == "preview":
            return {"status": "preview", "initiativeSummary": det["summary"],
                    "descriptionPreview": det["descriptionText"][:500],
                    "matches": find_matching_epics(det["summary"])}
        if mode == "link":
            if not epic_key:
                return {"__error__": "no epic_key"}
            r = _link_initiative_epic(initiative_key, epic_key)
            return {"status": "linked", "epic": {"key": epic_key, "url": f"https://{JIRA_HOST}/browse/{epic_key}"}, **r}
        if mode in ("create", "auto"):
            if mode == "auto":
                exact = [m for m in find_matching_epics(det["summary"]) if m["exact"]]
                if exact:
                    r = _link_initiative_epic(initiative_key, exact[0]["key"])
                    return {"status": "linked", "epic": exact[0], **r}
            ep = _jira("POST", "/rest/api/3/issue",
                       {"fields": {"project": {"key": JIRA_PROJECT},
                                   "issuetype": {"id": EPIC_ISSUETYPE_ID},
                                   "summary": det["summary"][:250],
                                   "description": _adf(det["descriptionText"])}})
            epic = {"key": ep["key"], "url": f"https://{JIRA_HOST}/browse/{ep['key']}"}
            _link_initiative_epic(initiative_key, epic["key"])
            return {"status": "created", "epic": epic}
    except Exception as e:
        return {"__error__": str(e)}
    return {"__error__": "bad mode"}


def submit_budgets(epic_budgets, months, dry_run=True):
    """Write planned SP into epics' monthly Budget fields. Idempotent: diffs against the
    current values and only PUTs changed (epic, month) cells; re-running is a no-op.
    epic_budgets = {epicKey: {MonthName: sp}}; months = MonthNames to write."""
    months = [m for m in months if m in BUDGET_MONTHS]
    keys = sorted(epic_budgets.keys())
    if not keys or not months:
        return {"dryRun": dry_run, "diffs": [], "applied": []}
    current = {}
    try:
        for j in range(0, len(keys), 80):
            chunk = keys[j:j + 80]
            d = _jira("POST", "/rest/api/3/search/jql",
                      {"jql": f"key in ({','.join(chunk)})",
                       "fields": list(BUDGET_FIELDS.values()), "maxResults": 100})
            for it in d.get("issues", []):
                current[it["key"]] = {m: (it["fields"].get(BUDGET_FIELDS[m]) or 0) for m in BUDGET_MONTHS}
    except Exception as e:
        return {"__error__": f"read current budgets: {e}"}

    diffs = []
    for k, mb in epic_budgets.items():
        for m in months:
            to = round(float(mb.get(m, 0) or 0), 1)
            frm = round(float(current.get(k, {}).get(m, 0) or 0), 1)
            if to != frm:
                diffs.append({"epic": k, "month": m, "from": frm, "to": to})
    if dry_run:
        return {"dryRun": True, "diffs": diffs}

    by_epic = {}
    for d in diffs:
        by_epic.setdefault(d["epic"], {})[d["month"]] = d["to"]
    applied = []
    for k, mb in by_epic.items():
        fields = {BUDGET_FIELDS[m]: v for m, v in mb.items()}
        try:
            _jira("PUT", f"/rest/api/3/issue/{k}", {"fields": fields})
            applied.append({"epic": k, "months": mb, "ok": True})
        except Exception as e:
            applied.append({"epic": k, "error": str(e), "ok": False})
    return {"dryRun": False, "diffs": diffs, "applied": applied}


def retro_summary(months):
    """Retro data for a set of calendar months (['YYYY-MM', ...]):
    per epic, PLANNED SP (the epic's Budget fields for those months) vs ACTUAL SP
    (story points of Done / Mobile-Release-Pending tickets resolved in the period,
    rolled to their parent epic), plus the delta. Covers epics that had either
    planned budget OR actual work in the window."""
    import datetime as dt
    yms = sorted(m for m in months if m)
    if not yms:
        return {"__error__": "no months selected"}

    def _first(ym): y, m = map(int, ym.split("-")); return dt.date(y, m, 1)
    def _last(ym):
        y, m = map(int, ym.split("-"))
        nxt = dt.date(y + 1, 1, 1) if m == 12 else dt.date(y, m + 1, 1)
        return nxt - dt.timedelta(days=1)
    start, end = _first(yms[0]).isoformat(), _last(yms[-1]).isoformat()
    mnames = [BUDGET_MONTHS[int(ym.split("-")[1]) - 1] for ym in yms]

    def _search(jql, fields):
        out, tok, pages = [], None, 0
        while True:
            body = {"jql": jql, "fields": fields, "maxResults": 100}
            if tok:
                body["nextPageToken"] = tok
            d = _jira("POST", "/rest/api/3/search/jql", body)
            out += d.get("issues", [])
            tok = d.get("nextPageToken")
            pages += 1
            if d.get("isLast") or not tok or pages > 50:
                break
        return out

    try:
        # ACTUAL: SP of resolved Done/MRP work items in the window, summed per parent epic
        if not SP_FIELD:
            return {"__error__": "jira_fields.story_points not set in sprint_planning.yaml"}
        children = _search(
            f'project = {JIRA_PROJECT} AND issuetype in (Story,Task,Bug) '
            f'AND status in (Done,"Mobile Release Pending") '
            f'AND resolved >= "{start}" AND resolved <= "{end}"',
            ["parent", SP_FIELD])
        actual = {}
        for i in children:
            f = i["fields"]
            p = (f.get("parent") or {}).get("key")
            if p:
                actual[p] = actual.get(p, 0) + (f.get(SP_FIELD) or 0)
        # PLANNED: epic monthly Budget fields (reuse epic_budgets)
        eb = epic_budgets()
        if "__error__" in eb:
            return eb
        bud = {e["key"]: e for e in eb["epics"]}
        planned = {k: round(sum(bud[k]["budgets"].get(mn, 0) for mn in mnames), 1) for k in bud}

        keys = set(actual) | {k for k, v in planned.items() if v > 0}
        summ = {e["key"]: e.get("summary", "") for e in bud.values()}
        missing = [k for k in keys if k not in summ]
        for j in range(0, len(missing), 80):
            for it in _search(f"key in ({','.join(missing[j:j+80])})", ["summary"]):
                summ[it["key"]] = it["fields"].get("summary", "")

        rows = []
        for k in keys:
            pl = round(planned.get(k, 0), 1)
            ac = round(actual.get(k, 0), 1)
            rows.append({"key": k, "url": f"https://{JIRA_HOST}/browse/{k}",
                         "summary": summ.get(k, ""), "planned": pl, "actual": ac,
                         "delta": round(pl - ac, 1)})
        rows.sort(key=lambda r: -abs(r["delta"]))
    except Exception as e:
        sys.stderr.write(f"[retro_summary] {e}\n")
        return {"__error__": str(e)}

    return {"months": yms, "monthNames": mnames, "start": start, "end": end,
            "epics": rows,
            "totalPlanned": round(sum(r["planned"] for r in rows), 1),
            "totalActual": round(sum(r["actual"] for r in rows), 1)}


def retro_notes(months):
    """Highs/Lows parsed from the routine-generated retro artifacts,
    <repo>/management/retros/<since>-to-<until>.md (numbered '## Highs' / '## Lows'),
    for the given calendar months. Picks the canonical monthly file per month (start
    and end in the same month, no variant suffix) and reads only the top-level numbered
    items (not the indented sub-bullets)."""
    import glob
    import re
    base = os.path.join(os.path.dirname(ROOT), "management", "retros")   # repo-root/management/retros
    canon = re.compile(r"^\d{4}-\d{2}-\d{2}-to-\d{4}-\d{2}-\d{2}\.md$")
    highs, lows, srcs = [], [], []
    for ym in sorted(set(m for m in months if m)):
        cands = [p for p in glob.glob(os.path.join(base, f"{ym}-01-to-{ym}-*.md"))
                 if canon.match(os.path.basename(p))]
        if not cands:
            continue
        fp = max(cands)   # latest end-date in the month = the canonical monthly retro
        srcs.append(os.path.basename(fp))
        h, l = _parse_highs_lows(open(fp, encoding="utf-8", errors="replace").read())
        highs += h
        lows += l
    return {"months": sorted(set(months)), "highs": highs, "lows": lows, "sources": srcs}


def _parse_highs_lows(text):
    """Pure parser: from a retro markdown body, return (highs, lows) — the top-level
    numbered items under '## Highs' / '## Lows' (bold markers stripped; sub-bullets and
    other sections ignored)."""
    import re
    highs, lows, sec = [], [], None
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^##\s+highs", s, re.I):
            sec = "h"; continue
        if re.match(r"^##\s+lows", s, re.I):
            sec = "l"; continue
        if s.startswith("## "):
            sec = None; continue
        m = re.match(r"^\d+[.)]\s+(.*)", line)   # top-level numbered item only (not sub-bullets)
        if sec and m:
            item = m.group(1).replace("**", "").strip()
            if item:
                (highs if sec == "h" else lows).append(item)
    return highs, lows


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


def _month_bounds(y, m):
    """(first_day, last_day) of calendar month m/y."""
    first = dt.date(y, m, 1)
    nxt = dt.date(y + 1, 1, 1) if m == 12 else dt.date(y, m + 1, 1)
    return first, nxt - dt.timedelta(days=1)


def month_capacity(year, month):
    """Sprint-style capacity for ONE full calendar month (past or future).

    Same per-person model as build() — net working days (minus leaves / mandatory
    holidays / on-call) and effective SP (net × role efficiency) — summarised for the
    month with no daily grid. On-call uses the rota-aligned weekly probe (oncall_by_week)
    so it's ~4-5 OpsGenie calls per month, not one per working day. OpsGenie resolves
    both past and future dates, so historical months compute the same way."""
    eff = eff_by_role()
    team = roster()
    email2canon = {p["email"]: p["canonical"] for p in team}

    first, last = _month_bounds(year, month)
    days = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]
    working = [d for d in days if d.weekday() < 5]
    hol = holidays_for(first.year, days)
    mand_hol = {d for d, v in hol.items() if v["type"] == "holiday"}
    wd = len([d for d in working if d.isoformat() not in mand_hol])

    oncall = oncall_by_week(working)
    oncall_canon = {}
    for diso, email in oncall.items():
        c = email2canon.get(email)
        if c:
            oncall_canon[diso] = c
    leaves = leaves_for(first, last)

    people = []
    for p in team:
        plv = leaves.get(p["canonical"], {})
        leave_n = onc_n = 0
        for d in working:
            diso = d.isoformat()
            if diso in mand_hol:
                continue
            if plv.get(diso) == "L":
                leave_n += 1
            elif oncall_canon.get(diso) == p["canonical"]:
                onc_n += 1
        net = wd - leave_n - onc_n
        sp = round(net * eff.get(p["role"], 0), 2)
        people.append({"name": p["name"], "canonical": p["canonical"],
                       "role": p["role"], "eff": eff.get(p["role"], 0),
                       "net": net, "sp": sp, "leave": leave_n, "oncall": onc_n})

    team_sp = round(sum(p["sp"] for p in people), 1)
    nominal_sp = round(sum(wd * eff.get(p["role"], 0) for p in people), 1)
    return {
        "key": f"{first.year}-{first.month:02d}",
        "label": first.strftime("%B %Y"),
        "start": first.isoformat(), "end": last.isoformat(),
        "workingDays": wd,
        "people": people,
        "teamNetDays": sum(p["net"] for p in people),
        "teamSP": team_sp,
        "nominalSP": nominal_sp,
        "utilisation": round(team_sp / nominal_sp * 100) if nominal_sp else 0,
    }


def build_monthly(n=3, today=None):
    """Convenience: `n` calendar months of capacity starting with the current month."""
    today = today or dt.date.today()
    eff = eff_by_role()
    months, y, m = [], today.year, today.month
    for _ in range(n):
        months.append(month_capacity(y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return {"generated": today.isoformat(), "effByRole": eff, "months": months}


_load_cfg()   # populate org-specific globals now that all helpers are defined


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "sprint"
    if which == "monthly":
        m = build_monthly()
        out = os.path.join(ROOT, "derived", "monthly.json")
    else:
        m = build()
        out = os.path.join(ROOT, "derived", "capacity.json")
    with open(out, "w") as f:
        json.dump(m, f, indent=2)
    sys.stderr.write(f"wrote {out}\n")
    print(json.dumps(m, indent=2))

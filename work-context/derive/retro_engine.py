"""Sprint retro: initiative -> epic planned-vs-actual, plus per-developer productivity.

Built on capacity_engine's primitives (roster, month_capacity, Jira search) rather than
re-deriving any of them. Three things here are easy to get wrong and are therefore spelled
out in code rather than left to the caller:

ATTRIBUTION. A ticket carries a *history* of sprints, so `sprint in (A,B)` counts a
carryover once per sprint it ever sat in. Summing adjacent sprints that way read 175.9 SP
where the deduped figure was 123.9. Every ticket is attributed to the LAST sprint in its
history — the one it was finished in — so each is counted exactly once.

`resolution_sprint_end` IS NOT A RESOLUTION DATE. It holds a sprint's end date, and it is
written when the ticket resolves, from whichever sprint was open then. A sprint left open
past its end date therefore stamps everything that follows with the previous sprint's date:
when S6 ran 15 days over, 25 tickets resolved in October were stamped 2026-09-22 and S7's
own end date never appeared at all. The org's published pivots bucket on this field, so it
is computed here for RECONCILIATION only and never as the primary number.

CAPACITY ALREADY NETS OUT ON-CALL. `month_capacity()` computes each person's days as
`working - leave - oncall`, so the denominator is on-call-adjusted before we touch it.
What is NOT handled there is the other side: SP delivered on on-call tickets is real work
that was never planned, so counting it in the numerator inflates efficiency against a
denominator that already removed the time. On-call SP is split out and reported beside the
number, never inside it.
"""
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capacity_engine as ce

SCHEMA = 3

# A ticket is on-call work when its SUMMARY says so — this team names them "Oncall" rather
# than labelling them, and no label or component is set on any of them. Configurable
# because the convention is the team's, not the tool's.
DEFAULT_ONCALL_RE = r"on[\s_\-]?call"

# Org-specific Jira fields this module needs on top of capacity_engine's set. Both are
# read from config so nothing is hardcoded; the defaults match this org's instance.
SPRINT_FIELD = "customfield_10010"   # sprint HISTORY array, not a scalar
RSE_FIELD = "customfield_11438"      # resolution_sprint_end; reconciliation only


def _fields():
    jf = (_cfg().get("jira_fields") or {})
    return (jf.get("sprint") or SPRINT_FIELD,
            jf.get("resolution_sprint_end") or RSE_FIELD)


def _cfg():
    return ce._optional_yaml("sprint_planning.yaml") or {}


def oncall_re():
    return re.compile(_cfg().get("oncall_summary_regex") or DEFAULT_ONCALL_RE, re.I)


def _search(jql, fields, cap_pages=120):
    out, tok, pages = [], None, 0
    while True:
        body = {"jql": jql, "fields": fields, "maxResults": 100}
        if tok:
            body["nextPageToken"] = tok
        d = ce._jira("POST", "/rest/api/3/search/jql", body)
        out += d.get("issues", [])
        tok = d.get("nextPageToken")
        pages += 1
        if d.get("isLast") or not tok or pages > cap_pages:
            break
    return out


# ----------------------------------------------------------------- pickers

# The retro picker wants sprints with something in them. A push wants the one it is
# about to write into, which is usually the one that has not started yet.
PICKER_STATES = ("closed", "active")
ALL_STATES = ("closed", "active", "future")


def board_sprints(limit=24, states=PICKER_STATES):
    """Sprints for the picker, newest first.

    Closed and active by default: a future sprint has nothing resolved in it and would
    just be an empty row. Pass ALL_STATES when you need the sprint that is being planned
    rather than the ones being reported on, or a push will decide the sprint does not
    exist and create a second one beside it.
    """
    out, start = [], 0
    while True:
        d = ce._jira("GET", f"/rest/agile/1.0/board/{ce._board_id()}"
                            f"/sprint?startAt={start}&maxResults=50", None)
        vals = d.get("values") or []
        out += vals
        if d.get("isLast") or not vals:
            break
        start += 50
    rows = [{"id": s["id"], "name": s.get("name", ""),
             "start": (s.get("startDate") or "")[:10],
             "end": (s.get("endDate") or "")[:10],
             "state": s.get("state", "")}
            for s in out if s.get("endDate") and s.get("state") in tuple(states)]
    rows.sort(key=lambda r: r["end"], reverse=True)
    return rows[:limit]


def jira_projects():
    """Projects the retro can be filtered to. Keyed off the issues the board actually
    sees plus the configured project, so the list is short and relevant."""
    try:
        d = ce._jira("GET", "/rest/api/3/project/search?maxResults=100", None)
        rows = [{"key": p["key"], "name": p.get("name", "")} for p in d.get("values", [])]
        rows.sort(key=lambda r: r["key"])
        return rows
    except Exception as e:
        return [{"key": ce.JIRA_PROJECT, "name": ce.JIRA_PROJECT, "__warn__": str(e)}]


# ----------------------------------------------------------------- core

def _attribute(issues, sprint_end, SPRINT):
    """{issue -> last sprint id in its history}. Carryovers land in the sprint they were
    finished in, so each ticket is counted once however many sprints it passed through."""
    out = {}
    for i in issues:
        ss = i["fields"].get(SPRINT) or []
        if not ss:
            continue
        last = max(ss, key=lambda s: sprint_end.get(s.get("id"), "") or "")
        out[i["key"]] = last.get("id")
    return out


def sprint_retro(sprint_ids, projects=None, window=None):
    """Planned-vs-actual by initiative/epic, and per-developer productivity, for a set of
    sprints.

    `window` overrides the capacity window; by default it is the union of the selected
    sprints' own date ranges, which is what "capacity for these sprints" means.
    """
    if not sprint_ids:
        return {"__error__": "no sprints selected"}
    sprint_ids = [int(s) for s in sprint_ids]
    projects = [p.strip() for p in (projects or [ce.JIRA_PROJECT]) if p and p.strip()]
    if not projects:
        return {"__error__": "no projects selected"}

    meta = {s["id"]: s for s in board_sprints(limit=200)}
    unknown = [s for s in sprint_ids if s not in meta]
    if unknown:
        return {"__error__": f"unknown sprint id(s): {unknown}"}
    send = {sid: m["end"] for sid, m in meta.items()}
    sel = [meta[s] for s in sprint_ids]
    w_start = min(s["start"] for s in sel if s["start"])
    w_end = max(s["end"] for s in sel if s["end"])
    if window:
        w_start, w_end = window

    projq = ",".join(projects)
    sq = ",".join(str(s) for s in sprint_ids)
    rx = oncall_re()
    SPRINT, RSE = _fields()
    SP = ce.SP_FIELD
    flds = ["summary", SP, "parent", "assignee", "status", "resolution", "resolutiondate",
            "issuetype", SPRINT, RSE_FIELD, "project"]

    try:
        raw = _search(f"project in ({projq}) and sprint in ({sq}) "
                      f"and issuetype in (Story,Task,Bug)", flds)
    except Exception as e:
        return {"__error__": str(e)}

    attr = _attribute(raw, send, SPRINT)
    keep = {k for k, sid in attr.items() if sid in set(sprint_ids)}

    rows, bad, carried = [], [], []
    for i in raw:
        f = i["fields"]
        if i["key"] not in keep:
            # In one of the selected sprints, but FINISHED somewhere else — carried forward,
            # or still open. This is the "the work was done but the ticket didn't close, so
            # the bandwidth doesn't show up" case, and it is the single biggest reason a
            # sprint reads under capacity. Counted nowhere in delivery; reported on its own.
            a = (f.get("assignee") or {}) or {}
            res = ((f.get("resolution") or {}) or {}).get("name") or ""
            carried.append({
                "key": i["key"], "summary": f.get("summary") or "",
                "sp": float(f.get(SP) or 0),
                "assignee": a.get("displayName") or "",
                "accountId": a.get("accountId") or "",
                "status": ((f.get("status") or {}) or {}).get("name") or "",
                "epic": (f.get("parent") or {}).get("key") or "",
                "landedIn": attr.get(i["key"]),
                "resolved": (f.get("resolutiondate") or "")[:10],
                "stillOpen": not res})
            continue
        res = ((f.get("resolution") or {}) or {}).get("name") or ""
        st = ((f.get("status") or {}) or {}).get("name") or ""
        a = (f.get("assignee") or {}) or {}
        r = {"key": i["key"], "summary": f.get("summary") or "",
             "sp": float(f.get(SP) or 0), "estimated": f.get(SP) is not None,
             "epic": (f.get("parent") or {}).get("key") or "",
             "assignee": a.get("displayName") or "", "accountId": a.get("accountId") or "",
             "status": st, "resolution": res,
             "resolved": (f.get("resolutiondate") or "")[:10],
             "issuetype": ((f.get("issuetype") or {}) or {}).get("name") or "",
             "project": ((f.get("project") or {}) or {}).get("key") or "",
             "sprint": attr[i["key"]],
             "rse": (f.get(RSE) or "")[:10],
             "oncall": bool(rx.search(f.get("summary") or "")),
             "done": res == "Done",
             "cancelled": st == "Cancelled" or res == "Won't Do"}
        rows.append(r)
        if r["done"] and not r["estimated"]:
            bad.append(r["key"])

    months = _window_month_days(w_start, w_end)
    tree = _epic_tree(rows, carried, months, projects)
    people = _people(rows, carried, w_start, w_end)
    return {"v": SCHEMA, "generated": dt.date.today().isoformat(),
            "sprints": sel, "projects": projects,
            "window": {"start": w_start, "end": w_end},
            "months": months, "tickets": rows, "carried": carried,
            "unestimated": bad, "tree": tree, "people": people,
            "totals": _totals(rows, carried, people)}


def _window_month_days(start, end):
    """Budget months the window touches, with how many window days fall in each.

    A sprint window straddles month boundaries (9 Sep -> 7 Oct is Sep and Oct), and the
    monthly Budget fields are per calendar month. Rather than pick one and hide the choice,
    every touched month is returned with its day count; `primary` is the month holding most
    of the window and is what the headline delta uses."""
    s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    cnt = {}
    d = s
    while d <= e:
        cnt[f"{d.year:04d}-{d.month:02d}"] = cnt.get(f"{d.year:04d}-{d.month:02d}", 0) + 1
        d += dt.timedelta(days=1)
    rows = [{"ym": k, "name": ce.BUDGET_MONTHS[int(k[5:]) - 1], "days": v,
             "field": ce.BUDGET_FIELDS.get(ce.BUDGET_MONTHS[int(k[5:]) - 1], "")}
            for k, v in sorted(cnt.items())]
    best = max(rows, key=lambda r: r["days"]) if rows else None
    for r in rows:
        r["primary"] = bool(best and r["ym"] == best["ym"])
    return rows


def _epic_tree(rows, carried, months, projects):
    """initiative -> epic -> {planned budget per month, delivered, on-call, carried}.

    Only epics that actually appear in the window are fetched; budgets and the initiative
    link come from the epic itself, so an epic whose initiative never declared our pod is
    still placed correctly."""
    keys = set(r["epic"] for r in rows + carried if r["epic"])
    bfields = [m["field"] for m in months if m["field"]]
    # An epic that was BUDGETED for the window but produced no ticket has no row otherwise,
    # which hides exactly the rows a retro exists to find: in September that was 10 epics
    # carrying 61 SP of budget with nothing delivered against them. Pull them in explicitly.
    prim = [m for m in months if m["primary"] and m["field"]]
    if prim:
        n = prim[0]["field"].split("_")[1]
        try:
            for it in _search(f"project in ({','.join(projects)}) and issuetype = Epic "
                              f"and cf[{n}] is not EMPTY", ["summary"]):
                keys.add(it["key"])
        except Exception:
            pass
    keys = sorted(keys)
    if not keys:
        return []
    want = ["summary", "status", "issuelinks", "project"] + bfields
    meta = {}
    for j in range(0, len(keys), 80):
        for it in _search(f"key in ({','.join(keys[j:j+80])})", want):
            meta[it["key"]] = it["fields"]

    def initiative(f):
        for l in (f.get("issuelinks") or []):
            o = l.get("outwardIssue") or l.get("inwardIssue") or {}
            k = o.get("key", "")
            if ce.OINT_PROJECT and k.startswith(ce.OINT_PROJECT + "-"):
                return k, (o.get("fields") or {}).get("summary") or ""
        return "", ""

    epics = {}
    for k in keys:
        f = meta.get(k, {})
        ik, isum = initiative(f)
        epics[k] = {
            "key": k, "summary": f.get("summary") or "",
            "status": ((f.get("status") or {}) or {}).get("name") or "",
            "project": ((f.get("project") or {}) or {}).get("key") or "",
            "initiative": ik, "initiativeSummary": isum,
            "planned": {m["ym"]: (f.get(m["field"]) or 0) for m in months if m["field"]},
            "delivered": 0.0, "oncall": 0.0, "cancelled": 0.0, "carried": 0.0,
            "tickets": []}
    for r in rows:
        e = epics.get(r["epic"])
        if not e:
            continue
        if r["cancelled"]:
            e["cancelled"] += r["sp"]
        elif r["done"]:
            e["oncall" if r["oncall"] else "delivered"] += r["sp"]
        e["tickets"].append(r["key"])
    for r in carried:
        e = epics.get(r["epic"])
        if e:
            e["carried"] += r["sp"]

    by_init = {}
    for e in epics.values():
        for kk in ("delivered", "oncall", "cancelled", "carried"):
            e[kk] = round(e[kk], 2)
        # A sprint window straddles months (9 Sep -> 7 Oct is 22 days Sep, 7 days Oct) but
        # the Budget fields are per calendar month. Summing every touched month charges a
        # 4-week window with 8 weeks of budget: IDEA-1 read 26.0 planned (Sep 8 + Oct 18)
        # against a window that was 76% September. The headline uses the PRIMARY month only;
        # every touched month stays in `planned` so the page can show the columns and the
        # reader can see the choice rather than inherit it.
        e["plannedTotal"] = round(sum(e["planned"].get(m["ym"], 0)
                                      for m in months if m["primary"]), 2)
        e["plannedAll"] = round(sum(e["planned"].values()), 2)
        e["delta"] = round(e["delivered"] - e["plannedTotal"], 2)
        grp = by_init.setdefault(e["initiative"] or "(no initiative)", {
            "initiative": e["initiative"], "summary": e["initiativeSummary"],
            "epics": [], "planned": 0.0, "plannedAll": 0.0, "delivered": 0.0,
            "oncall": 0.0, "cancelled": 0.0, "carried": 0.0})
        grp["epics"].append(e)
        grp["planned"] += e["plannedTotal"]
        grp["plannedAll"] = round(grp.get("plannedAll", 0) + e["plannedAll"], 2)
        for kk in ("delivered", "oncall", "cancelled", "carried"):
            grp[kk] += e[kk]
    out = []
    for g in by_init.values():
        for kk in ("planned", "delivered", "oncall", "cancelled", "carried"):
            g[kk] = round(g[kk], 2)
        g["delta"] = round(g["delivered"] - g["planned"], 2)
        g["epics"].sort(key=lambda e: -abs(e["delta"]))
        out.append(g)
    out.sort(key=lambda g: (not g["initiative"], -abs(g["delta"])))
    return out


def tier_bands():
    """Per-level SP-per-day expectation as a RANGE, not the midpoint.

    capacity_engine.eff_by_role() averages each tier's low/high into one number, which is
    right for planning a month but wrong for judging a person: it turns "within what this
    level is expected to produce" into a single pass/fail line. The bands are deliberately
    LOWER for senior tiers — an SDE3 is expected to spend about half their time on design,
    review and unblocking, so direct ticket throughput is meant to be low."""
    try:
        tiers = ce._yaml("tier_expectations.yaml")["tiers"]
    except Exception:
        return {}
    return {t: {"low": v.get("sp_efficiency_low"), "high": v.get("sp_efficiency_high"),
                "mid": round((v.get("sp_efficiency_low", 0)
                              + v.get("sp_efficiency_high", 0)) / 2, 2),
                "notes": v.get("notes", "")}
            for t, v in tiers.items()}


def _people(rows, carried, start, end):
    """Per-developer delivery against capacity for the window.

    The denominator is capacity_engine's own per-person effective SP, which already removes
    leave, mandatory holidays and on-call DAYS. The numerator therefore excludes on-call
    TICKETS — counting work whose time was already removed from the denominator would
    inflate efficiency on exactly the people who carried the rota."""
    y, m = int(start[:4]), int(start[5:7])
    cap = ce.month_capacity(y, m, window=(dt.date.fromisoformat(start),
                                          dt.date.fromisoformat(end)))
    if "__error__" in cap:
        return {"__error__": cap["__error__"]}
    # Match on Jira accountId, not display name. "Dev Two" in the roster is
    # "K Dev Two" in Jira, which split one person into two rows — a real capacity row
    # with no delivery, and a delivery row with no capacity. accountId does not drift.
    acct = {}
    try:
        for p in (ce._yaml("people.yaml")["people"]):
            if p.get("jira_id"):
                acct[str(p["jira_id"])] = p.get("canonical") or p.get("name")
    except Exception:
        pass
    by_canon = {}

    per = {}
    for p in (cap.get("people") or []):
        if p.get("canonical"):
            by_canon[p["canonical"]] = p["name"]
        per[p["name"]] = {
            "name": p["name"], "canonical": p.get("canonical", ""), "role": p.get("role", ""),
            "capacitySP": round(p.get("sp") or 0, 2), "netDays": p.get("net") or 0,
            "effFactor": p.get("eff"),
            "leave": p.get("leave") or 0, "oncallDays": p.get("oncall") or 0,
            "wfh": p.get("wfh") or 0,
            "delivered": 0.0, "oncallSP": 0.0, "cancelled": 0.0, "carried": 0.0,
            "tickets": 0, "unestimated": 0}
    def resolve(r):
        """Roster row for a ticket's assignee: accountId first, display name only as a
        fallback for people the roster has no Jira id for."""
        canon = acct.get(r.get("accountId") or "")
        if canon and by_canon.get(canon) in per:
            return by_canon[canon]
        return r.get("assignee") or "(unassigned)"

    unknown = {}
    for r in rows:
        who = resolve(r)
        t = per.get(who) or unknown.setdefault(who, {
            "name": who, "canonical": "", "role": "", "capacitySP": 0, "netDays": 0,
            "leave": 0, "oncallDays": 0, "wfh": 0, "delivered": 0.0, "oncallSP": 0.0,
            "cancelled": 0.0, "carried": 0.0, "tickets": 0, "unestimated": 0,
            "offRoster": True})
        t["tickets"] += 1
        if r["cancelled"]:
            t["cancelled"] += r["sp"]
        elif r["done"]:
            t["oncallSP" if r["oncall"] else "delivered"] += r["sp"]
            if not r["estimated"]:
                t["unestimated"] += 1
    for r in carried:
        who = resolve(r)
        t = per.get(who) or unknown.get(who)
        if t:
            t["carried"] += r["sp"]
    bands = tier_bands()
    out = list(per.values()) + list(unknown.values())
    for t in out:
        # Judge delivery against the BAND this level is held to, not against one number.
        # A single percentage silently compares an SDE1 (0.85 SP/day expected) with an SDE3
        # (0.55), whose lower figure is the design and review time the tier assumes.
        b = bands.get(t.get("role") or "")
        nd = t.get("netDays") or 0
        if b and nd and b.get("low") and b.get("high"):
            t["band"] = {"role": t["role"], "low": round(nd * b["low"], 2),
                         "high": round(nd * b["high"], 2),
                         "perDayLow": b["low"], "perDayHigh": b["high"],
                         "notes": b.get("notes", "")}
        else:
            t["band"] = None
        for kk in ("delivered", "oncallSP", "cancelled", "carried"):
            t[kk] = round(t[kk], 2)
        c = t["capacitySP"]
        # Efficiency is delivery against the time that was actually available. It is a
        # conversation starter, not a rating: an unestimated ticket scores zero and a
        # carried one scores zero even though both consumed real days.
        t["efficiency"] = round(t["delivered"] / c * 100, 1) if c else None
        t["accounted"] = round(t["delivered"] + t["oncallSP"] + t["carried"], 2)
        t["gap"] = round(c - t["accounted"], 2) if c else None
        bb = t.get("band")
        t["vsLevel"] = (None if not bb else
                        "above" if t["delivered"] > bb["high"] else
                        "within" if t["delivered"] >= bb["low"] else "below")
    out.sort(key=lambda t: (t.get("efficiency") is None, -(t.get("efficiency") or 0)))
    return out


def _totals(rows, carried, people):
    done = [r for r in rows if r["done"]]
    onc = [r for r in done if r["oncall"]]
    cap = sum(p["capacitySP"] for p in people if isinstance(people, list)
              and not p.get("offRoster")) if isinstance(people, list) else 0
    deliv = round(sum(r["sp"] for r in done if not r["oncall"]), 2)
    return {"capacitySP": round(cap, 2),
            "deliveredSP": deliv,
            "oncallSP": round(sum(r["sp"] for r in onc), 2),
            "cancelledSP": round(sum(r["sp"] for r in rows if r["cancelled"]), 2),
            "carriedSP": round(sum(r["sp"] for r in carried), 2),
            "doneTickets": len(done), "carriedTickets": len(carried),
            "efficiency": round(deliv / cap * 100, 1) if cap else None}



# ----------------------------------------------------------------- insights

def _ident():
    """canonical -> {github aliases, slack id} from people.yaml, for cross-source lookups."""
    out = {}
    try:
        for p in ce._yaml("people.yaml")["people"]:
            c = p.get("canonical")
            if not c:
                continue
            out[c] = {"github": [a for a in (p.get("github_aliases") or []) if a],
                      "slack": p.get("slack_id") or ""}
    except Exception:
        pass
    return out


def sprint_insights(doc, db=None):
    """Why delivery deviated from capacity, per developer.

    Two layers. The BRIDGE is arithmetic in story points — capacity minus the work that is
    accounted for (delivered, on-call, carried, cancelled) leaves a residual that nothing in
    Jira explains. The SIGNALS are context for that residual, drawn from the Synapse event
    store: review burden and PR latency (real engineering time that never becomes a story
    point on your own ticket) and message volume (an interrupt proxy).

    Deliberately NOT included: meeting load. The meeting source carries exactly one actor,
    the owner's own calendar, so a per-developer meeting figure would be fabricated. The
    team-wide owner figure is returned separately and labelled.
    """
    people = doc.get("people")
    if not isinstance(people, list):
        return doc
    w = doc.get("window") or {}
    start, end = w.get("start"), w.get("end")
    path = db or os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "index", "events.db")
    ident = _ident()
    notes = []

    for t in people:
        cap = t.get("capacitySP") or 0
        acc = (t.get("delivered") or 0) + (t.get("oncallSP") or 0) \
            + (t.get("carried") or 0) + (t.get("cancelled") or 0)
        t["bridge"] = {
            "capacity": round(cap, 2),
            "delivered": round(t.get("delivered") or 0, 2),
            "oncall": round(t.get("oncallSP") or 0, 2),
            "carried": round(t.get("carried") or 0, 2),
            "cancelled": round(t.get("cancelled") or 0, 2),
            "unexplained": round(cap - acc, 2) if cap else None}
        t["signals"] = {}

    if not os.path.exists(path):
        notes.append("events.db not found — only the Jira-side bridge is available.")
        doc["insights"] = {"notes": notes}
        return doc

    import sqlite3
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for t in people:
            canon = t.get("canonical")
            ids = ident.get(canon) or {}
            gh, sl = ids.get("github") or [], ids.get("slack") or ""
            s = t["signals"]
            if gh:
                gh = [g.lower() for g in gh]
                ph = ",".join("?" * len(gh))
                q = (f"select event_type, count(*) from events where source='github' "
                     f"and lower(actor) in ({ph}) and ts between ? and ? group by 1")
                got = dict(c.execute(q, (*gh, start, end + "T23:59:59")).fetchall())
                s["prsOpened"] = got.get("pr_opened", 0)
                s["prsMerged"] = got.get("pr_merged", 0)
                # Reviews and comments are time spent on OTHER people's work. It never
                # becomes a story point on your own ticket, so it is invisible in delivery
                # and is one of the commonest reasons a strong dev reads as under capacity.
                s["reviewsGiven"] = got.get("review", 0)
                s["reviewCommentsGiven"] = got.get("comment", 0)
            else:
                s["githubUnmapped"] = True
            if sl:
                n = c.execute("select count(*) from events where source='slack' and actor=? "
                              "and ts between ? and ?",
                              (sl, start, end + "T23:59:59")).fetchone()[0]
                s["slackMessages"] = n
            else:
                s["slackUnmapped"] = True
        unmapped = _suggest_github(c, people, ident, start, end)
        owner_meetings = c.execute(
            "select count(*) from events where source='meeting' and ts between ? and ?",
            (start, end + "T23:59:59")).fetchone()[0]
        c.close()
    except Exception as e:
        notes.append(f"event store unavailable: {e}")
        doc["insights"] = {"notes": notes}
        return doc

    notes.append("Meeting load is not per-developer: the meeting source carries only the "
                 "owner's calendar, so no per-person figure is shown.")
    notes.append("Reviews and review comments are work done on other people's tickets. "
                 "They consume real days and never appear as delivered story points.")
    if unmapped:
        notes.append(f"{len(unmapped)} developer(s) have no usable github_aliases in "
                     "people.yaml, so their review burden is blank. Suggested handles are "
                     "listed under insights.unmapped — confirm before adding; a wrong "
                     "alias would credit one person's work to another.")
    doc["insights"] = {"notes": notes, "ownerMeetingEvents": owner_meetings,
                       "unmapped": unmapped, "window": {"start": start, "end": end}}
    return doc


def _suggest_github(conn, people, ident, start, end):
    """Propose a github handle for each roster dev the config cannot map.

    Proposals only. Attribution in a view that compares developers has to be exact, so a
    guessed handle is never folded into anyone's numbers — it is returned for a human to
    confirm into people.yaml."""
    actors = [r[0] for r in conn.execute(
        "select actor, count(*) from events where source='github' "
        "and ts between ? and ? group by 1 order by 2 desc",
        (start, end + "T23:59:59")).fetchall()
        if not r[0].endswith("[bot]")]
    out = []
    for t in people:
        sg = t.get("signals", {})
        # Also propose for a dev whose CONFIGURED alias matched nothing. A stale handle is
        # worse than a missing one: it renders as "did no reviews" rather than as a gap.
        mapped_but_silent = (not sg.get("githubUnmapped")
                             and not any(sg.get(k) for k in
                                         ("prsOpened", "prsMerged", "reviewsGiven",
                                          "reviewCommentsGiven")))
        if not sg.get("githubUnmapped") and not mapped_but_silent:
            continue
        if mapped_but_silent:
            sg["githubAliasMatchedNothing"] = True
        canon = (t.get("canonical") or "").lower()
        parts = [x for x in re.split(r"[^a-z0-9]+", canon) if len(x) > 2]
        hits = [a for a in actors
                if any(x in a.lower().replace("-", "").replace("_", "") for x in parts)]
        out.append({"name": t.get("name"), "canonical": t.get("canonical"),
                    "configured": (ident.get(t.get("canonical")) or {}).get("github") or [],
                    "reason": "alias matched nothing" if mapped_but_silent
                              else "no github_aliases configured",
                    "candidates": hits[:4]})
    return out


# ----------------------------------------------------------------- mid-sprint check

def _working_days(a, b):
    """Working days in [a, b] inclusive, minus mandatory holidays."""
    days = [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]
    hol = ce.holidays_for(a.year, days)
    mand = {d for d, v in hol.items() if v.get("type") == "holiday"}
    return [d for d in days if d.weekday() < 5 and d.isoformat() not in mand]


def sprint_check(sprint_ids, projects=None, stale_days=3, today=None):
    """Mid-sprint review: is the board honest, and if so, is the sprint landing?

    Different from sprint_retro() in three ways that matter while a sprint is still open:

    BOARD STATE, NOT HISTORY. Tickets are taken as they sit on the board NOW, not attributed
    to the last sprint of their history. Mid-flight there is no "finished elsewhere" yet.

    CAPACITY IS PRO-RATED. Delivery so far is compared against the working days ELAPSED, not
    the whole sprint, so everyone does not read "behind" until the final day. The full-sprint
    figure and a projection at current pace are returned alongside.

    HYGIENE GATES THE NUMBER. A stale board makes productivity unmeasurable, and a low number
    produced by bad bookkeeping reads as underperformance in a 1:1 — which is a hard thing to
    walk back. Where confidence is low the caller is told to withhold the figure and show the
    specific gaps instead.
    """
    if not sprint_ids:
        return {"__error__": "no sprints selected"}
    sprint_ids = [int(s) for s in sprint_ids]
    projects = [p.strip() for p in (projects or [ce.JIRA_PROJECT]) if p and p.strip()]
    meta = {s["id"]: s for s in board_sprints(limit=200)}
    unknown = [s for s in sprint_ids if s not in meta]
    if unknown:
        return {"__error__": f"unknown sprint id(s): {unknown}"}
    sel = [meta[s] for s in sprint_ids]
    start = dt.date.fromisoformat(min(s["start"] for s in sel if s["start"]))
    end = dt.date.fromisoformat(max(s["end"] for s in sel if s["end"]))
    today = today or dt.date.today()
    asof = min(today, end)

    allw = _working_days(start, end)
    elapsed = [d for d in allw if d <= asof]
    frac = (len(elapsed) / len(allw)) if allw else 0

    SPRINT, RSE = _fields()
    SP = ce.SP_FIELD
    rx = oncall_re()
    flds = ["summary", SP, "parent", "assignee", "status", "resolution", "resolutiondate",
            "issuetype", "created", "updated", "statuscategorychangedate", SPRINT]
    try:
        raw = _search(f"project in ({','.join(projects)}) and sprint in "
                      f"({','.join(str(s) for s in sprint_ids)}) "
                      f"and issuetype in (Story,Task,Bug)", flds)
    except Exception as e:
        return {"__error__": str(e)}

    # Only tickets the board still holds: a carryover whose LAST sprint is a later one has
    # already moved on and is not this sprint's business.
    send = {sid: m["end"] for sid, m in meta.items()}
    attr = _attribute(raw, send, SPRINT)
    keep = {k for k, sid in attr.items() if sid in set(sprint_ids)}

    stale_cut = (elapsed[-stale_days] if len(elapsed) >= stale_days
                 else (elapsed[0] if elapsed else asof))
    tickets = []
    for i in raw:
        if i["key"] not in keep:
            continue
        f = i["fields"]
        a = (f.get("assignee") or {}) or {}
        st = ((f.get("status") or {}) or {}).get("name") or ""
        res = ((f.get("resolution") or {}) or {}).get("name") or ""
        moved = (f.get("statuscategorychangedate") or "")[:10]
        rd = (f.get("resolutiondate") or "")[:10]
        # "Done" means done BY the as-of date. Without this a ticket closed on day 9 counts
        # as delivered when you ask about day 3, which silently flatters every historical
        # read and makes the projection nonsense.
        done = res == "Done" and (not rd or rd <= asof.isoformat())
        cancelled = st == "Cancelled" or res == "Won't Do"
        tickets.append({
            "resolved": rd,
            "key": i["key"], "summary": f.get("summary") or "", "sp": float(f.get(SP) or 0),
            "estimated": f.get(SP) is not None, "epic": (f.get("parent") or {}).get("key") or "",
            "assignee": a.get("displayName") or "", "accountId": a.get("accountId") or "",
            "status": st, "done": done, "cancelled": cancelled,
            "oncall": bool(rx.search(f.get("summary") or "")),
            "lastMoved": moved,
            "stale": bool(not done and not cancelled and moved and moved < stale_cut.isoformat()),
            "addedMidSprint": (f.get("created") or "")[:10] > start.isoformat(),
            "notStarted": (not done and not cancelled
                           and st.lower() in ("to do", "open", "backlog", "selected for development")),
        })
    doc = {"v": SCHEMA, "generated": dt.date.today().isoformat(),
           "mode": "in-progress" if today < end else "closed",
           "sprints": sel, "projects": projects,
           "window": {"start": start.isoformat(), "end": end.isoformat(),
                      "asOf": asof.isoformat()},
           "pace": {"workingDays": len(allw), "elapsed": len(elapsed),
                    "remaining": len(allw) - len(elapsed), "fraction": round(frac, 3)},
           "staleCutoff": stale_cut.isoformat(), "staleDays": stale_days,
           "tickets": tickets}
    doc["people"] = _check_people(tickets, start, end, asof, frac)
    doc["totals"] = _check_totals(tickets, doc["people"], frac)
    return doc


def _check_people(tickets, start, end, asof, frac):
    """Per developer, mid-sprint. Capacity comes twice — pro-rated to the days elapsed and
    for the whole sprint — because judging day 6 of 10 against ten days of capacity makes
    every person look behind until the sprint is over."""
    full = ce.month_capacity(start.year, start.month, window=(start, end))
    sofar = ce.month_capacity(start.year, start.month, window=(start, asof))
    if "__error__" in full:
        return {"__error__": full["__error__"]}
    byname = {p["name"]: p for p in (sofar.get("people") or [])}
    bands_ = tier_bands()

    def treat(role, days, delivered):
        """One consistent reading: capacity and delivery measured over the SAME days.

        On-call is the case that makes this matter. month_capacity() removes on-call DAYS
        from the denominator, so counting on-call SP in the numerator credits the rota twice
        — 3.0 done against 3.75 capacity becomes 8.0 against 3.75, which reads 213%. Either
        treatment is defensible; mixing them is not. So each is built over its own day count
        and the caller picks which to show."""
        eff = (bands_.get(role) or {}).get("mid")
        cap = round(days * eff, 2) if eff else 0
        b = bands_.get(role or "")
        band = ({"low": round(days * b["low"], 2), "high": round(days * b["high"], 2),
                 "perDayLow": b["low"], "perDayHigh": b["high"]}
                if b and days and b.get("low") else None)
        return {"days": days, "capacity": cap, "delivered": round(delivered, 2),
                "pace": round(delivered / cap * 100, 1) if cap else None,
                "band": band,
                "vsLevel": (None if not band else "above" if delivered > band["high"]
                            else "within" if delivered >= band["low"] else "below")}
    acct = {}
    try:
        for p in ce._yaml("people.yaml")["people"]:
            if p.get("jira_id"):
                acct[str(p["jira_id"])] = p.get("canonical") or p.get("name")
    except Exception:
        pass
    canon2name = {p.get("canonical"): p["name"] for p in (full.get("people") or [])}
    bands = tier_bands()

    per = {}
    for p in (full.get("people") or []):
        sp_now = (byname.get(p["name"]) or {}).get("sp") or 0
        nd_now = (byname.get(p["name"]) or {}).get("net") or 0
        per[p["name"]] = {
            "name": p["name"], "canonical": p.get("canonical", ""), "role": p.get("role", ""),
            "capacityFull": round(p.get("sp") or 0, 2), "netDaysFull": p.get("net") or 0,
            "capacityToDate": round(sp_now, 2), "netDaysToDate": nd_now,
            "oncallDaysToDate": (byname.get(p["name"]) or {}).get("oncall") or 0,
            "leave": p.get("leave") or 0, "oncallDays": p.get("oncall") or 0,
            "done": 0.0, "oncallSP": 0.0, "cancelled": 0.0, "inFlight": 0.0,
            "inFlightOncall": 0.0, "notStarted": 0.0, "stale": 0, "unestimated": 0,
            "tickets": 0, "closedTickets": 0, "addedMid": 0}

    def who(t):
        c = acct.get(t.get("accountId") or "")
        if c and canon2name.get(c) in per:
            return canon2name[c]
        return t.get("assignee") or "(unassigned)"

    extra = {}
    for t in tickets:
        n = who(t)
        r = per.get(n) or extra.setdefault(n, {
            "name": n, "canonical": "", "role": "", "capacityFull": 0, "netDaysFull": 0,
            "capacityToDate": 0, "netDaysToDate": 0, "leave": 0, "oncallDays": 0,
            "done": 0.0, "oncallSP": 0.0, "cancelled": 0.0, "inFlight": 0.0,
            "inFlightOncall": 0.0, "notStarted": 0.0, "stale": 0, "unestimated": 0,
            "tickets": 0, "closedTickets": 0, "addedMid": 0, "offRoster": True})
        r["tickets"] += 1
        if t["addedMidSprint"]:
            r["addedMid"] += 1
        if t["stale"]:
            r["stale"] += 1
        if t["done"] and not t["estimated"]:
            r["unestimated"] += 1
        if t["cancelled"]:
            r["cancelled"] += t["sp"]
        elif t["done"]:
            r["closedTickets"] += 1
            r["oncallSP" if t["oncall"] else "done"] += t["sp"]
        elif t["notStarted"]:
            r["notStarted"] += t["sp"]
        else:
            r["inFlight"] += t["sp"]
            if t["oncall"]:
                r["inFlightOncall"] += t["sp"]

    out = list(per.values()) + list(extra.values())
    for r in out:
        for k in ("done", "oncallSP", "cancelled", "inFlight", "inFlightOncall", "notStarted"):
            r[k] = round(r[k], 2)
        # Two consistent readings, never mixed. Default (exOncall) leaves on-call out of
        # both sides; incOncall puts the rota days back into capacity AND the on-call SP
        # into delivery, which is what "count his on-call ticket" actually requires.
        nd = r.get("netDaysToDate") or 0
        od = r.get("oncallDaysToDate") or 0
        r["exOncall"] = treat(r.get("role"), nd, r["done"])
        r["incOncall"] = treat(r.get("role"), nd + od, r["done"] + r["oncallSP"])
        # "What if the work already started lands?" An in-flight on-call ticket only counts
        # under the include treatment, so the two forward views differ by exactly that.
        r["ifLandsEx"] = treat(r.get("role"), nd,
                               r["done"] + (r["inFlight"] - r["inFlightOncall"]))
        r["ifLandsInc"] = treat(r.get("role"), nd + od, r["done"] + r["oncallSP"] + r["inFlight"])
        c = r["capacityToDate"]
        r["paceEfficiency"] = round(r["done"] / c * 100, 1) if c else None
        # At this rate, where does the whole sprint land?
        r["projected"] = round(r["done"] / frac, 2) if frac else None
        r["projectedPct"] = (round(r["projected"] / r["capacityFull"] * 100, 1)
                             if r["projected"] is not None and r["capacityFull"] else None)
        b = bands.get(r.get("role") or "")
        nd = r.get("netDaysToDate") or 0
        r["band"] = ({"low": round(nd * b["low"], 2), "high": round(nd * b["high"], 2),
                      "perDayLow": b["low"], "perDayHigh": b["high"]}
                     if b and nd and b.get("low") else None)
        bb = r["band"]
        r["vsLevel"] = (None if not bb else "above" if r["done"] > bb["high"]
                        else "within" if r["done"] >= bb["low"] else "below")
        # Confidence gate. Each reason here is a thing that makes the NUMBER wrong, not a
        # thing that makes the person slow — so the caller shows these instead of a figure.
        why = []
        if r["unestimated"]:
            why.append(f"{r['unestimated']} closed ticket(s) carry no estimate")
        if r["stale"] >= 2:
            why.append(f"{r['stale']} ticket(s) not moved in {3}+ working days")
        if not r["capacityToDate"]:
            why.append("no capacity row — not on the roster")
        r["confidence"] = "low" if why else "ok"
        r["confidenceWhy"] = why
    out.sort(key=lambda r: (r["confidence"] != "low", -(r.get("stale") or 0),
                            (r.get("paceEfficiency") if r.get("paceEfficiency") is not None else 999)))
    return out


def _check_totals(tickets, people, frac):
    done = round(sum(t["sp"] for t in tickets if t["done"] and not t["oncall"]), 2)
    cap_now = (round(sum(p["capacityToDate"] for p in people
                         if not p.get("offRoster")), 2) if isinstance(people, list) else 0)
    cap_full = (round(sum(p["capacityFull"] for p in people
                          if not p.get("offRoster")), 2) if isinstance(people, list) else 0)
    committed = round(sum(t["sp"] for t in tickets if not t["cancelled"]), 2)
    onc = round(sum(t["sp"] for t in tickets if t["done"] and t["oncall"]), 2)
    cap_inc = (round(sum((p["exOncall"]["capacity"] if False else p["incOncall"]["capacity"])
                         for p in people if not p.get("offRoster")), 2)
               if isinstance(people, list) else 0)
    return {"committedSP": committed, "doneSP": done,
            "capacityInc": cap_inc, "doneInc": round(done + onc, 2),
            "paceInc": round((done + onc) / cap_inc * 100, 1) if cap_inc else None,
            "oncallSP": round(sum(t["sp"] for t in tickets if t["done"] and t["oncall"]), 2),
            "cancelledSP": round(sum(t["sp"] for t in tickets if t["cancelled"]), 2),
            "inFlightSP": round(sum(t["sp"] for t in tickets
                                    if not t["done"] and not t["cancelled"]
                                    and not t["notStarted"]), 2),
            "notStartedSP": round(sum(t["sp"] for t in tickets if t["notStarted"]), 2),
            "capacityToDate": cap_now, "capacityFull": cap_full,
            "paceEfficiency": round(done / cap_now * 100, 1) if cap_now else None,
            "projectedSP": round(done / frac, 2) if frac else None,
            "staleTickets": sum(1 for t in tickets if t["stale"]),
            "addedMidSprint": sum(1 for t in tickets if t["addedMidSprint"]),
            "unestimated": sum(1 for t in tickets if t["done"] and not t["estimated"]),
            "lowConfidence": sum(1 for p in people
                                 if isinstance(people, list) and p.get("confidence") == "low")}


# ----------------------------------------------------------------- manual sprint board

_STEP = 0.25            # the board plans to the quarter day
BOARD_SCHEMA = 4        # v4: ONE store, scheduled by date so windows can overlap


def _quarter(v, default):
    """Round to the nearest quarter day. Raises on anything that is not a number, so a
    malformed start is reported rather than silently becoming day one."""
    if v is None or v == "":
        v = default
    return round(float(v) / _STEP) * _STEP
BOARD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "state", "sprint_board.json")


def _board_key(sprint_ids, window=None):
    """A plan is keyed by the sprint set, or by the date window when there is no sprint —
    planning a cycle Jira has not created yet is the point of the date mode."""
    if window and window[0] and window[1]:
        return f"w:{window[0]}..{window[1]}"
    return ",".join(str(s) for s in sorted(int(x) for x in sprint_ids))


def push_decisions(path=None):
    """Decisions recorded against tiles that are derived rather than drawn."""
    store = (_read_board(path) or {}).get("board") or {}
    d = store.get("derived")
    return d if isinstance(d, dict) else {}


def board_plan(path=None):
    """The whole plan: every task and every heading, for every sprint and every window.

    There is deliberately ONE store rather than a plan per selection. The backlog is a list
    of what is on the manager's mind, which does not change because a different sprint is
    on screen; and scheduled work is pinned to a DATE, so a window overlapping days that
    are already planned shows that work instead of looking empty.
    """
    try:
        with open(path or BOARD_FILE) as fh:
            doc = json.load(fh) or {}
    except Exception:
        doc = {}
    store = doc.get("board") or {}
    return {"tasks": list(store.get("tasks") or []), "cats": list(store.get("cats") or []),
            "effort": bool(store.get("effort")),
            "epics": dict(store.get("epics") or {})}


def _migrate_keyed(doc, key, dates):
    """Convert a plan saved against a sprint or window key into the single dated store.

    Older plans held a day INDEX, which only means anything next to the selection it was
    saved under - the same work was invisible from every other one. The dates are known
    here, so the conversion happens once, on the first load of that selection.
    """
    old = doc.get(key)
    if not isinstance(old, dict) or "tasks" not in old:
        return False
    store = doc.setdefault("board", {"tasks": [], "cats": []})
    have = {t.get("id") for t in store.get("tasks") or []}
    for t in old.get("tasks") or []:
        if not isinstance(t, dict) or t.get("id") in have:
            continue
        conv = dict(t)
        try:
            start = float(conv.pop("start", 0) or 0)
        except (TypeError, ValueError):
            start = 0.0
        i = int(start)
        if conv.get("who") and 0 <= i < len(dates):
            conv["date"] = dates[i]
            conv["off"] = round((start - i) / _STEP) * _STEP
        else:
            # Anything that cannot be placed on a real date becomes backlog rather than
            # being dropped: it was written down for a reason.
            conv["date"], conv["off"], conv["who"] = "", 0.0, ""
        store["tasks"].append(conv)
        have.add(conv.get("id"))
    for c in old.get("cats") or []:
        if c and c not in store["cats"]:
            store["cats"].append(c)
    doc.pop(key, None)
    return True


def save_board_plan(tasks, cats=None, path=None, effort=None):
    """Replace the whole plan.

    Scratch only — nothing here is written to Jira. The board is for deciding who does what
    and when; moving a bar must never silently reassign a ticket or change a sprint, because
    an undo in the UI could not take that back.
    """
    if not isinstance(tasks, list):
        return {"__error__": "tasks must be a list"}
    if cats is not None and not isinstance(cats, list):
        return {"__error__": "cats must be a list"}
    clean = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "").strip()
        if not tid:
            continue
        try:
            # The board plans to the quarter day, so a length and an offset into a day are
            # stored as whole numbers of quarters. Rounding here rather than trusting the
            # browser keeps a hand-edited file on the same grid the bars are drawn on.
            days = max(_STEP, min(60.0, _quarter(t.get("days"), 1)))
            off = min(1 - _STEP, max(0.0, _quarter(t.get("off"), 0)))
        except (TypeError, ValueError):
            return {"__error__": f"bad days/off on {tid!r}"}
        date = str(t.get("date") or "").strip()
        if date:
            try:
                dt.date.fromisoformat(date)
            except ValueError:
                return {"__error__": f"bad date {date!r} on {tid!r}"}
        who = str(t.get("who") or "")
        # Scheduled means a person AND a day. Half of that is not a plan, so it goes back
        # to the backlog rather than being drawn somewhere arbitrary.
        if not who or not date:
            who, date, off = "", "", 0.0
        if not str(t.get("title") or "").strip() and not str(t.get("key") or "").strip():
            continue
        try:
            tint = int(t.get("tint") or 0)
        except (TypeError, ValueError):
            tint = 0
        clean.append({"id": tid, "key": str(t.get("key") or ""),
                      "tint": tint if 1 <= tint <= 12 else 0,
                      "title": str(t.get("title") or "")[:300],
                      "who": who, "date": date, "off": off, "days": days,
                      "sp": float(t.get("sp") or 0), "adhoc": bool(t.get("adhoc")),
                      "cat": str(t.get("cat") or "")[:60],
                      # Which epic this one task goes to, when it is not the heading's.
                      "epic": str(t.get("epic") or "").strip().upper()[:30]})
    seen, heads = set(), []
    for c in (cats or []):
        name = str(c or "").strip()[:60]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            heads.append(name)
    f = path or BOARD_FILE
    try:
        with open(f) as fh:
            doc = json.load(fh) or {}
    except Exception:
        doc = {}
    # Planning in EFFORT rather than in calendar days: two story points for someone at
    # 0.5 is four days on the board and still two against their capacity. It is stored
    # with the plan because it changes what every tile's length MEANS, so two browsers
    # must not disagree about it.
    doc["board"] = {"tasks": clean, "cats": heads, "v": BOARD_SCHEMA,
                    "effort": bool(effort),
                    # Heading to epic. Kept across a save of the tasks themselves.
                    "epics": (doc.get("board") or {}).get("epics") or {},
                    "saved": dt.datetime.now().isoformat(timespec="seconds")}
    os.makedirs(os.path.dirname(f), exist_ok=True)
    tmp = f + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, f)
    return {"ok": True, "tasks": len(clean)}


def _write_board(doc, path=None):
    f = path or BOARD_FILE
    os.makedirs(os.path.dirname(f), exist_ok=True)
    tmp = f + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, f)


def _read_board(path=None):
    try:
        with open(path or BOARD_FILE) as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def save_board_epics(epics, task_epics=None, path=None):
    """Which epic each heading belongs to, and any per-task override.

    The board files work under free-text headings and Jira wants an epic. This mapping is
    the one thing a push cannot work out for itself, so it is stored with the plan rather
    than retyped every cycle.
    """
    if not isinstance(epics, dict):
        return {"__error__": "epics must be an object"}
    if task_epics is not None and not isinstance(task_epics, dict):
        return {"__error__": "taskEpics must be an object"}
    doc = _read_board(path)
    store = doc.setdefault("board", {"tasks": [], "cats": []})
    clean = {}
    for cat, key in epics.items():
        # An empty heading is a real one: it is every tile typed straight onto the board.
        c, k = str(cat or "").strip(), str(key or "").strip().upper()
        if k:
            clean[c] = k
    store["epics"] = clean
    for tid, key in (task_epics or {}).items():
        for t in store.get("tasks") or []:
            if t.get("id") == tid:
                k = str(key or "").strip().upper()
                if k:
                    t["epic"] = k
                else:
                    t.pop("epic", None)
    _write_board(doc, path)
    return {"ok": True, "headings": len(clean)}


# What you decided about a tile on the push page. Stored on the tile itself, next to the
# plan it belongs to, because a decision made here has to survive leaving the page: the
# alternative is retyping every link and every skip on the way back.
PUSH_FIELDS = {
    "key": str,      # the ticket this tile already is
    "epic": str,     # an epic for this one tile, overriding its heading
    "nopush": bool,  # leave it alone: it is handled outside this push
    "pts": float,    # the estimate to send, overriding the one the board derives
    # Renames the TILE, not just the ticket. A board tile and the ticket it becomes
    # should not be allowed to drift apart, and a tile's name is the one thing here the
    # board is not the better place to fix: you only notice it reads badly when you see
    # it about to become a summary.
    "title": str,
}


def save_push_decisions(decisions, path=None):
    """Record per-tile push decisions. One write for the lot.

    `decisions` is {task_id: {key?, epic?, nopush?, pts?}}. A field that is absent is
    left alone; a field that is present and empty is CLEARED, which is what makes an
    accepted suggestion undoable.
    """
    if not isinstance(decisions, dict):
        return {"__error__": "decisions must be an object"}
    doc = _read_board(path)
    store = doc.setdefault("board", {"tasks": [], "cats": []})
    by_id = {t.get("id"): t for t in store.get("tasks") or []}
    # Some tiles are derived rather than drawn: the oncall stints read out of the
    # capacity calendar have no row on the board to hang a decision on. Their ids are
    # stable, so the decisions live in a map of their own beside the tasks.
    derived = store.setdefault("derived", {})
    saved, unknown = 0, []
    for tid, d in decisions.items():
        t = by_id.get(str(tid))
        if t is None:
            if ":" not in str(tid):
                unknown.append(str(tid))
                continue
            t = derived.setdefault(str(tid), {})
        if not isinstance(d, dict):
            return {"__error__": f"decision for {tid} must be an object"}
        for field, kind in PUSH_FIELDS.items():
            if field not in d:
                continue
            v = d[field]
            if kind is bool:
                if v:
                    t[field] = True
                else:
                    t.pop(field, None)
            elif kind is float:
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    f = 0.0
                if f > 0:
                    t[field] = f
                else:
                    t.pop(field, None)
            else:
                sv = str(v or "").strip()
                if field == "key":
                    sv = sv.upper()
                if sv:
                    t[field] = sv
                elif field == "title":
                    pass          # a tile with no name at all is not an improvement
                else:
                    t.pop(field, None)
        saved += 1
    _write_board(doc, path)
    return {"ok": True, "saved": saved, "unknown": unknown}


def set_task_key(task_id, key, path=None):
    """Record the ticket a task became, the moment it is created.

    Called from inside the push, before the next call is attempted: a key that exists only
    in a Jira response is a duplicate waiting for the next run.

    Also used to LINK a tile to a ticket that already exists, which is a planning
    decision rather than a side effect of a write. An empty key unlinks: a link made by
    accepting a suggestion has to be undoable, or a wrong guess is permanent.
    """
    k = str(key or "").strip().upper()
    if not task_id:
        return {"__error__": "need a task"}
    doc = _read_board(path)
    store = doc.get("board") or {}
    for t in store.get("tasks") or []:
        if t.get("id") == task_id:
            if k:
                t["key"] = k
            else:
                t.pop("key", None)
            _write_board(doc, path)
            return {"ok": True, "key": k}
    # A derived tile, oncall being the one that exists, has no row on the board. Its key
    # still has to land somewhere: without this the ticket is created again on the next
    # run, and only the adoption guard stops a duplicate.
    if ":" in str(task_id):
        d = store.setdefault("derived", {}).setdefault(str(task_id), {})
        if k:
            d["key"] = k
        else:
            d.pop("key", None)
        _write_board(doc, path)
        return {"ok": True, "key": k}
    return {"__error__": f"no such task: {task_id}"}


def _plan_for(sprint_ids, window, dates):
    """The plan, migrating this selection's old keyed copy first if there is one."""
    try:
        with open(BOARD_FILE) as fh:
            doc = json.load(fh) or {}
    except Exception:
        doc = {}
    if _migrate_keyed(doc, _board_key(sprint_ids, window), dates):
        os.makedirs(os.path.dirname(BOARD_FILE), exist_ok=True)
        tmp = BOARD_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
        os.replace(tmp, BOARD_FILE)
    return board_plan()


def sprint_board(sprint_ids, projects=None, window=None):
    """Per-developer swimlanes over the sprint's days, plus the sprint's tickets.

    The day grid carries each person's OWN non-working days (leave, holiday, on-call,
    weekend) so a bar can be laid across the days that person actually has. A five-day task
    on someone with two days of leave in the middle genuinely takes seven calendar days, and
    a planner that cannot show that is just a spreadsheet with colours.
    """
    projects = [p.strip() for p in (projects or [ce.JIRA_PROJECT]) if p and p.strip()]
    sprint_ids = [int(s) for s in (sprint_ids or [])]
    sel = []
    if window and window[0] and window[1]:
        # Date mode: plan a cycle Jira has not created a sprint for yet. There are no
        # tickets to place from, only capacity and whatever you type in.
        try:
            start = dt.date.fromisoformat(window[0])
            end = dt.date.fromisoformat(window[1])
        except ValueError:
            return {"__error__": f"bad dates {window[0]!r}..{window[1]!r}"}
        if end < start:
            return {"__error__": "to-date is before from-date"}
        if (end - start).days > 120:
            return {"__error__": "window longer than 120 days"}
        sel = [{"id": 0, "name": f"{window[0]} → {window[1]}",
                "start": window[0], "end": window[1], "state": "planned"}]
    else:
        if not sprint_ids:
            return {"__error__": "no sprint selected"}
        meta = {s["id"]: s for s in board_sprints(limit=200)}
        unknown = [s for s in sprint_ids if s not in meta]
        if unknown:
            return {"__error__": f"unknown sprint id(s): {unknown}"}
        sel = [meta[s] for s in sprint_ids]
        start = dt.date.fromisoformat(min(s["start"] for s in sel if s["start"]))
        end = dt.date.fromisoformat(max(s["end"] for s in sel if s["end"]))

    cap = ce.month_capacity(start.year, start.month, window=(start, end))
    if "__error__" in cap:
        return {"__error__": cap["__error__"]}

    SPRINT, _ = _fields()
    SP = ce.SP_FIELD
    raw = []
    if sprint_ids:
        try:
            raw = _search(f"project in ({','.join(projects)}) and sprint in "
                          f"({','.join(str(s) for s in sprint_ids)}) "
                          f"and issuetype in (Story,Task,Bug)",
                          ["summary", SP, "assignee", "status", "resolution", "parent"])
        except Exception as e:
            return {"__error__": str(e)}

    acct = {}
    try:
        for p in ce._yaml("people.yaml")["people"]:
            if p.get("jira_id"):
                acct[str(p["jira_id"])] = p.get("canonical") or p.get("name")
    except Exception:
        pass
    tickets = []
    for i in raw:
        f = i["fields"]
        a = (f.get("assignee") or {}) or {}
        res = ((f.get("resolution") or {}) or {}).get("name") or ""
        st = ((f.get("status") or {}) or {}).get("name") or ""
        tickets.append({"key": i["key"], "summary": f.get("summary") or "",
                        "sp": float(f.get(SP) or 0),
                        "who": acct.get(a.get("accountId") or "") or "",
                        "assignee": a.get("displayName") or "",
                        "status": st, "epic": (f.get("parent") or {}).get("key") or "",
                        "done": res == "Done",
                        "cancelled": st == "Cancelled" or res == "Won't Do"})

    return {"v": BOARD_SCHEMA, "generated": dt.date.today().isoformat(),
            "sprints": sel, "projects": projects,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "days": cap.get("days") or [],
            "people": [{"name": p["name"], "canonical": p.get("canonical", ""),
                        "role": p.get("role", ""), "eff": p.get("eff"),
                        "net": p.get("net"), "sp": p.get("sp"),
                        # Needed by the push to assign anything. Dropping it here left
                        # fifteen real tickets in Jira with no owner.
                        "jira_id": p.get("jira_id") or "",
                        # Which days were marked by hand, so the board can show an
                        # override as an override and cycle it from where it is.
                        "manual": p.get("manual") or [],
                        "statuses": p.get("statuses") or []}
                       for p in (cap.get("people") or [])],
            "mode": "window" if (window and window[0]) else "sprint",
            "tickets": tickets,
            "plan": _plan_for(sprint_ids, window, [d.get("date") for d in (cap.get("days") or [])])}

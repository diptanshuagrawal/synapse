#!/usr/bin/env python3
"""Turn the manual sprint board into a real Jira sprint.

Split deliberately in two.

`plan_push` is pure: the board plan plus a snapshot of Jira goes in, a list of exactly
what WOULD happen comes out. Nothing is sent, nothing is guessed, and every refusal says
why. That is the half worth testing hard, and it is the half the dashboard shows you
before anything is written.

`apply_push` is a thin adapter that does what the plan said, in order, recording each
step before attempting the next. It takes the call function as an argument so a test can
assert the exact payloads and their order without a Jira to send them to.
"""
import datetime as dt
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = 1

# Actions a row can carry. Named rather than spelled out at each use, because the whole
# preview is read by these words.
# A tile typed straight onto the board has no heading at all. That is a real case, not a
# mistake, so it is mappable like any other: everything with no heading goes to one epic.
NO_HEADING = "(no heading)"

CREATE = "create"      # no such ticket: make one
ADOPT = "adopt"        # a ticket with this summary already sits in the target epic
UPDATE = "update"      # the task already carries a key
SKIP = "skip"          # nothing to do, or something is missing; `why` says which


def _norm(s):
    """Summaries match on words, not on whitespace or case: a plan typed by hand and a
    ticket typed into Jira are rarely spaced the same."""
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def sprint_name(window, prefix=""):
    """A sprint is matched by NAME before it is created, so the name has to be derived
    from the plan rather than typed differently each time."""
    a, b = (window or ["", ""])[0], (window or ["", ""])[1]
    if not a or not b:
        return ""
    return f"{prefix.strip() + ' ' if prefix.strip() else ''}{a} to {b}"


def epic_for(task, mapping, fallback=""):
    """Which epic a task belongs to.

    A task's own epic wins, then the heading it is filed under, then the fallback. The
    board files work under free-text headings and Jira wants an epic, so this mapping is
    the one thing a push cannot work out for itself.
    """
    own = str(task.get("epic") or "").strip()
    if own:
        return own
    cat = str(task.get("cat") or "").strip().lower()
    for k, v in (mapping or {}).items():
        if str(k).strip().lower() == cat and str(v or "").strip():
            return str(v).strip()
    return str(fallback or "").strip()


# How this project picks an epic from a line of free text was already settled by
# /resolve-initiatives (derive/resolve_prefetch.py): ask Jira's own text search, then let
# the results vote. Reusing the weights rather than inventing a second matcher, because
# two of them would drift apart and only one would be the one you trust.
EPIC_TITLE_VOTE = 2    # the epic's own title matched the text
TICKET_VOTE = 1        # a ticket under that epic matched the text
DEAD = ("cancelled", "done")


# Jira's `summary ~ "..."` wants all the words, and a board tile is terse ("EP Migration",
# "TXN Guardrails"). So the epic list we already hold is matched locally as well, and both
# kinds of evidence vote in the same pool. Measured on the real board: text search alone
# found 11 of 29, local titles alone 14, the two together 22.
STOPWORDS = frozenset("""
the a an of for and or to in on with by from at as is are be been it its this that
new old update updates fix fixes test tests change changes work task ticket issue
id ids
""".split())


def words(s):
    """Significant words of a title, lowercased.

    Two-letter words are kept. The short ones are often the whole point on a board tile
    ("EP Migration", "TB Fix", "AA FIP"), and dropping them matched those tiles on the
    generic half of their name instead. The stop list, not the length, removes noise.
    """
    return {w for w in re.findall(r"[a-z0-9]+", str(s or "").lower())
            if len(w) > 1 and w not in STOPWORDS}


def title_matches(title, epics, min_overlap=0.25):
    """Epics whose NAME overlaps the tile's words, as `rank_epics` evidence.

    Jaccard rather than a raw count, so a long epic name cannot out-match a short one
    just by containing more words.
    """
    want = words(title)
    if not want:
        return []
    out = []
    for e in epics or []:
        key = str(e.get("key") or "")
        have = words(e.get("summary"))
        if not key or not have:
            continue
        both = want & have
        if both and len(both) / len(want | have) >= min_overlap:
            out.append({"key": key, "summary": str(e.get("summary") or ""),
                        "status": str(e.get("status") or "")})
    return out


# Linking a tile to a ticket that already exists is worth more than guessing its epic,
# because the ticket already knows its epic. A board tile is shorthand for the ticket's
# real name ("AA FIP" for "AA FIP: PAN + DOB discovery"), so plain word overlap misses
# most of them and containment is the signal that works.
EXACT = 1.0
PREFIX = 0.92
CONTAINS = 0.84
ALL_WORDS = 0.76       # every word of the tile appears in the summary, in any order
MIN_LINK = 0.45
DONE_PENALTY = 0.35    # finished work is rarely what a tile on a future sprint means


def match_tickets(title, issues, limit=3, min_score=MIN_LINK):
    """Existing tickets this tile probably already IS, best first. Pure.

    Scored by how completely the tile's text is contained in the ticket summary rather
    than by shared words, because the tile is almost always the short form.

    Closed work is scored but pushed down, never dropped: seeing that a tile matches a
    Done ticket is the useful answer sometimes, and silently hiding it would make the
    tile look new when it is not.
    """
    want_n = _norm(title)
    want_w = words(title)
    if not want_n:
        return []
    out = []
    for i in issues or []:
        key = str(i.get("key") or "")
        summ = str(i.get("summary") or "")
        have_n = _norm(summ)
        if not key or not have_n:
            continue
        have_w = words(summ)
        if have_n == want_n:
            score, why = EXACT, "same name"
        elif want_n and have_n.startswith(want_n):
            score, why = PREFIX, "the ticket name starts with this"
        elif want_n and want_n in have_n:
            score, why = CONTAINS, "the ticket name contains this"
        elif want_w and want_w <= have_w:
            score, why = ALL_WORDS, "every word of this is in the ticket name"
        else:
            both = want_w & have_w
            if not both:
                continue
            score, why = len(both) / len(want_w | have_w), "shares " + ", ".join(sorted(both))
        # Several tickets can land in the same band: "activity, backfill" is in both
        # "Backfill Activity / Sub-activity" and "Backfill : Add account_balance entry".
        # Word overlap breaks that tie, so the tightest summary wins instead of the
        # lowest ticket number.
        tie = (len(want_w & have_w) / len(want_w | have_w)) if (want_w or have_w) else 0.0
        # Relevance decides whether it is a match at all; being finished only decides
        # where it ranks. Otherwise a tile that IS a done ticket would look brand new.
        if score < min_score:
            continue
        dead = str(i.get("status") or "").lower() in DEAD
        if dead:
            score *= DONE_PENALTY
        out.append({"key": key, "summary": summ, "epic": str(i.get("epic") or ""),
                    "status": str(i.get("status") or ""), "closed": dead,
                    "score": round(score, 3), "tie": round(tie, 3), "why": why})
    out.sort(key=lambda r: (-r["score"], -r["tie"], r["key"]))
    return out[:max(0, limit)]


def rank_epics(epic_hits, ticket_hits, limit=3):
    """Candidate epics for one tile, best first. Pure.

    `epic_hits` are epics whose title matched, `ticket_hits` are non-epic issues whose
    summary matched, each carrying the `parent` it sits under. A matching epic title is
    worth more than a matching ticket, but several tickets under one epic outvote a
    single loose title match, which is the behaviour that makes this useful on tiles
    named after the work rather than after the epic.

    Closed and cancelled issues do not vote: they are evidence about last cycle.
    """
    votes = {}
    for e in epic_hits or []:
        key = str(e.get("key") or "")
        if not key or str(e.get("status") or "").lower() in DEAD:
            continue
        v = votes.setdefault(key, {"votes": 0, "summary": str(e.get("summary") or ""),
                                   "why": []})
        v["votes"] += EPIC_TITLE_VOTE
        v["why"].append("its title matches")
    for t in ticket_hits or []:
        par = str(t.get("parent") or "")
        if not par or str(t.get("status") or "").lower() in DEAD:
            continue
        v = votes.setdefault(par, {"votes": 0,
                                   "summary": str(t.get("parentSummary") or ""), "why": []})
        v["votes"] += TICKET_VOTE
        if not v["summary"]:
            v["summary"] = str(t.get("parentSummary") or "")
        if len(v["why"]) < 3:
            v["why"].append(str(t.get("summary") or "")[:46])
    ranked = sorted(votes.items(), key=lambda kv: (-kv[1]["votes"], kv[0]))[:max(0, limit)]
    return [{"epic": k, "summary": m["summary"], "votes": m["votes"], "why": m["why"]}
            for k, m in ranked]


# Oncall is not a tile anyone draws. It is "O" in the capacity calendar, and the days it
# takes are already subtracted there (net = working days - leave - oncall), which is why
# drawing a tile for it as well would charge the fortnight twice. The work still needs a
# ticket, so the push derives one from the rota rather than asking anyone to plan it.
ONCALL = "O"
ONCALL_KIND = "oncall"


def oncall_tiles(people, window, label="Oncall: {name}"):
    """One synthetic tile per unbroken oncall stint in the window.

    `statuses` runs one entry per calendar day from the window's first day, so the index
    IS the offset. A stint broken by a weekend is one stint, not two: the pager does not
    stop over the weekend, and two tickets for one rotation is not what anyone means.

    The ids are derived, not random, so a decision made about one of these survives the
    next preview: the same rota produces the same id.
    """
    a = (window or ["", ""])[0]
    try:
        start = dt.date.fromisoformat(str(a))
    except (TypeError, ValueError):
        return []
    out = []
    for p in people or []:
        st = p.get("statuses") or []
        who = str(p.get("canonical") or "")
        if not who:
            continue
        run = []           # indexes of oncall days, weekends inside a stint bridged below
        for i, code in enumerate(list(st) + [None]):
            if code == ONCALL:
                run.append(i)
                continue
            # A weekend sitting between two oncall days belongs to the same stint.
            if code == "WE" and run and ONCALL in st[i + 1:i + 3]:
                continue
            if not run:
                continue
            first, last = run[0], run[-1]
            d0, d1 = start + dt.timedelta(days=first), start + dt.timedelta(days=last)
            out.append({
                "id": f"oncall:{who}:{d0.isoformat()}",
                "title": label.format(name=p.get("name") or who,
                                      who=who, **{"from": d0.isoformat(),
                                                  "to": d1.isoformat()}),
                "who": who, "date": d0.isoformat(),
                # Sized by the rota: the days actually carried, weekends excluded.
                "days": float(len(run)), "kind": ONCALL_KIND,
                "cat": "", "sp": 0,
            })
            run = []
    return out


def _day(v):
    """The date part of whatever Jira returned: dates come back as full timestamps."""
    return str(v or "")[:10]


def _overlap(a1, b1, a2, b2):
    """Days two date ranges share. Zero when they do not meet."""
    try:
        s = max(dt.date.fromisoformat(a1), dt.date.fromisoformat(a2))
        e = min(dt.date.fromisoformat(b1), dt.date.fromisoformat(b2))
    except (TypeError, ValueError):
        return 0
    return max(0, (e - s).days + 1)


# A sprint rarely lines up to the day with the window typed on the board: Jira's end date
# is often the following morning. Requiring the dates to match exactly reported "none"
# for a sprint covering the same fortnight, so a sprint qualifies on how much of the
# window it covers instead.
COVERAGE = 0.6


def find_sprint(window, sprints, name=""):
    """The sprint this window already has, if any.

    Matched on OVERLAP first and on name only as a fallback. The name a team gives a
    sprint ("TEAM OND 26 S1") has nothing to do with the name this page would derive
    from a date range, and the dates themselves differ by a day more often than not, so
    neither an exact date match nor a name match finds the sprint that is plainly there.
    Getting this wrong creates a second sprint on top of a real one.

    A sprint that is not closed wins over one that is: a closed sprint covering the same
    fortnight is history, not the thing being planned.
    """
    a, b = _day((window or ["", ""])[0]), _day((window or ["", ""])[1])
    try:
        span = (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days + 1
    except (TypeError, ValueError):
        span = 0
    by_overlap, by_name = [], []
    for sp in sprints or []:
        ov = _overlap(a, b, _day(sp.get("start")), _day(sp.get("end"))) if span else 0
        if span and ov >= span * COVERAGE:
            by_overlap.append((ov, sp))
        elif name and _norm(sp.get("name")) == _norm(name):
            by_name.append(sp)
    if by_overlap:
        best = max(ov for ov, _ in by_overlap)
        pool = [sp for ov, sp in by_overlap if ov == best]
        live = [sp for sp in pool if str(sp.get("state") or "").lower() != "closed"]
        return (live or pool)[0], "dates"
    if by_name:
        live = [sp for sp in by_name if str(sp.get("state") or "").lower() != "closed"]
        return (live or by_name)[0], "name"
    return None, ""


def next_sprint_name(sprints, window):
    """What the NEXT sprint should probably be called.

    A derived "2026-10-21 to 2026-11-03" is a correct name and the wrong one: teams name
    sprints in a series, and a push that breaks the series leaves a sprint nobody
    recognises on the board. So the most recent sprint before this window is read, and if
    its name ends in a number that number is carried forward.

    A guess, shown in an editable box, never used without being seen.
    """
    a = (window or ["", ""])[0]
    prior = [sp for sp in sprints or [] if _day(sp.get("start")) and _day(sp.get("start")) < _day(a)]
    prior += [sp for sp in sprints or [] if _day(sp.get("end")) and _day(sp.get("end")) == _day(a)]
    if not prior:
        return ""
    last = max(prior, key=lambda sp: _day(sp.get("start")))
    name = str(last.get("name") or "").strip()
    m = re.search(r"^(.*?)(\d+)(\D*)$", name)
    if not m:
        return ""
    head, num, tail = m.group(1), m.group(2), m.group(3)
    return f"{head}{int(num) + 1:0{len(num)}d}{tail}"


def standing_for(title, rules):
    """The standing-work rule a tile matches, if any.

    Oncall and the standing review slot exist whether or not anyone plans them, so their
    size is a property of the rota rather than of the days a tile happens to cover.

    The FIRST matching rule wins, so config lists the specific ones above the general
    ones: writing the oncall report is not the same job as carrying the pager.
    """
    hay = _norm(title)
    for r in rules or []:
        m = _norm((r or {}).get("match"))
        if m and m in hay:
            return r
    return None


def plan_push(plan, people, window, jira, opts=None):
    """What a push would do. Pure.

    `jira` is a snapshot: {"epics": [{key, summary}], "sprints": [{id, name, state}],
    "issues": {KEY: {summary, epic, assignee, sp, sprintIds}},
    "byEpic": {EPIC: [{key, summary}]}}.
    """
    opts = opts or {}
    mapping = (plan or {}).get("epics") or {}
    fallback = str(opts.get("fallbackEpic") or "").strip()
    effort = bool((plan or {}).get("effort"))
    by_canon = {p.get("canonical"): p for p in (people or [])}
    epic_keys = {str(e.get("key")) for e in (jira or {}).get("epics") or []}
    issues = (jira or {}).get("issues") or {}
    by_epic = (jira or {}).get("byEpic") or {}

    name = sprint_name(window, opts.get("sprintPrefix") or "")
    existing, matched_by = find_sprint(window, (jira or {}).get("sprints"), name)

    # The board plan is ONE list across every sprint and window, and the board itself
    # only counts what the current window covers ("N scheduled outside this window").
    # Without the same filter here the push swept up every scheduled tile on the board
    # and put the lot into whichever sprint this window matched.
    w_from, w_to = (window or ["", ""])[0], (window or ["", ""])[1]

    def _covers(day):
        if not (w_from and w_to):
            return True
        return str(w_from) <= str(day) <= str(w_to)

    rows, seen, elsewhere = [], {}, 0
    for t in (plan or {}).get("tasks") or []:
        who = str(t.get("who") or "")
        title = str(t.get("title") or "").strip()
        key = str(t.get("key") or "").strip()
        # `ownEpic` is what this task overrides for itself; `epic` below is what it
        # RESOLVES to, heading mapping included. They are different facts and the page
        # must not offer the resolved one back as if it were an override, or saving twice
        # would freeze the mapping onto every task and re-mapping would stop working.
        row = {"id": t.get("id"), "title": title, "key": key, "who": who,
               "cat": str(t.get("cat") or ""),
               "ownEpic": str(t.get("epic") or "").strip(),
               # Echoed back so the page can show the decisions you already made rather
               # than redrawing them as untouched.
               "nopush": bool(t.get("nopush")), "pts": _num(t.get("pts")),
               "warn": []}

        # Backlog tiles are not commitments. Neither is a nameless one.
        if not who or not t.get("date"):
            continue
        # Scheduled, but not in the fortnight being pushed. Counted, not listed: it
        # belongs to another sprint and saying so is the point.
        if not _covers(t.get("date")):
            elsewhere += 1
            continue
        # Marked as handled elsewhere. Reported rather than dropped, so the count you
        # confirm still adds up to the board you are looking at.
        if t.get("nopush"):
            row.update({"action": SKIP, "why": "you marked this as handled outside the push"})
            rows.append(row)
            continue
        if not title:
            row.update({"action": SKIP, "why": "no title"})
            rows.append(row)
            continue

        person = by_canon.get(who) or {}
        row["assignee"] = person.get("name") or who
        row["accountId"] = str(person.get("jira_id") or "")
        if not row["accountId"]:
            row["warn"].append("no Jira account on file, it will be left unassigned")

        # Story points, and they must be the number the board's Planned column shows for
        # this tile. Anything else means the plan you approved and the plan that lands in
        # Jira are different plans.
        #
        # In days mode that number is the tile's length, as typed: a day is a day, and
        # efficiency is already accounted for in the capacity it is measured against.
        # In effort mode the tile carries its own estimate, and only a tile old enough to
        # predate effort mode is read as a span and converted.
        # Mirrors daysOf()/effortOf() in derived/sprint-board.html.
        eff = _num(person.get("eff"), 1) or 1
        sp = _num(t.get("sp"))
        if effort:
            if sp <= 0:
                sp = round(_num(t.get("days")) * eff * 4) / 4
        else:
            sp = round(_num(t.get("days")) * 4) / 4

        # Standing work exists whether or not it is written down, and the calendar days
        # it happens to occupy say nothing about its size. Configured, not hardcoded.
        if t.get("kind") == ONCALL_KIND:
            row["spFrom"] = f"{_num(t.get('days')):g} day(s) on the rota"
        standing = standing_for(title, opts.get("standing")) \
            if t.get("kind") != ONCALL_KIND else None
        if standing:
            sp = _num(standing.get("points"), sp)
            row["spFrom"] = f"standing work: {standing.get('match')}"

        # A number typed on this row beats everything above it.
        if _num(t.get("pts")) > 0:
            sp = _num(t.get("pts"))
            row["spFrom"] = "set by hand"
        row["sp"] = sp
        row["kind"] = str(t.get("kind") or "")
        if sp <= 0:
            row["warn"].append("no estimate")

        epic = epic_for(t, mapping, fallback)
        # A tile linked to a real ticket needs no heading mapping: the ticket already
        # knows which epic it belongs to, and that answer beats any guess of ours.
        if not epic and key:
            epic = str((issues.get(key) or {}).get("epic") or "")
            if epic:
                row["epicFrom"] = f"inherited from {key}"
        row["epic"] = epic
        if not epic:
            row.update({"action": SKIP, "why": "no epic for this heading"})
            rows.append(row)
            continue
        if epic_keys and epic not in epic_keys:
            row.update({"action": SKIP, "why": f"{epic} is not an epic in this project"})
            rows.append(row)
            continue

        # Two tiles with the same name under the same epic would create two tickets on
        # the first run and adopt the same one on the second. Say so instead.
        dup = (epic, _norm(title))
        if dup in seen:
            row.update({"action": SKIP, "why": f"same name as another tile ({seen[dup]})"})
            rows.append(row)
            continue
        seen[dup] = t.get("id")

        if key:
            cur = issues.get(key)
            if cur is None:
                row.update({"action": SKIP, "why": f"{key} was not found in Jira"})
                rows.append(row)
                continue
            changes = []
            if str(cur.get("epic") or "") != epic:
                changes.append(("epic", cur.get("epic") or "none", epic))
            if row["accountId"] and str(cur.get("assignee") or "") != row["accountId"]:
                changes.append(("assignee", cur.get("assigneeName") or "none", row["assignee"]))
            want_type = str(opts.get("issuetype") or "")
            cur_type = str(cur.get("issuetype") or "")
            if want_type and cur_type and cur_type != want_type:
                changes.append(("issuetype", cur.get("issuetypeName") or cur_type,
                                opts.get("issuetypeName") or want_type))
            cur_sp = _num(cur.get("sp"))
            if sp > 0 and cur_sp <= 0:
                changes.append(("points", "none", sp))
            elif sp > 0 and abs(cur_sp - sp) > 1e-9:
                # Someone else's estimate. Offered, never taken without a separate yes.
                row["warn"].append(f"Jira says {cur_sp} points, the board says {sp}")
                if opts.get("overwritePoints"):
                    changes.append(("points", cur_sp, sp))
            sid = existing.get("id") if existing else None
            if sid and sid not in (cur.get("sprintIds") or []):
                changes.append(("sprint", "not in it", name))
            elif not sid:
                changes.append(("sprint", "not in it", name))
            row.update({"action": UPDATE if changes else SKIP,
                        "changes": changes})
            if not changes:
                row["why"] = "already as planned"
            rows.append(row)
            continue

        # No key. Before creating, look for a ticket of the same name already sitting in
        # the target epic: that is what a failed write-back leaves behind, and creating
        # again would quietly double it.
        twin = None
        for cand in by_epic.get(epic) or []:
            if _norm(cand.get("summary")) == _norm(title):
                twin = cand
                break
        if twin:
            row.update({"action": ADOPT, "key": str(twin.get("key")),
                        "why": "a ticket of this name is already in that epic"})
        else:
            row.update({"action": CREATE})
        rows.append(row)

    counts = {a: 0 for a in (CREATE, ADOPT, UPDATE, SKIP)}
    for r in rows:
        counts[r.get("action", SKIP)] = counts.get(r.get("action", SKIP), 0) + 1

    unmapped = sorted({r["cat"] or NO_HEADING for r in rows
                       if r.get("action") == SKIP and r.get("why") == "no epic for this heading"})
    return {"v": SCHEMA, "generated": dt.date.today().isoformat(),
            "sprint": {"name": (existing or {}).get("name") or name,
                       "derivedName": name,
                       "start": (window or ["", ""])[0],
                       "end": (window or ["", ""])[1],
                       "exists": bool(existing),
                       "suggestName": "" if existing else
                           (next_sprint_name((jira or {}).get("sprints"), window) or name),
                       "matchedBy": matched_by,
                       "state": (existing or {}).get("state") or "",
                       "id": (existing or {}).get("id")},
            "effort": effort, "fallbackEpic": fallback,
            "rows": rows, "counts": counts, "unmapped": unmapped,
            "elsewhere": elsewhere,
            "ready": bool(name) and not unmapped and (counts[CREATE] + counts[ADOPT] + counts[UPDATE]) > 0}


def apply_push(preview, call, board_id, project, fields, confirm=False, on_key=None):
    """Do what the preview said, in order.

    `call(method, path, body)` is the Jira caller, passed in so a test can record the
    exact payloads and their order. `on_key(task_id, key)` is called the moment a ticket
    is created, before anything else is attempted, because a key that is not written back
    is a duplicate on the next run.
    """
    if not confirm:
        return {"__error__": "a push is never started without an explicit confirmation"}
    if not preview or preview.get("v") != SCHEMA:
        return {"__error__": "stale preview, run it again"}
    if preview.get("unmapped"):
        return {"__error__": "every heading needs an epic first: "
                             + ", ".join(preview["unmapped"])}

    sp_field = (fields or {}).get("story_points")
    # No default. Guessing produced fifteen tickets of the wrong type, silently, and the
    # only way anyone found out was by looking at them in Jira.
    issuetype = str((fields or {}).get("issuetype_id") or "").strip()
    if not issuetype:
        return {"__error__": "jira_fields.task_issuetype_id is not set in "
                             "config/sprint_planning.yaml, so there is no type to "
                             "create tickets as"}
    sprint = dict(preview.get("sprint") or {})
    done = {"sprint": None, "created": [], "adopted": [], "updated": [], "failed": []}

    try:
        if not sprint.get("id"):
            made = call("POST", "/rest/agile/1.0/sprint",
                        {"name": sprint.get("name"), "originBoardId": board_id,
                         "startDate": sprint.get("start"), "endDate": sprint.get("end")})
            sprint["id"] = made.get("id")
            done["sprint"] = {"id": sprint["id"], "name": sprint.get("name"), "created": True}
        else:
            done["sprint"] = {"id": sprint["id"], "name": sprint.get("name"), "created": False}
    except Exception as e:
        return {"__error__": f"could not create the sprint: {e}", "done": done}

    into_sprint = []
    for row in preview.get("rows") or []:
        act = row.get("action")
        if act == SKIP:
            continue
        try:
            if act == CREATE:
                f = {"project": {"key": project},
                     "issuetype": {"id": issuetype},
                     "summary": row.get("title", "")[:250],
                     "parent": {"key": row.get("epic")}}
                if row.get("accountId"):
                    f["assignee"] = {"accountId": row["accountId"]}
                if sp_field and _num(row.get("sp")) > 0:
                    f[sp_field] = _num(row["sp"])
                made = call("POST", "/rest/api/3/issue", {"fields": f})
                key = made.get("key")
                row["key"] = key
                # Written back FIRST. A key that only exists in this response is a
                # duplicate waiting for the next run.
                if on_key and key:
                    on_key(row.get("id"), key)
                done["created"].append({"id": row.get("id"), "key": key,
                                        "title": row.get("title")})
                into_sprint.append(key)
            elif act == ADOPT:
                if on_key and row.get("key"):
                    on_key(row.get("id"), row.get("key"))
                done["adopted"].append({"id": row.get("id"), "key": row.get("key"),
                                        "title": row.get("title")})
                into_sprint.append(row.get("key"))
            elif act == UPDATE:
                f = {}
                for what, _old, new in row.get("changes") or []:
                    if what == "epic":
                        f["parent"] = {"key": new}
                    elif what == "assignee" and row.get("accountId"):
                        f["assignee"] = {"accountId": row["accountId"]}
                    elif what == "points" and sp_field:
                        f[sp_field] = _num(new)
                    elif what == "issuetype":
                        f["issuetype"] = {"id": issuetype}
                if f:
                    call("PUT", f"/rest/api/3/issue/{row['key']}", {"fields": f})
                done["updated"].append({"key": row.get("key"),
                                        "changed": [c[0] for c in row.get("changes") or []]})
                into_sprint.append(row.get("key"))
        except Exception as e:
            done["failed"].append({"id": row.get("id"), "key": row.get("key"),
                                   "title": row.get("title"), "error": str(e)})

    # Moving issues into the sprint is one call for up to fifty, and it is done LAST so a
    # failure earlier leaves tickets that exist rather than a sprint that half-contains
    # things nobody made.
    keys = [k for k in into_sprint if k]
    for i in range(0, len(keys), 50):
        chunk = keys[i:i + 50]
        try:
            call("POST", f"/rest/agile/1.0/sprint/{sprint['id']}/issue", {"issues": chunk})
        except Exception as e:
            done["failed"].append({"key": ", ".join(chunk), "error": f"into sprint: {e}"})

    done["ok"] = not done["failed"]
    done["sprintId"] = sprint.get("id")
    return done

#!/usr/bin/env python3
"""standup_gather.py — single-shot data gather for /standup.

Why: the standup gather was being run as ~10 sequential model-driven SQL
round-trips. The DB work is <0.1s; the cost was tool round-trips + model turns.
This script does the ENTIRE gather (all roster members, board state, window
events, heavy Slack scan) in ONE pass and prints a compact digest the model
formats + enriches in a single turn.

Usage:
  python3 bin/standup_gather.py <YYYY-MM-DD> [scope]
    scope = team (default) | me | <canonical>
Read-only. Never writes events.db.
"""
import sqlite3, sys, json, datetime, re, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "work-context/index/events.db")
PEOPLE = os.path.join(ROOT, "work-context/config/people.yaml")

sys.path.insert(0, os.path.join(ROOT, "work-context"))
from derive.sources_config import slack_permalink, owner_handle  # noqa: E402

TERMINAL = {"Done", "Closed", "Resolved", "Released", "Released and Reviewed"}
INPROG = {"In Progress", "In Review", "In Development"}
TODO = {"To Do", "Open", "Reopened", "Selected for Development", "Backlog"}
CMR_CLOSED = {"Released", "Cancelled", "Released and Reviewed", "Implementation Reviewed", "Review Complete", "Rolled Back"}
# MATCHER-VS-TRUNCATION INVARIANT: every query whose rows are tested by these three
# regexes (or by `<@uid>` / `<!subteam^…>` substring checks) MUST select the FULL `body`
# column — never `substr(body, …)`. Trigger words / mentions routinely sit past the first
# few hundred chars (long greeting + cc-list + URL first), so truncating before the regex
# silently drops real asks/leaves. Trim only for DISPLAY, in Python (`snip = …[:N]`).
LEAVE_RE = re.compile(r"\b(on leave|sick|fever|unwell|ooo|out of office|day off|taking the day|wfh|working from home|taking leave|half day)\b", re.I)
# Automation / non-ask noise to drop from the @-ask scan.
NOISE_RE = re.compile(r"(Request approved for|marked the issue as resolved|Time to Resolve|Weekly Oncall Stats|Weekly Stats Report|has joined the channel)", re.I)
# A real ask = a question or an imperative directed at someone.
ASK_RE = re.compile(r"(\?|\bcan you\b|\bcould you\b|\bplease\b|\bpls\b|\bplz\b|\bneed from you\b|\breview\b|\bcheck\b|\bapprove\b|\bconfirm\b|\bshare\b|\bupdate on\b|\bany update\b|\baction item\b|\bpick(ed)? (this )?up\b)", re.I)


def load_roster():
    import yaml
    d = yaml.safe_load(open(PEOPLE))
    people = d.get("people", d)
    out = {}
    for p in (people if isinstance(people, list) else people.values()):
        if isinstance(p, dict) and p.get("scope") == "team":
            out[p["canonical"]] = p
    return out


def owner_subteams():
    """Map of Slack subteam ping-token -> tier for subteams the OWNER belongs to
    (rows flagged `owner_member: true` in config/team_subteams.yaml). tier ∈
    {'managerial','dev'}, defaulting to 'managerial' when unset. Used to widen AND
    classify the owner @-ask scan: a direct <@owner> mention or a MANAGERIAL-group ping
    (tech-managers / cbs-ems / IC …) is the owner's own reply-pending; a DEV-group ping
    (his team handle) is work to ROUTE to a dev, not his personal reply. Fail-soft → {}."""
    import yaml
    try:
        cfg = yaml.safe_load(open(os.path.join(ROOT, "work-context/config/team_subteams.yaml")))
        out = {}
        for s in cfg.get("subteams", []):
            if s.get("owner_member") and s.get("id"):
                out[f"<!subteam^{s['id']}"] = (s.get("tier") or "managerial")
        return out
    except Exception:
        return {}


def _opsgenie_cfg():
    """Return (schedule, identifier_type, key) from config/oncall.yaml + secrets.
    Raises on missing config/key so callers can fail-soft uniformly."""
    import yaml
    og = yaml.safe_load(open(os.path.join(ROOT, "work-context/config/oncall.yaml")))["opsgenie"]
    env = og.get("api_key_env", "OPSGENIE_API_KEY")
    key = os.environ.get(env, "")
    if not key:
        sec = os.path.expanduser("~/.secrets/opsgenie_api_key")
        if os.path.exists(sec):
            key = open(sec).read().strip()
    if not key:
        raise RuntimeError(f"no API key ({env} unset, no ~/.secrets/opsgenie_api_key)")
    return og["schedule"], og.get("identifier_type", "name"), key


def _oncall_at(sched, idtype, key, date_iso=None):
    """Who's on-call now (date_iso=None) or at a future instant. Uses flat=true so
    the response is a plain list of recipient emails (onCallRecipients)."""
    import urllib.request, urllib.parse
    qs = {"scheduleIdentifierType": idtype, "flat": "true"}
    if date_iso:
        qs["date"] = date_iso
    url = (f"https://api.opsgenie.com/v2/schedules/{sched}/on-calls?"
           + urllib.parse.urlencode(qs))
    req = urllib.request.Request(url, headers={"Authorization": "GenieKey " + key})
    d = json.load(urllib.request.urlopen(req, timeout=10))
    return d.get("data", {}).get("onCallRecipients", []) or []


def gather_oncall(roster):
    """Live Opsgenie who's-on-call, config-driven (config/oncall.yaml). Fail-soft:
    a dead Opsgenie must not kill the gather — emit a warning line instead."""
    try:
        sched, idtype, key = _opsgenie_cfg()
        emails = _oncall_at(sched, idtype, key)
        by_email = {v.get("email", ""): k for k, v in roster.items()}
        return [f"  {e}  canonical={by_email.get(e, '?(not roster)')}" for e in emails] or ["  (none returned)"]
    except Exception as e:
        return [f"  ⚠️ opsgenie lookup failed: {e}"]


def gather_oncall_forecast(roster, date_str, days=14):
    """Forecast the on-call primary for each of the next `days` days (rolling, =1
    sprint). Returns (lines, forecast) where forecast={iso_date: canonical|email|None}.
    One on-calls?date= query per day (noon UTC); each is independently fail-soft so a
    single dead day degrades to None rather than killing the whole forecast."""
    by_email = {v.get("email", ""): k for k, v in roster.items()}
    forecast, lines = {}, []
    try:
        sched, idtype, key = _opsgenie_cfg()
    except Exception as e:
        return [f"  ⚠️ forecast unavailable: {e}"], forecast
    d0 = datetime.date.fromisoformat(date_str)
    for i in range(days):
        day = (d0 + datetime.timedelta(days=i)).isoformat()
        try:
            emails = _oncall_at(sched, idtype, key, f"{day}T12:00:00Z")
            who = by_email.get(emails[0], emails[0]) if emails else None
        except Exception:
            who = None  # leave a hole; don't abort the rest of the sprint
        forecast[day] = who
        lines.append(f"  {day} {who or '?(lookup failed)'}")
    return lines, forecast


def gather_risks(cur, roster, date_str, forecast, days=14):
    """Risk/collision scan over the next `days` days (rolling sprint):
      • LEAVE×ONCALL — a roster member scheduled on-call on a day they're on leave.
      • COVERAGE     — ≥2 roster members out the same day (thin coverage).
    Cross-refs the on-call forecast against durable team_leaves. Fail-soft."""
    horizon = (datetime.date.fromisoformat(date_str) + datetime.timedelta(days=days)).isoformat()
    # per-day set of on-leave roster canonicals + the reason for messaging
    onleave, reason = {}, {}
    try:
        for a, ds, de, rs in cur.execute(
                "SELECT actor,date_start,date_end,reason FROM team_leaves "
                "WHERE date_end>=? AND date_start<=? ORDER BY date_start",
                (date_str, horizon)).fetchall():
            if a not in roster or not (ds and de):
                continue
            d = datetime.date.fromisoformat(ds)
            end = datetime.date.fromisoformat(de)
            while d <= end:
                iso = d.isoformat()
                if date_str <= iso <= horizon:
                    onleave.setdefault(iso, set()).add(a)
                    reason[(a, iso)] = f"{rs} {ds}..{de}"
                d += datetime.timedelta(days=1)
    except sqlite3.Error as e:
        return [f"  ⚠️ team_leaves read failed: {e}"]
    risks = []
    for day in sorted(set(list(forecast) + list(onleave))):
        out_today = sorted(onleave.get(day, set()))
        oc = forecast.get(day)
        if oc and oc in onleave.get(day, set()):
            risks.append(f"  ⚠️ LEAVE×ONCALL {oc} on-call {day} but ON LEAVE ({reason.get((oc, day), 'leave')})")
        if len(out_today) >= 2:
            risks.append(f"  ⚠️ COVERAGE {day}: {len(out_today)} out ({', '.join(out_today)})")
    return risks or ["  (none)"]


def gather_leaves(cur, roster, date_str, W1, WL):
    """LEAVES = durable team_leaves (overlapping the day + upcoming 14d) + live
    slack scan (lookback..window-end) so a same-day sick message isn't missed."""
    lines = []
    horizon = (datetime.date.fromisoformat(date_str) + datetime.timedelta(days=14)).isoformat()
    try:
        for a, ds, de, rs, url in cur.execute(
                "SELECT actor,date_start,date_end,reason,url FROM team_leaves "
                "WHERE date_end>=? AND date_start<=? ORDER BY date_start",
                (date_str, horizon)).fetchall():
            tag = "ON-LEAVE-THIS-DAY" if (ds and de and ds <= date_str <= de) else "UPCOMING"
            lines.append(f"  {tag} {a} {ds}..{de} ({rs}) {url or ''}")
    except sqlite3.Error as e:
        lines.append(f"  ⚠️ team_leaves read failed: {e}")
    sl2canon = {v.get("slack_id"): k for k, v in roster.items() if v.get("slack_id")}
    # FULL body (no substr) — LEAVE_RE detection must see the whole message; a leave note
    # ("ooo", "sick", "wfh") often sits after a greeting/cc and would be cut at 200. Display
    # trims below (snip = …[:120]). See the matcher-vs-truncation note on slack_recent.
    for actor, ts, body, subj in cur.execute(
            "SELECT actor,ts,body,subject FROM events "
            "WHERE source='slack' AND ts>=? AND ts<? ORDER BY ts", (WL, W1)):
        if actor in sl2canon and LEAVE_RE.search(body or ""):
            snip = re.sub(r"\s+", " ", body or "")[:120]
            try:
                _, ch, mts = subj.split(":", 2)
                link = slack_permalink(ch, mts)
            except Exception:
                link = ""
            lines.append(f"  LIVE-SIGNAL {sl2canon[actor]} [{ts[:16]}] link={link} :: {snip}")
    return lines or ["  (none)"]


# TEAM open-ask lookback = 2 days (window day + 1 prior): a longer window re-surfaces
# the same stale teammate asks every run. The OWNER queue uses a LONGER lookback —
# a manager's pending reply/approval/review can legitimately sit unanswered for days,
# and dropping it after 2d is exactly the blind spot we're closing.
OWNER_ASK_LOOKBACK_DAYS = 5


def ist_window(date_str):
    """IST day -> UTC ISO bounds (naive, comparable to stored ts[:19]).
    Returns (W0, W1, WL, WLo): window start/end, 2-day team lookback, and the
    longer owner-ask lookback (OWNER_ASK_LOOKBACK_DAYS)."""
    y, m, dd = map(int, date_str.split("-"))
    ist0 = datetime.datetime(y, m, dd, 0, 0)
    u0 = ist0 - datetime.timedelta(hours=5, minutes=30)
    u1 = u0 + datetime.timedelta(days=1)
    uL = u0 - datetime.timedelta(days=1)
    uLo = u0 - datetime.timedelta(days=OWNER_ASK_LOOKBACK_DAYS)
    f = lambda x: x.strftime("%Y-%m-%dT%H:%M:%S")
    return f(u0), f(u1), f(uL), f(uLo)


def main():
    if len(sys.argv) < 2:
        print("usage: standup_gather.py <YYYY-MM-DD> [scope]"); sys.exit(1)
    date_str = sys.argv[1]
    scope = sys.argv[2] if len(sys.argv) > 2 else "team"
    W0, W1, WL, WLo = ist_window(date_str)  # WL=2d team lookback, WLo=longer owner lookback
    c = sqlite3.connect(DB); cur = c.cursor()
    roster = load_roster()

    # owner = manager; exclude from team digest. Heuristic: canonical 'owner'.
    owner = owner_handle()
    if scope == "team":
        members = [k for k in roster if k != owner]
    elif scope == "me":
        members = [owner]
    else:
        members = [scope] if scope in roster else []
    if not members:
        print(f"!! scope '{scope}' not in roster {list(roster)}"); sys.exit(1)

    # --- DEV vs REVIEWER roles: delegated to the shared engine (single source of
    # truth — derive/jira_metrics). It replays assignment-event TITLES + status
    # transitions, so reassignment-to-reviewer is actually detected. The old inline
    # reconstruction read the `assignee` COLUMN, which is null on status/assignment
    # rows, so it never saw a reassignment (reviewer was always None). infer_all_*
    # returns CANONICAL handles, so the board is keyed/matched by canonical (not
    # email) below. ---
    from derive.jira_metrics import infer_all_ticket_roles, load_people_lookup
    roles = infer_all_ticket_roles(c, load_people_lookup())
    WORK_STATES = ("in_progress", "in_review_active", "in_review_awaiting_reviewer")

    # Per-subject metadata the role engine doesn't carry: issue_type + title.
    state = {}
    for sub, et, it, title in cur.execute(
        "SELECT subject,event_type,issue_type,title FROM events "
        "WHERE source='jira' AND subject IS NOT NULL ORDER BY ts"):
        d = state.setdefault(sub, {})
        if it: d["type"] = it
        if et == "issue_created" and "title" not in d:
            d["title"] = (title or "")[:90]

    # Merge roles + build indices (all canonical). owner = dev for active work,
    # else the current assignee (To-Do/CMR keep latest-assignee semantics).
    by_assignee, by_owner, by_reviewer = {}, {}, {}
    for sub, r in roles.items():
        d = state.setdefault(sub, {})
        d["status"] = r.current_status
        d["state"] = r.state
        d["dev"] = r.dev or r.dev_raw               # display: canonical, else raw name
        d["reviewer"] = r.reviewer                  # canonical, else raw (or None)
        cur_c = r.current_assignee
        d["assignee"] = cur_c or r.current_assignee_raw
        owner_c = r.dev if r.state in WORK_STATES else cur_c
        d["owner"] = (r.dev or r.dev_raw) if r.state in WORK_STATES else d["assignee"]
        if owner_c: by_owner.setdefault(owner_c, []).append(sub)
        if r.reviewer: by_reviewer.setdefault(r.reviewer, []).append(sub)
        if cur_c: by_assignee.setdefault(cur_c, []).append(sub)
    nm = lambda x: x   # roles already canonical; kept as a display hook

    emails = {m: roster[m].get("email", "") for m in members}
    slids = {m: roster[m].get("slack_id", "") for m in members}
    jids = {m: roster[m].get("jira_id", "") for m in members}
    ghs = {m: roster[m].get("github", "") for m in members}

    # --- WINDOW jira events (actor OR assignee in member set) ---
    member_emails = set(filter(None, emails.values()))
    win_jira = cur.execute(
        "SELECT ts,event_type,subject,to_status,actor,assignee FROM events "
        "WHERE source='jira' AND ts>=? AND ts<? ORDER BY ts", (W0, W1)).fetchall()
    # --- WINDOW github by member ---
    member_ghs = set(filter(None, ghs.values()))
    win_gh = cur.execute(
        "SELECT ts,event_type,subject,actor,substr(title,1,70) FROM events "
        "WHERE source='github' AND ts>=? AND ts<? ORDER BY ts", (W0, W1)).fetchall()
    # --- WINDOW confluence by member jira_id ---
    win_conf = cur.execute(
        "SELECT ts,event_type,subject,actor FROM events "
        "WHERE source='confluence' AND ts>=? AND ts<? ORDER BY ts", (W0, W1)).fetchall()
    # confluence page titles
    conf_titles = {}
    for sub, ti in cur.execute("SELECT subject,title FROM events WHERE source='confluence' AND title IS NOT NULL"):
        conf_titles[sub] = ti
    # --- github PR INDEX: DETERMINISTIC per-PR descriptor ---
    # author (gh login → roster canonical) + title + first body line, keyed by PR number,
    # built from pr_opened/pr_merged rows — the ONLY github rows carrying the real PR
    # title/body. Review/comment rows have neither, so a member who only REVIEWED a PR
    # still gets a full descriptor here. The formatter COPIES this verbatim — it must never
    # re-derive a PR's purpose or guess its author from who reviewed it. pr_opened wins for
    # author (the opener); scoped to PRs referenced in this window to keep the block small.
    def _pr_num(subject):
        return subject.rsplit("#", 1)[-1] if subject and "#" in subject else None
    gh_login_to_canon = {v: k for k, v in ghs.items() if v}
    win_pr_nums = {n for n in (_pr_num(s) for (_, _, s, _, _) in win_gh) if n}
    pr_index = {}
    for sub, actor, ti, bo in cur.execute(
            "SELECT subject,actor,title,substr(body,1,200) FROM events "
            "WHERE source='github' AND event_type IN ('pr_opened','pr_merged') "
            "ORDER BY CASE event_type WHEN 'pr_opened' THEN 0 ELSE 1 END, ts"):
        n = _pr_num(sub)
        if not n or n not in win_pr_nums or n in pr_index:
            continue
        author = gh_login_to_canon.get(actor, actor or "?")
        desc = next((l.strip(" -*\t") for l in (bo or "").splitlines() if l.strip(" -*\t")), "")
        pr_index[n] = (sub, author, (ti or "").strip(), desc)
    # --- SLACK authored in window + mentions over the 2-day lookback ---
    slack_auth = cur.execute(
        "SELECT actor,ts,channel_id,thread_ts,substr(body,1,260),subject FROM events "
        "WHERE source='slack' AND ts>=? AND ts<? ORDER BY ts", (W0, W1)).fetchall()
    # FULL body (no substr) — these rows feed regex/mention DETECTION (mtok / ASK_RE /
    # NOISE_RE / subteam tokens), and a matcher MUST see the whole message. Truncating
    # before the regex silently drops asks whose trigger words sit past the cut — a real
    # message often opens with a ping + a long cc-list of @mentions + a tracker URL and
    # only says "Action Items / please review" much later (validated 2026-06-22: an EM
    # "CI Gating Beta Rollout" ping of @tech-managers was missed at a 260-char cut). The
    # same truncation also hid the owner/member being @-mentioned in a trailing `cc:` line.
    # Display always trims in Python (snip = …[:N]); never trim in SQL that feeds a matcher.
    slack_recent = cur.execute(
        "SELECT ts,channel_id,thread_ts,actor,body,subject FROM events "
        "WHERE source='slack' AND ts>=? AND ts<? AND body LIKE '%<@%'", (WL, W1)).fetchall()
    # OWNER-scoped slack over the LONGER lookback (WLo), incl. subteam pings (<!subteam^)
    # not just direct <@uid> — the owner queue must catch group-handle escalations too.
    slack_owner_recent = cur.execute(
        "SELECT ts,channel_id,thread_ts,actor,body,subject FROM events "
        "WHERE source='slack' AND ts>=? AND ts<? "
        "AND (body LIKE '%<@%' OR body LIKE '%<!subteam^%')", (WLo, W1)).fetchall()

    def hhmm(ts): return ts[11:16] if len(ts) > 15 else ts

    def slack_link(subject):
        # subject = slack:{channel}:{message_ts} → permalink (works for root + reply).
        try:
            _, ch, mts = subject.split(":", 2)
            return slack_permalink(ch, mts)
        except Exception:
            return ""

    out = []
    out.append(f"# STANDUP GATHER  date={date_str}  scope={scope}  windowUTC=[{W0} .. {W1})")
    out.append(f"# roster(reports)={members}")
    out.append("# RULES: credit work to OWNER=dev (assignee while In Progress), NOT the transitioner or reviewer. IR=own work in review (reviewer shown); REVIEWING=member reviewing another dev's ticket. CMR by latest assignee. Cap inprog~5, todo~5. Exclude Epics. Enrich+link in formatting.")

    # DATA FRESHNESS — guard against silent-stale ingest. If a source's newest event
    # predates the window end, the digest for this day is built on incomplete data;
    # the skill MUST surface this rather than report empty work as "quiet".
    def _to_dt(ts):
        if ts is None:
            return None
        s = str(ts)
        try:
            if s.replace(".", "", 1).isdigit():       # slack epoch float
                return datetime.datetime.fromtimestamp(float(s), datetime.timezone.utc).replace(tzinfo=None)
            return datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None
    w1_dt = datetime.datetime.strptime(W1, "%Y-%m-%dT%H:%M:%S")
    now_dt = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    fresh_rows, stale_any = [], False
    for src, in [("jira",), ("confluence",), ("github",), ("slack",)]:
        row = cur.execute("SELECT MAX(ts) FROM events WHERE source=?", (src,)).fetchone()
        newest = _to_dt(row[0] if row else None)
        if newest is None:
            fresh_rows.append(f"  {src:11} newest=NONE  ⚠️ NO DATA"); stale_any = True
            continue
        age_h = (now_dt - newest).total_seconds() / 3600.0
        stale = newest < w1_dt
        if stale:
            stale_any = True
        fresh_rows.append(
            f"  {src:11} newest={newest.strftime('%Y-%m-%dT%H:%M')}Z  age={age_h:.0f}h"
            + ("  ⚠️ STALE — before window end; data incomplete for this day" if stale else "  ok"))
    out.append(f"# DATA FRESHNESS (vs window end {W1}Z){'  ⚠️ STALE SOURCES PRESENT' if stale_any else ''}")
    out.extend(fresh_rows)
    out.append("# ONCALL (live Opsgenie, config/oncall.yaml)")
    out.extend(gather_oncall(roster))
    out.append("# LEAVES (team_leaves overlapping day + upcoming 14d; LIVE-SIGNAL = slack scan lookback..window-end)")
    out.extend(gather_leaves(cur, roster, date_str, W1, WL))
    out.append("# ONCALL FORECAST (14d rolling = 1 sprint; per-day primary via on-calls?date=)")
    fc_lines, forecast = gather_oncall_forecast(roster, date_str)
    out.extend(fc_lines)
    out.append("# RISKS (14d rolling: LEAVE×ONCALL collisions + COVERAGE gaps; surface in Day update §A)")
    out.extend(gather_risks(cur, roster, date_str, forecast))
    # PR INDEX — copy these descriptors VERBATIM when rendering any PR; never guess.
    out.append("# PR INDEX (deterministic: author=pr_opened actor→canonical, title+desc from events.db — COPY VERBATIM, never re-derive)")
    for n in sorted(pr_index, key=lambda x: int(x) if x.isdigit() else 0):
        sub, author, title, desc = pr_index[n]
        line = f"  #{n} ({sub}) author={author} title=\"{title}\""
        if desc:
            line += f" :: {desc[:140]}"
        out.append(line)
    out.append("")

    for m in members:
        em, sl, jid, gh = emails[m], slids[m], jids[m], ghs[m]
        out.append(f"\n================ {m}  (email={em} slack={sl} gh={gh}) ================")

        # leave scan: did THIS member post a leave signal in window?
        leave = [a for a in slack_auth if a[0] == sl and LEAVE_RE.search(a[4] or "")]
        if leave:
            t = leave[0]
            out.append(f"LEAVE_SIGNAL: [{hhmm(t[1])}] ch={t[2]} :: {t[4][:120]}")

        # DONE (window): jira transitions to terminal where member is assignee-at-close + authored PR merges
        out.append("-- WINDOW jira (member as actor or assignee-at-close) --")
        for ts, et, sub, tost, actor, asg in win_jira:
            owner_now = state.get(sub, {}).get("owner")   # canonical (dev) or latest assignee
            if owner_now == m or actor == em:
                tag = "OWN" if owner_now == m else "byActor"
                out.append(f"  [{hhmm(ts)}] {et} {sub} ->{tost}  owner={nm(owner_now)} {tag} | {state.get(sub,{}).get('title','')}")
        out.append("-- WINDOW github --")
        for ts, et, sub, actor, ti in win_gh:
            if actor == gh:
                out.append(f"  [{hhmm(ts)}] {et} {sub} | {ti}")
        out.append("-- WINDOW confluence --")
        for ts, et, sub, actor in win_conf:
            if actor == jid:
                out.append(f"  [{hhmm(ts)}] {et} {sub} | {conf_titles.get(sub,'')}")

        # CURRENT BOARD state, non-Epic. Ownership (§3c):
        #   IP / IR(own work) → keyed by work OWNER (dev), not latest assignee.
        #   REVIEWING         → keyed by reviewer (member reviewing another dev's ticket).
        #   CMR               → keyed by latest assignee (no dev/review semantics).
        ip, ir, td, cmr = [], [], [], []
        for sub in by_owner.get(m, []):
            d = state[sub]; st = d.get("status"); stt = d.get("state"); it = d.get("type"); ti = d.get("title", "")
            if it in ("Epic", "CMR"):
                continue
            if stt == "in_progress":
                ip.append((sub, it, st, ti))
            elif stt in ("in_review_active", "in_review_awaiting_reviewer"):
                ir.append((sub, it, ti, d.get("reviewer")))   # own work, in review
            elif st in TODO:
                td.append((sub, it, ti))
        reviewing = []
        for sub in by_reviewer.get(m, []):
            d = state[sub]; it = d.get("type"); ti = d.get("title", "")
            if it in ("Epic", "CMR"):
                continue
            reviewing.append((sub, it, ti, d.get("dev")))
        for sub in by_assignee.get(m, []):
            d = state[sub]
            if d.get("type") == "CMR" and d.get("status") not in CMR_CLOSED:
                cmr.append((sub, d.get("status"), d.get("title", "")))
        out.append(f"-- BOARD now: inprog={len(ip)} inReview(own)={len(ir)} reviewing={len(reviewing)} todo={len(td)} openCMR={len(cmr)} --")
        for x in ip: out.append(f"  IP   {x[0]} {x[1]}/{x[2]} | {x[3]}")
        for x in ir:
            tail = f"reviewer={nm(x[3])}" if x[3] else "awaiting reviewer"
            out.append(f"  IR   {x[0]} {x[1]}/In Review ({tail}) | {x[2]}")
        for x in reviewing:
            out.append(f"  REVIEWING {x[0]} {x[1]} (dev={nm(x[3])}) | {x[2]}")
        for x in cmr: out.append(f"  CMR  {x[0]} {x[1]} | {x[2]}")
        for x in td[:8]: out.append(f"  TODO {x[0]} {x[1]} | {x[2]}")
        if len(td) > 8: out.append(f"  TODO (+{len(td)-8} more)")

        # SLACK authored in window
        auth = [a for a in slack_auth if a[0] == sl]
        out.append(f"-- SLACK authored in window ({len(auth)}) --")
        for actor, ts, ch, thr, body, subj in auth:
            out.append(f"  [{hhmm(ts)}] ch={ch} thr={thr} link={slack_link(subj)} :: {body}")
        # SLACK asks: mentions of member, UNANSWERED-by-member only (open pickup candidates).
        # answered = member authored in the same thread (any time) → drop, it's handled.
        mtok = f"<@{sl}"
        answered_threads = {a[3] for a in slack_auth if a[0] == sl and a[3]}
        asks = []
        for ts, ch, thr, actor, body, subj in slack_recent:
            b = body or ""
            if mtok not in b or actor == sl:
                continue
            if thr and thr in answered_threads:
                continue  # member replied in-thread → answered
            if NOISE_RE.search(b):
                continue  # approval-bot / auto-resolve / stats noise
            if not ASK_RE.search(b):
                continue  # not a real ask: no question / imperative → likely FYI or cc
            asks.append((ts, ch, thr, actor, b, subj))
        out.append(f"-- SLACK open @-asks (unanswered, 2d->window-end) ({len(asks)}) --")
        for ts, ch, thr, actor, body, subj in asks[-6:]:
            snip = re.sub(r"\s+", " ", body or "")[:120]
            out.append(f"  [{ts[:16]}] ch={ch} thr={thr} from={actor} link={slack_link(subj)} :: {snip}")

    # ---- OWNER FOCUS: what the MANAGER personally needs to action / know ----
    # Emitted for every scope (the owner reads this even on a `team` run). Feeds the
    # 📅 Day update (§7a — DAY SIGNALS) + ⚠️ Your queue (§7b — reply-pending slack asks
    # incl. subteam pings over a 5-day lookback, confluence @-mentions, board decisions).
    o = roster.get(owner, {})
    o_sl, o_em, o_jid = o.get("slack_id", ""), o.get("email", ""), o.get("jira_id", "")
    out.append(f"\n================ OWNER FOCUS  (manager={owner} slack={o_sl}) ================")

    # (A) @-asks directed at the owner, UNANSWERED by the owner in-thread = reply pending.
    # Matches a DIRECT <@owner> mention OR a ping of a subteam the owner belongs to
    # (owner_subteam_ids — team handle + incident-commander group). "answered" = owner
    # authored any message in that thread over the owner lookback window (WLo).
    o_answered = {r[0] for r in cur.execute(
        "SELECT DISTINCT thread_ts FROM events WHERE source='slack' AND actor=? "
        "AND ts>=? AND ts<? AND thread_ts IS NOT NULL", (o_sl, WLo, W1)).fetchall()}
    o_mtok = f"<@{o_sl}"
    o_subteams = owner_subteams()  # token -> tier ('managerial' | 'dev')
    o_asks = []
    for ts, ch, thr, actor, body, subj in slack_owner_recent:
        b = body or ""
        if actor == o_sl:
            continue
        direct = o_mtok in b
        matched_tiers = [tier for tok, tier in o_subteams.items() if tok in b]
        via_subteam = bool(matched_tiers)
        if not (direct or via_subteam):
            continue
        if thr and thr in o_answered:
            continue
        if NOISE_RE.search(b) or not ASK_RE.search(b):
            continue
        # direct <@owner> OR a managerial-group ping = owner's own reply; a DEV-group ping
        # (no managerial/direct) = route-to-dev. Managerial wins when both are pinged.
        if direct or "managerial" in matched_tiers:
            how = "direct" if direct else "subteam-mgr"
        else:
            how = "subteam-dev"
        o_asks.append((ts, ch, thr, actor, b, subj, how))
    out.append(f"-- OWNER @-asks (your reply pending = direct/subteam-mgr; subteam-dev = route to a dev; {OWNER_ASK_LOOKBACK_DAYS}d->window-end) ({len(o_asks)}) --")
    for ts, ch, thr, actor, body, subj, how in o_asks[-15:]:
        snip = re.sub(r"\s+", " ", body or "")[:160]
        out.append(f"  [{ts[:16]}] via={how} ch={ch} thr={thr} from={actor} link={slack_link(subj)} :: {snip}")

    # (A2) Confluence @-mentions of the owner (modern mentions store ri:account-id=<acct>;
    # legacy ri:userkey mentions can't be matched without the owner's userkey and are
    # skipped). A comment by someone else that tags the owner on a doc = reply likely due.
    o_conf = []
    if o_jid:
        for ts, subj, body, actor in cur.execute(
                "SELECT ts,subject,body,actor FROM events WHERE source='confluence' "
                "AND event_type='comment' AND ts>=? AND ts<? ORDER BY ts", (WLo, W1)):
            b = body or ""
            if o_jid in b and actor != o_jid:
                title = conf_titles.get(subj, subj)
                pid = subj.split(":", 1)[1] if ":" in subj else subj
                snip = re.sub(r"<[^>]+>", " ", b)
                snip = re.sub(r"\s+", " ", snip)[:140].strip()
                o_conf.append((ts, pid, title, snip))
    out.append(f"-- OWNER confluence @-mentions (reply likely due, {OWNER_ASK_LOOKBACK_DAYS}d->window-end) ({len(o_conf)}) --")
    for ts, pid, title, snip in o_conf[-12:]:
        out.append(f"  [{ts[:16]}] page={pid} \"{title}\" :: {snip}")

    # (B) owner board items needing a decision: open CMRs (approve/execute) + In-Review.
    o_cmr, o_ir = [], []
    for sub in by_assignee.get(owner, []):
        d = state[sub]; st = d.get("status"); stt = d.get("state"); it = d.get("type"); ti = d.get("title", "")
        if it == "Epic":
            continue
        if it == "CMR" and st not in CMR_CLOSED:
            o_cmr.append((sub, st, ti))
        elif stt in ("in_review_active", "in_review_awaiting_reviewer"):
            o_ir.append((sub, it, st, ti))
    out.append(f"-- OWNER board needing action: openCMR={len(o_cmr)} inReview={len(o_ir)} --")
    for x in o_cmr: out.append(f"  CMR {x[0]} {x[1]} | {x[2]}")
    for x in o_ir: out.append(f"  IR  {x[0]} {x[1]}/{x[2]} | {x[3]}")

    # (C) DAY SIGNALS for the info-dump: releases/CMRs moved in window + leave/beta callouts.
    #     (Model synthesises the prose dump from these + the per-member blocks above.)
    rel = []
    for ts, et, sub, tost, actor, asg in win_jira:
        d = state.get(sub, {})
        if d.get("type") == "CMR" or (tost and any(k in (tost or "") for k in ("Released", "Pending Release", "Change Approved"))):
            rel.append((hhmm(ts), sub, tost, d.get("type", ""), d.get("title", "")))
    out.append(f"-- DAY SIGNALS: release/CMR transitions in window ({len(rel)}) --")
    seen = set()
    for hh, sub, tost, it, ti in rel:
        k = (sub, tost)
        if k in seen:
            continue
        seen.add(k)
        out.append(f"  [{hh}] {sub} ->{tost} ({it}) | {ti}")
    # beta / prod release announcements in window slack (any roster author)
    REL_RE = re.compile(r"\b(beta release|releasing|deployed to|rolling out|rolled out|prod release|going live|go-live|hotfix)\b", re.I)
    rel_msgs = [a for a in slack_auth if REL_RE.search(a[4] or "")]
    out.append(f"-- DAY SIGNALS: release/deploy slack callouts ({len(rel_msgs)}) --")
    for actor, ts, ch, thr, body, subj in rel_msgs[:8]:
        snip = re.sub(r"\s+", " ", body or "")[:140]
        out.append(f"  [{hhmm(ts)}] by={actor} ch={ch} link={slack_link(subj)} :: {snip}")

    print("\n".join(out))


if __name__ == "__main__":
    main()

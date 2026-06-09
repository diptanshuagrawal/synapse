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


def ist_window(date_str):
    """IST day -> UTC ISO bounds (naive, comparable to stored ts[:19])."""
    y, m, dd = map(int, date_str.split("-"))
    ist0 = datetime.datetime(y, m, dd, 0, 0)
    u0 = ist0 - datetime.timedelta(hours=5, minutes=30)
    u1 = u0 + datetime.timedelta(days=1)
    u7 = u0 - datetime.timedelta(days=6)  # 7d-back for open asks
    f = lambda x: x.strftime("%Y-%m-%dT%H:%M:%S")
    return f(u0), f(u1), f(u7)


def main():
    if len(sys.argv) < 2:
        print("usage: standup_gather.py <YYYY-MM-DD> [scope]"); sys.exit(1)
    date_str = sys.argv[1]
    scope = sys.argv[2] if len(sys.argv) > 2 else "team"
    W0, W1, W7 = ist_window(date_str)
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

    # --- ONE PASS: latest state per jira subject ---
    state = {}
    for sub, et, ts, assignee, to_status, it, title in cur.execute(
        "SELECT subject,event_type,ts,assignee,to_status,issue_type,title "
        "FROM events WHERE source='jira' ORDER BY ts"):
        d = state.setdefault(sub, {})
        if assignee: d["assignee"] = assignee
        if to_status: d["status"] = to_status
        if it: d["type"] = it
        if et == "issue_created" and "title" not in d:
            d["title"] = (title or "")[:90]
    # index subjects by assignee
    by_assignee = {}
    for sub, d in state.items():
        a = d.get("assignee")
        if a:
            by_assignee.setdefault(a, []).append(sub)

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
    # --- SLACK authored in window + mentions in 7d window ---
    slack_auth = cur.execute(
        "SELECT actor,ts,channel_id,thread_ts,substr(body,1,260),subject FROM events "
        "WHERE source='slack' AND ts>=? AND ts<? ORDER BY ts", (W0, W1)).fetchall()
    slack_recent = cur.execute(
        "SELECT ts,channel_id,thread_ts,actor,substr(body,1,260),subject FROM events "
        "WHERE source='slack' AND ts>=? AND ts<? AND body LIKE '%<@%'", (W7, W1)).fetchall()

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
    out.append("# RULES: credit by assignee (shown), not transitioner. Cap inprog~5, todo~5. Exclude Epics. Enrich+link in formatting.\n")

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
            owner_now = state.get(sub, {}).get("assignee")
            if owner_now == em or actor == em:
                tag = "OWN" if owner_now == em else "byActor"
                out.append(f"  [{hhmm(ts)}] {et} {sub} ->{tost}  assignee={owner_now} {tag} | {state.get(sub,{}).get('title','')}")
        out.append("-- WINDOW github --")
        for ts, et, sub, actor, ti in win_gh:
            if actor == gh:
                out.append(f"  [{hhmm(ts)}] {et} {sub} | {ti}")
        out.append("-- WINDOW confluence --")
        for ts, et, sub, actor in win_conf:
            if actor == jid:
                out.append(f"  [{hhmm(ts)}] {et} {sub} | {conf_titles.get(sub,'')}")

        # CURRENT BOARD state (assignee=member), non-Epic
        ip, td, cmr = [], [], []
        for sub in by_assignee.get(em, []):
            d = state[sub]; st = d.get("status"); it = d.get("type"); ti = d.get("title", "")
            if it == "Epic":
                continue
            if it == "CMR":
                if st not in CMR_CLOSED:
                    cmr.append((sub, st, ti))
            elif st in INPROG:
                ip.append((sub, it, st, ti))
            elif st in TODO:
                td.append((sub, it, ti))
        out.append(f"-- BOARD now: inprog={len(ip)} todo={len(td)} openCMR={len(cmr)} --")
        for x in ip: out.append(f"  IP  {x[0]} {x[1]}/{x[2]} | {x[3]}")
        for x in cmr: out.append(f"  CMR {x[0]} {x[1]} | {x[2]}")
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
        out.append(f"-- SLACK open @-asks (unanswered, 7d->window-end) ({len(asks)}) --")
        for ts, ch, thr, actor, body, subj in asks[-6:]:
            snip = re.sub(r"\s+", " ", body or "")[:120]
            out.append(f"  [{ts[:16]}] ch={ch} thr={thr} from={actor} link={slack_link(subj)} :: {snip}")

    print("\n".join(out))


if __name__ == "__main__":
    main()

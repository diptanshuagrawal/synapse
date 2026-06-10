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
    # open-ask / owner-ask lookback = past 2 days only (window day + 1 prior).
    # A longer window re-surfaces the same stale asks on every daily run.
    uL = u0 - datetime.timedelta(days=1)
    f = lambda x: x.strftime("%Y-%m-%dT%H:%M:%S")
    return f(u0), f(u1), f(uL)


def main():
    if len(sys.argv) < 2:
        print("usage: standup_gather.py <YYYY-MM-DD> [scope]"); sys.exit(1)
    date_str = sys.argv[1]
    scope = sys.argv[2] if len(sys.argv) > 2 else "team"
    W0, W1, WL = ist_window(date_str)  # WL = 2-day-back lookback for asks
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
    # --- SLACK authored in window + mentions over the 2-day lookback ---
    slack_auth = cur.execute(
        "SELECT actor,ts,channel_id,thread_ts,substr(body,1,260),subject FROM events "
        "WHERE source='slack' AND ts>=? AND ts<? ORDER BY ts", (W0, W1)).fetchall()
    slack_recent = cur.execute(
        "SELECT ts,channel_id,thread_ts,actor,substr(body,1,260),subject FROM events "
        "WHERE source='slack' AND ts>=? AND ts<? AND body LIKE '%<@%'", (WL, W1)).fetchall()

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
    out.append("# RULES: credit work to OWNER=dev (assignee while In Progress), NOT the transitioner or reviewer. IR=own work in review (reviewer shown); REVIEWING=member reviewing another dev's ticket. CMR by latest assignee. Cap inprog~5, todo~5. Exclude Epics. Enrich+link in formatting.\n")

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
    # "Needs your attention" + "For your day" sections (§7b/§7c of the skill).
    o = roster.get(owner, {})
    o_sl, o_em, o_jid = o.get("slack_id", ""), o.get("email", ""), o.get("jira_id", "")
    out.append(f"\n================ OWNER FOCUS  (manager={owner} slack={o_sl}) ================")

    # (A) @-asks directed at the owner, UNANSWERED by the owner in-thread = reply pending.
    # "answered" = owner authored any message in that thread over the 2-day lookback.
    o_answered = {r[0] for r in cur.execute(
        "SELECT DISTINCT thread_ts FROM events WHERE source='slack' AND actor=? "
        "AND ts>=? AND ts<? AND thread_ts IS NOT NULL", (o_sl, WL, W1)).fetchall()}
    o_mtok = f"<@{o_sl}"
    o_asks = []
    for ts, ch, thr, actor, body, subj in slack_recent:
        b = body or ""
        if o_mtok not in b or actor == o_sl:
            continue
        if thr and thr in o_answered:
            continue
        if NOISE_RE.search(b) or not ASK_RE.search(b):
            continue
        o_asks.append((ts, ch, thr, actor, b, subj))
    out.append(f"-- OWNER @-asks (your reply pending, 2d->window-end) ({len(o_asks)}) --")
    for ts, ch, thr, actor, body, subj in o_asks[-12:]:
        snip = re.sub(r"\s+", " ", body or "")[:160]
        out.append(f"  [{ts[:16]}] ch={ch} thr={thr} from={actor} link={slack_link(subj)} :: {snip}")

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

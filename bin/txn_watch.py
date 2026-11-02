#!/usr/bin/env python3
"""txn_watch.py — auto-run the txn-status-summary skill on oncall txn escalations.

  --once         : one scan of the oncall channel: txn skill cards only (LaunchAgent, every 2 min).
  --status       : status job: auto-acknowledge + LLM closure loop (LaunchAgent, every 10 min).
  --once --dry   : same, but print the cards instead of posting / saving state.
  --issue <link> : run one escalation by its oncall-thread link (dry; for testing).

Flow: the issue bot posts "New Issue Reported … <!subteam^…> … <link|Issue Link>" in the
oncall channel. For each new top-level message tagging a trigger subteam we read the
linked source thread (+ the oncall thread's own replies), pull out an RRN or src_txn_id,
run the skill's lookup chain across the configured upstreams and post a summary
card in the oncall thread with Approve / Reject buttons. bin/relay_bot.py handles the
clicks (txn: action ids): Approve posts the final reply on the original (source) thread, Reject dismisses.

Output is PII-safe by construction: account ids, balances and payee names never leave
this script (only status, internal ids, amounts, dates and the narration prefix do).

Config: work-context/config/txn_watch.yaml. State: work-context/state/txn_watch.json.
Astra creds are the skill's own (~/.claude/astra-secret.env); never read here.
"""
import json, os, re, subprocess, sys, time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "work-context/config/txn_watch.yaml")
STATE = os.path.join(ROOT, "work-context/state/txn_watch.json")
SECRETS = os.path.expanduser("~/.secrets")
IST = timezone(timedelta(hours=5, minutes=30))

LINK_RE = re.compile(r"archives/(C\w+)/p(\d{10})(\d{6})(?:\?thread_ts=(\d+\.\d+))?[^|>]*\|Issue Link")
RRN_RE = re.compile(r"(?<![\d.])\d{12}(?![\d.])")
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
TICKET_UUID_RE = re.compile(r"UUID:?\*?:?\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
DONE_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check", "done", "resolved",
                  "check", "white_tick", "green_tick", "closed"}
DONE_RE = re.compile(r"\b(resolved|refunded|been reversed|credited back|closing this|closed|fixed|not our issue)\b", re.I)
SETUP_ERR_RE = re.compile(r"SETUP_NEEDED|expired|401|not a JWT|missing ASTRA", re.I)


def load_cfg():
    import yaml
    return yaml.safe_load(open(CONFIG))


# The three upstreams this walks are deployment-specific: their names, and the names of
# the gold tables the skill reports under, say which bank this is. They were written into
# the strings, which made an otherwise generic tool unpublishable. They come from config
# now, with neutral fallbacks so the module still reads sensibly without one.
SYS_FALLBACK = {"origin": "origin", "ledger": "ledger", "core": "core"}


def systems(cfg=None):
    """Display names for the upstreams, in the order money moves through them."""
    try:
        # `cfg is None` means "read it", not `{}` — an empty config passed in is an
        # answer, and treating it as absent made the fallbacks untestable.
        s = (((load_cfg() if cfg is None else cfg).get("systems") or {}).get("labels")) or {}
    except Exception:
        s = {}
    return {k: str(s.get(k) or v) for k, v in SYS_FALLBACK.items()}


def block_keys(cfg=None):
    """The skill's block titles mapped to stable keys. The titles name real schemas, so
    the mapping is configured rather than carried here."""
    try:
        return (((load_cfg() if cfg is None else cfg).get("systems") or {}).get("blocks")) or {}
    except Exception:
        return {}


def secret(name):
    return open(os.path.join(SECRETS, name)).read().strip()


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"last_ts": None, "issues": {}, "alerted_day": ""}


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)


def update_issue(key, **fields):
    """Read-modify-write one issue entry (the listener also writes here on clicks)."""
    st = load_state()
    st["issues"].setdefault(key, {}).update(fields)
    save_state(st)


# ---------- Oncall Reporter status API ----------
# The host is internal, so it comes from config like everything else that names the
# deployment. Blank means the status API is simply not wired up here.
def oncall_api(cfg=None):
    c = load_cfg() if cfg is None else cfg
    return str((c.get("oncall_api") or {}).get("base") or "").rstrip("/")



CLOSING_EMOJI = ("✅", "⚪")    # refunded / debit stands: the question is answered


CLOSED_STATUSES = ("RESOLVED", "NOT_OUR_ISSUE", "FYI")


def _oncall_get(params):
    import requests
    r = requests.get(oncall_api(), timeout=30, params=params,
                     headers={"Authorization": f"Bearer {secret('oncall_reporter_api_key')}", "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reporter_issues(team, since_epoch, statuses=("OPEN",), until_epoch=None, max_pages=5):
    """Reporter's issues for `team` reported in [since, until], following the cursor."""
    out, cursor = [], None
    for _ in range(max_pages):
        p = [("team_oncall_handle", team), ("reported_from", _iso(since_epoch)), ("limit", "100")]
        p += [("status", s) for s in statuses]
        if until_epoch:
            p.append(("reported_to", _iso(until_epoch)))
        if cursor:
            p.append(("cursor", cursor))
        d = _oncall_get(p)
        out += d.get("data", [])
        cursor = (d.get("meta") or {}).get("next_cursor")
        if not (d.get("meta") or {}).get("has_more"):
            break
    return out


def issue_status(issue_ts, channel, team):
    """Current Reporter status of one issue. No single-issue GET exists, and the list filters on
    the SOURCE message's time (often hours or days before the oncall post), so look back from
    the oncall post: a day first, then 30 days."""
    key = f"{channel}_{issue_ts}"
    allst = ("OPEN", "ACKNOWLEDGED", "BLOCKED", "RESOLVED", "FYI", "NOT_OUR_ISSUE")
    for back in (86400, 30 * 86400):
        for it in reporter_issues(team, float(issue_ts) - back, allst, float(issue_ts) + 120, 10):
            if it.get("issue_id") == key:
                return it.get("status")
    return None


# ---------- who was on call (Opsgenie) ----------
_PERIODS = {"since": None, "rows": []}


def oncall_periods(cfg, since_epoch):
    """[(start, end, email)] from the schedule's final timeline (overrides included), fetched in
    ONE call covering since_epoch → now and cached per process. Per-reply on-calls lookups
    hit Opsgenie's rate limit (429) on any real sweep."""
    import requests
    if _PERIODS["since"] is not None and _PERIODS["since"] <= since_epoch:
        return _PERIODS["rows"]
    og = cfg.get("opsgenie") or {}
    start = since_epoch - 7 * 86400
    weeks = int((time.time() - start) // (7 * 86400)) + 2
    r = requests.get(f"https://api.opsgenie.com/v2/schedules/{og['schedule']}/timeline", timeout=30,
                     headers={"Authorization": f"GenieKey {secret(og.get('api_key_secret', 'opsgenie_api_key'))}"},
                     params={"identifierType": og.get("identifier_type", "name"), "interval": str(weeks),
                             "intervalUnit": "weeks", "date": datetime.fromtimestamp(start, IST).isoformat()})
    r.raise_for_status()
    rows = []
    for rot in r.json()["data"]["finalTimeline"].get("rotations") or []:
        for p in rot.get("periods") or []:
            who = ((p.get("recipient") or {}).get("name") or "").lower()
            if who:
                rows.append((datetime.fromisoformat(p["startDate"].replace("Z", "+00:00")).timestamp(),
                             datetime.fromisoformat(p["endDate"].replace("Z", "+00:00")).timestamp(), who))
    _PERIODS.update(since=start, rows=rows)
    return rows


def was_oncall(email, epoch, cfg, issue_epoch=None):
    """True if `email` counts as the oncall for a reply at `epoch`.
    Handover overlap, two rules:
      1. on shift at `epoch`, widened to `handover_lead_minutes` before the shift starts and
         `handover_tail_hours` after it ends (the outgoing oncall still finishing up);
      2. (only with `issue_owner_unlimited: true`) issue owner: whoever was on call when the
         issue was raised (`issue_epoch`) keeps counting on THAT issue until it closes."""
    if not email:
        return False
    og = cfg.get("opsgenie") or {}
    lead = og.get("handover_lead_minutes", 120) * 60
    tail = og.get("handover_tail_hours", 24) * 3600
    since = min(epoch, issue_epoch or epoch)
    periods = [(s, e) for s, e, who in oncall_periods(cfg, since) if who == email.lower()]
    if any(s - lead <= epoch <= e + tail for s, e in periods):
        return True
    if issue_epoch is None or not og.get("issue_owner_unlimited", False):
        return False                  # owner rule off: the 24h windows above cover handovers
    return any(s - lead <= issue_epoch <= e for s, e in periods)


RAISED_BY_RE = re.compile(r"Raised By:?\*?:?\s*<@U\w+")
SLACK_EVENTS = (("Issue Reopened at", "OPEN"), ("Issue Resolved at", "RESOLVED"),
                ('"Not Our Issue"', "NOT_OUR_ISSUE"), ("FYI Message acknowledged", "FYI"),
                ("Issue Blocked", "BLOCKED"), ("Issue acknowledged at", "ACKNOWLEDGED"))


def slack_status(msg):
    """Status as shown on the Reporter's own Slack message: its context lines are a
    chronological event log, so the last recognised event wins. None = no events (OPEN).
    The API has been seen returning OPEN for issues Slack shows acked + resolved."""
    last = None
    for b in msg.get("blocks", []):
        if b.get("type") != "context":
            continue
        for e in b.get("elements", []):
            t = e.get("text", "")
            for needle, st in SLACK_EVENTS:
                if needle in t:
                    last = st
                    break
    return last


def set_issue_status(issue_ts, status, actor, channel=None, team=None):
    """PATCH the Reporter's status for the issue posted at `issue_ts` in the oncall channel.
    status ∈ ACKNOWLEDGED | RESOLVED | BLOCKED | NOT_OUR_ISSUE; actor = free-text (email).
    Never touches an issue that is already closed, or re-sets the status it already has.
    Returns (ok, note)."""
    import requests
    cfg = load_cfg()
    channel = channel or cfg["slack"]["channel_id"]
    team = team or cfg["slack"]["trigger_subteams"][0]
    try:
        cur = issue_status(issue_ts, channel, team)
    except Exception as e:
        return False, f"status lookup failed: {e}"
    try:   # trust the more advanced of API and Slack: the API can lag behind button clicks
        from slack_sdk import WebClient
        m = WebClient(token=secret("relay_slack_bot_token")).conversations_history(
            channel=channel, latest=issue_ts, inclusive=True, limit=1)["messages"][0]
        shown = slack_status(m)
    except Exception:
        shown = None
    if shown in CLOSED_STATUSES or (shown and cur in (None, "OPEN")):
        cur = shown
    if cur in CLOSED_STATUSES:
        return False, f"left as {cur} (already closed)"
    if cur == status or (status == "ACKNOWLEDGED" and cur == "BLOCKED"):
        return True, f"already {cur}"
    return patch_status(issue_ts, status, actor, channel)


def patch_status(issue_ts, status, actor, channel):
    """Unguarded PATCH. Use set_issue_status unless deliberately correcting a closed issue."""
    import requests
    tok = secret("oncall_reporter_api_key")
    try:
        r = requests.patch(f"{oncall_api()}/{channel}_{issue_ts}/status", timeout=30,
                           headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
                           json={"status": status, "actor": actor})
    except Exception as e:
        return False, f"status API unreachable: {e}"
    return r.ok, (status if r.ok else f"HTTP {r.status_code}: {r.text[:200]}")


def closing_status(reply):
    """Fallback label for cards posted before labels existed: answered → NOT_OUR_ISSUE
    (our systems worked, the reply gives clarity), else ACKNOWLEDGED."""
    return "NOT_OUR_ISSUE" if (reply or "").startswith(CLOSING_EMOJI) else "ACKNOWLEDGED"


# ---------- id extraction ----------
def thread_msgs(client, channel, ts):
    """Parent + replies of a thread; [] when the bot can't read it."""
    try:
        return client.conversations_replies(channel=channel, ts=ts, limit=200).get("messages", [])
    except Exception as e:
        print(f"read {channel}/{ts} failed: {getattr(e, 'response', {}).get('error', e)}", file=sys.stderr)
        return []


def ensure_member(client, channel, allowed):
    """Join `channel` if needed so its thread can be read. `allowed` = ["*"] means any
    public channel; private ones need a manual /invite."""
    if "*" not in allowed and channel not in allowed:
        return False
    try:
        info = client.conversations_info(channel=channel)["channel"]
        if not info.get("is_member") and not info.get("is_private"):
            client.conversations_join(channel=channel)
        return True
    except Exception as e:
        print(f"join {channel} failed: {e}", file=sys.stderr)
        return False


def extract_ids(text):
    """Distinct RRNs first (most reliable key), then uuids; order of appearance.
    CX ticket forms carry their own `*UUID:* <uuid>` field; that's the ticket, not a txn."""
    seen = {u.lower() for u in TICKET_UUID_RE.findall(text or "")}
    out = []
    for kind, rx in (("rrn", RRN_RE), ("src", UUID_RE)):
        for v in rx.findall(text or ""):
            v = v.lower()
            if v not in seen:
                seen.add(v); out.append((kind, v))
    return out[:5]


# ---------- skill run + parse ----------
def run_chain(cfg, kind, value):
    """Returns (blocks: {title: [row dicts]}, error or None). error 'not_found' = no TO row."""
    py = os.path.expanduser(cfg["skill"]["python"])
    script = os.path.expanduser(cfg["skill"]["script"])
    try:   # the skill's Astra poll has no timeout of its own; a stuck query must not wedge the watcher
        res = subprocess.run([py, "-W", "ignore", script, f"--{kind}", value],
                             capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {}, "astra query timed out after 300s"
    blocks, title, cols = {}, None, None
    for line in res.stdout.splitlines():
        if line.startswith("== "):
            title, cols = line[3:].replace(" (latest)", "").strip(), None
            blocks[title] = []
        elif title and line.strip() and line != "(no rows)":
            if cols is None:
                cols = line.split("\t")
            else:
                blocks[title].append(dict(zip(cols, line.split("\t"))))
    err = (res.stderr or "").strip()
    if res.returncode != 0:
        if "no casa transaction found" in err:
            return blocks, "not_found"
        return blocks, err.splitlines()[-1] if err else f"exit {res.returncode}"
    return blocks, None


def ist(ts):
    """'2026-10-17 21:31:15.608000+00:00' (UTC) → '18 Oct 2026, 03:01 AM IST'."""
    if not ts:
        return "?"
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(IST)
        return dt.strftime("%d %b %Y, %I:%M %p IST")
    except ValueError:
        return ts


def day(ts):
    try:
        return datetime.strptime(ts[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except (ValueError, TypeError):
        return ts or "?"


def rupees(v):
    try:
        f = float(v)
        return f"₹{f:,.2f}".replace(".00", "")
    except (ValueError, TypeError):
        return f"₹{v}"


def narration_prefix(n):
    """'UPI-Reversal-628987974466-<payee>-…' → 'UPI-Reversal-628987974466' (drops the payee)."""
    return "-".join((n or "").split("-")[:3])


def _rows(blocks, table):
    return blocks.get(table) or []


def summarize(blocks):
    """Deterministic verdict + per-layer lines from the relabelled chain output (skill §4
    table). Returns dict(verdict, emoji, lines, cx_line, rrn). Never includes account,
    balance or payee name."""
    txns = _rows(blocks, "TO_TXN")
    states = _rows(blocks, "TO_STATE")
    manual = _rows(blocks, "TO_MANUAL")
    orders = _rows(blocks, "LCS_ORDERS")
    cbs = _rows(blocks, "CBS")
    t = txns[0] if txns else {}
    s = states[0] if states else {}
    rrn = t.get("rrn", "")
    debit_o = next((o for o in orders if o.get("nature") == "DEBIT"), None)
    credit_o = next((o for o in orders if o.get("nature") == "CREDIT"), None)
    cbs_d = next((r for r in cbs if r.get("txn_nature") == "D"), None)
    cbs_c = next((r for r in cbs if r.get("txn_nature") == "C"), None)
    status = s.get("txn_status", "")
    revert = s.get("revert_src_txn_id", "")

    amt = rupees(int(t["amount_in_paise"]) / 100) if t.get("amount_in_paise", "").isdigit() else "?"
    SY = systems()
    lines = [f"*{SY['origin']}:* {t.get('txn_mode','?')} {t.get('txn_type','').lower()} of {amt} on {ist(t.get('created_at'))}. "
             f"Status `{status or '?'}`" + (f", final `{s.get('final_status')}`" if s.get("final_status") else "") + "."]
    lines.append(f"   src_txn_id `{t.get('src_txn_id','?')}`" + (f" · revert `{revert}` at {ist(s.get('revert_at'))}" if revert else ""))
    if manual:
        lines.append(f"   manual reversal: `{manual[0].get('status')}` (attempts {manual[0].get('attempt_count')})")
    lo = []
    if debit_o:
        lo.append(f"debit order `{debit_o['id']}` {debit_o['order_status']} ({ist(debit_o.get('created_at'))})")
    if credit_o:
        lo.append(f"credit order `{credit_o['id']}` {credit_o['order_status']} ({ist(credit_o.get('created_at'))})")
    lines.append(f"*{SY['ledger']}:* " + ("; ".join(lo) if lo else "no orders found") + ".")
    lc = []
    for r in (cbs_d, cbs_c):
        if r:
            lc.append(f"{'debit' if r is cbs_d else 'credit'} {rupees(r.get('txn_amount'))} on {day(r.get('txn_date'))} "
                      f"(`{narration_prefix(r.get('narration'))}`, {'active' if r.get('is_active') == 'True' else 'INACTIVE'})")
    lines.append("*CBS ledger:* " + ("; ".join(lc) if lc else "no entries found") + ".")

    credit_ok = credit_o and credit_o.get("order_status") == "SUCCESS"
    cbs_c_ok = cbs_c and cbs_c.get("is_active") == "True" and cbs_c.get("activity_id") == "1002"
    cx = ""
    if status == "REVERT_SUCCESS" and credit_ok and cbs_c_ok:
        v, e = "Refunded: the reversal is in the customer's account", "✅"
        cx = (f"Ask the user to check the statement entry dated *{day(cbs_c.get('txn_date'))}*, narration "
              f"`{narration_prefix(cbs_c.get('narration'))}`. A statement downloaded before that date won't show it.")
    elif credit_ok and not cbs_c and not cbs_d:
        v, e = (f"Reversed in {SY['ledger']}, but neither the debit nor the credit is in "
                f"{SY['core']} gold ({SY['core']} may never have posted this instant reversal, "
                f"or gold is missing that day; needs a live {SY['core']} read)"), "🟡"
    elif credit_ok and not cbs_c:
        v, e = (f"Reversal done in {SY['ledger']}, but no {SY['core']} credit in gold yet "
                f"(lag or posting gap; needs a live {SY['core']} read)"), "🟡"
    elif status.startswith("REVERT_") or (revert and not credit_ok):
        stop = SY['origin'] if not credit_o else SY['ledger']
        v, e = f"Reversal stuck at {stop} (`{status}`)", "🔴"
    elif status == "SUCCESS" and not revert:
        v, e = "Debit stands: the txn succeeded and was not reversed (check the beneficiary side)", "⚪"
    else:
        v, e = f"Needs a look: TO status `{status or 'unknown'}`", "🟠"
    # Reporter label the card proposes. NOT_OUR_ISSUE = our systems worked as designed and the
    # reply only gives clarity (auto-refund, debit stands, beneficiary-side failure);
    # RESOLVED = we had to fix something (manual reversal); ACKNOWLEDGED = still needs work.
    if e == "✅":
        label = "RESOLVED" if manual else "NOT_OUR_ISSUE"
    elif e == "⚪":
        label = "NOT_OUR_ISSUE"
    else:
        label = "ACKNOWLEDGED"
    return {"verdict": v, "emoji": e, "lines": lines, "cx_line": cx, "rrn": rrn, "label": label}


def relabel(blocks):
    """Map txn_status.py block titles to stable keys."""
    names = block_keys()
    return {names.get(k, k): v for k, v in blocks.items()}


# ---------- card ----------
def final_reply(summ):
    head = f"{summ['emoji']} *{summ['verdict']}.*"
    body = "\n".join(f"• {l}" if not l.startswith("   ") else l for l in summ["lines"])
    out = f"{head}\n{body}"
    if summ["cx_line"]:
        out += f"\n\n{summ['cx_line']}"
    return out + "\n_Source: Astra gold lake (txn-status-summary skill)._"


def card_blocks(key, summ, id_used, other_ids):
    note = f"Looked up `{id_used}`" + (f" · also seen in thread: {', '.join(f'`{x}`' for x in other_ids)}" if other_ids else "")
    return [
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"🤖 *Auto-check (txn-status-summary)*: draft for oncall review\n\n{final_reply(summ)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": note + " · gold lags up to ~1 day"}]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            "Posting replies on the original thread and sets the oncall status. "
            "_Not our issue_ = our systems worked, the reply only gives clarity. _Resolved_ = we fixed something."}]},
        {"type": "actions", "block_id": f"txnact_{key}", "elements": approve_buttons(key, summ.get("label")) + [
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "✕ Reject"},
             "action_id": f"txn:{key}:reject", "value": key},
        ]},
    ]


LABEL_TEXT = {"NOT_OUR_ISSUE": "not our issue", "RESOLVED": "resolved", "ACKNOWLEDGED": "acknowledge"}


def approve_buttons(key, label):
    """Proposed label first (primary); for answered cases also offer the other closing label."""
    label = label or "ACKNOWLEDGED"
    labels = [label] + ([x for x in ("NOT_OUR_ISSUE", "RESOLVED") if x != label] if label != "ACKNOWLEDGED" else [])
    return [{"type": "button", **({"style": "primary"} if i == 0 else {}),
             "text": {"type": "plain_text", "text": f"{'✓ ' if i == 0 else ''}Post · {LABEL_TEXT[l]}"},
             "action_id": f"txn:{key}:approve:{l}", "value": key}
            for i, l in enumerate(labels)]


# ---------- scan ----------
def alert_owner(client, cfg, st, msg, dry):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if st.get("alerted_day") == today or not cfg["slack"].get("alert_user"):
        return
    print(f"ALERT: {msg}", file=sys.stderr)
    if not dry:
        client.chat_postMessage(channel=cfg["slack"]["alert_user"],
                                text=f"⚠️ txn_watch paused: {msg}\nRefresh the `ASTRA_TOKEN` line in `~/.claude/astra-secret.env`.")
        st["alerted_day"] = today


def source_of(msg):
    """(channel, thread_ts) of the issue's source thread, or None. The issue bot keeps the
    link in the text until the issue is acked, then moves it into a context block."""
    texts = [msg.get("text", "")] + [e.get("text", "") for b in msg.get("blocks", []) if b.get("type") == "context"
                                     for e in b.get("elements", [])]
    m = LINK_RE.search("\n".join(texts))
    return (m.group(1), m.group(4) or f"{m.group(2)}.{m.group(3)}") if m else None


def team_members(client, cfg):
    users = set()
    for sid in cfg["slack"].get("approver_subteams") or []:
        try:
            users |= set(client.usergroups_users_list(usergroup=sid)["users"])
        except Exception as e:
            print(f"usergroup {sid} lookup failed: {e}", file=sys.stderr)
    return users


def already_handled(msg, oncall_msgs, src_msgs, team):
    """Reason string when the issue looks handled even though the issue bot may still show
    it unacked; None otherwise. Signals, cheapest first:
      1. issue bot marked it Resolved / Not Our Issue
      2. a resolution reaction on the source parent or on a team reply
      3. a team reply with resolution wording
      4. the latest reply in the source thread is from the team (already answered)"""
    ctx = " ".join(e.get("text", "") for b in msg.get("blocks", []) if b.get("type") == "context"
                   for e in b.get("elements", []))
    if "Resolved at" in ctx or "Not Our Issue" in ctx:
        return "issue bot: resolved / not our issue"
    thread = src_msgs or oncall_msgs
    replies = [r for r in thread[1:] if not r.get("bot_id")]
    team_replies = [r for r in replies + oncall_msgs[1:] if r.get("user") in team]
    for r in thread[:1] + team_replies:
        if any(x["name"] in DONE_REACTIONS for x in r.get("reactions", [])):
            return f"resolution reaction on {'parent' if r is thread[0] else 'a team reply'}"
    for r in team_replies:
        if DONE_RE.search(r.get("text", "")):
            return "team reply says it's resolved"
    if replies and replies[-1].get("user") in team:
        return "team already answered on the source thread"
    return None


def process(client, cfg, msg, dry, team=None):
    """Run one escalation. Returns (status, card_or_reason_or_None)."""
    ch = cfg["slack"]["channel_id"]
    oncall_msgs = thread_msgs(client, ch, msg["ts"]) or [msg]
    src_msgs = []
    src = source_of(msg)
    if src and ensure_member(client, src[0], cfg["slack"].get("source_channels") or []):
        src_msgs = thread_msgs(client, *src)
    reason = already_handled(msg, oncall_msgs, src_msgs, team if team is not None else team_members(client, cfg))
    if reason:
        return "skipped", reason
    ids = extract_ids("\n".join(m.get("text", "") for m in oncall_msgs + src_msgs))
    if not ids:
        return "no_ids", None
    for kind, val in ids:
        blocks, err = run_chain(cfg, kind, val)
        if err == "not_found":
            continue
        if err:
            return ("setup" if SETUP_ERR_RE.search(err) else "error"), err
        summ = summarize(relabel(blocks))
        shown = final_reply(summ)
        others = [v for _, v in ids if v != val and v not in shown]
        return "ready", {"summary": summ, "id_used": val, "others": others}
    return "not_found", None


def is_fyi(src_msgs, team_subteam):
    """True when no PERSON raised this, i.e. a pure bot alert (recon, failure summary, test
    report). A person raises an issue by tagging our oncall group themselves, or through a
    ticket workflow that posts on their behalf ("Raised By: @someone")."""
    for r in src_msgs or []:
        text = r.get("text") or ""
        human = not r.get("bot_id") and r.get("subtype") != "bot_message" and r.get("user")
        if human and f"<!subteam^{team_subteam}" in text:
            return False
        if RAISED_BY_RE.search(text):
            return False
    return True


def ack_sweep(client, cfg, st, dry=False, force=False):
    """Mark OPEN issues ACKNOWLEDGED once someone who was ON CALL at the time has replied on the
    oncall thread or the source thread. A reply from any other dev doesn't count: the oncall
    may not know about it. "On call" is judged at the reply's own time (Opsgenie), with a
    handover grace window, so the outgoing oncall's reply still counts after a handover.
    Runs every `ack_sweep_minutes`; credits the acknowledgement to the replier."""
    lim = cfg.get("limits") or {}
    now = time.time()
    if not (dry or force) and now - st.get("last_ack_sweep", 0) < lim.get("ack_sweep_minutes", 10) * 60:
        return
    st["last_ack_sweep"] = now
    ch, team = cfg["slack"]["channel_id"], cfg["slack"]["trigger_subteams"][0]
    emails = {}

    def email_of(uid):
        if uid not in emails:
            try:
                emails[uid] = (client.users_info(user=uid)["user"]["profile"].get("email") or "").lower()
            except Exception:
                emails[uid] = ""
        return emails[uid]

    # The Reporter records the ack time as "now", so only act on FRESH oncall replies; acking a
    # week-old reply would log a week-long time-to-acknowledge. The list filters on the source
    # message's date (can be days older than the oncall post), hence the wide fetch window.
    fresh = now - lim.get("ack_reply_window_minutes", 30) * 60
    for it in reporter_issues(team, now - lim.get("ack_lookback_days", 30) * 86400, ("OPEN",), None, 10):
        issue_ts = it["issue_id"].split("_", 1)[1]
        try:
            msg = client.conversations_history(channel=ch, latest=issue_ts, inclusive=True, limit=1)["messages"][0]
        except Exception:
            continue
        if slack_status(msg) not in (None, "OPEN"):
            continue                          # Slack already shows it handled; API is stale
        src = source_of(msg)
        if not (src and ensure_member(client, src[0], cfg["slack"].get("source_channels") or [])):
            continue                          # can't see the source: can't tell issue from FYI
        src_msgs = thread_msgs(client, *src)
        # An alert only bots raised (recon, failure summaries) is an FYI: its right state is
        # "Acknowledge FYI", which the API can't set. Leave those for a human.
        if is_fyi(src_msgs, team):
            continue
        hit = None
        for tch, msgs in ((ch, thread_msgs(client, ch, issue_ts)), (src[0], src_msgs)):
            for r in msgs:
                # human replies made after the issue was raised (5 min slack for clock/ordering)
                if r.get("bot_id") or not r.get("user") or float(r["ts"]) < max(float(issue_ts) - 300, fresh):
                    continue
                if f"<!subteam^{team}" in r.get("text", ""):
                    continue                  # the tag that raised the issue, not a reply to it
                e = email_of(r["user"])
                if was_oncall(e, float(r["ts"]), cfg, float(issue_ts)):
                    hit = (e, r["ts"], tch)
                    break
            if hit:
                break
        if not hit:
            continue
        when = datetime.fromtimestamp(float(hit[1]), IST).strftime("%d %b %H:%M")
        if dry:
            print(f"ACK {it['issue_id']}: {hit[0]} replied {when} on {hit[2]}")
            continue
        ok, note = set_issue_status(issue_ts, "ACKNOWLEDGED", hit[0], ch, team)
        print(f"ack {it['issue_id']}: {hit[0]} @ {when} → {note}")


def scan(dry=False, only_ts=None):
    from slack_sdk import WebClient
    cfg = load_cfg()
    client = WebClient(token=secret("relay_slack_bot_token"))
    st = load_state()
    ch, triggers = cfg["slack"]["channel_id"], cfg["slack"]["trigger_subteams"]
    lim = cfg.get("limits") or {}
    now = time.time()
    if not st.get("last_ts") and not only_ts:
        st["last_ts"] = f"{now:.6f}"           # first run: start from now, don't replay history
        if not dry:
            save_state(st)
        print("initialised; watching from now"); return

    if only_ts:
        msgs = client.conversations_history(channel=ch, latest=only_ts, inclusive=True, limit=1)["messages"]
    else:
        msgs = client.conversations_history(channel=ch, oldest=st["last_ts"], limit=100)["messages"]
        newest = max([float(m["ts"]) for m in msgs] + [float(st["last_ts"])])
        st["last_ts"] = f"{newest:.6f}"
        seen_src = {it.get("src") for it in st["issues"].values() if it.get("src")}
        for m in sorted(msgs, key=lambda x: float(x["ts"])):   # register new escalations
            if not any(f"<!subteam^{t}" in m.get("text", "") for t in triggers):
                continue
            src = source_of(m)
            src_key = f"{src[0]}/{src[1]}" if src else None
            if src_key and src_key in seen_src:    # "Older Issue Bumped Up" re-ping of a known issue
                continue
            seen_src.add(src_key)
            st["issues"].setdefault(m["ts"].replace(".", "_"),
                                    {"ts": m["ts"], "src": src_key, "status": "new", "first_seen": now, "last_try": 0})

    # due = new ones + not-found retries within the give-up window
    todo = []
    if only_ts:
        todo = [(only_ts.replace(".", "_"), {"ts": only_ts})]
    else:
        for k, it in st["issues"].items():
            # not_found = gold lag; error = transient Astra failure (worker crash, empty body)
            if it["status"] == "new" or (it["status"] in ("not_found", "error")
                                         and now - it["last_try"] >= lim.get("retry_minutes", 30) * 60
                                         and now - it["first_seen"] <= lim.get("give_up_hours", 24) * 3600) \
                    or (it["status"] == "no_ids" and not it.get("rechecked")       # ids often posted after the tag:
                        and now - it["last_try"] >= lim.get("no_ids_recheck_minutes", 60) * 60):  # one re-read, once
                todo.append((k, it))
        todo = sorted(todo, key=lambda x: x[1]["ts"])[: lim.get("max_issues_per_run", 3)]

    team = team_members(client, cfg) if todo else set()
    for key, it in todo:
        if it.get("status") == "no_ids":
            it["rechecked"] = True
        msg = client.conversations_history(channel=ch, latest=it["ts"], inclusive=True, limit=1)["messages"][0]
        status, out = process(client, cfg, msg, dry, team)
        print(f"{it['ts']}: {status}")
        if status == "setup":
            alert_owner(client, cfg, st, out, dry)
            break                              # stays 'new'; retried after the token is fixed
        if status == "ready":
            blocks = card_blocks(key, out["summary"], out["id_used"], out["others"])
            if dry:
                print(json.dumps(blocks, indent=1, ensure_ascii=False))
            else:
                r = client.chat_postMessage(channel=ch, thread_ts=it["ts"], blocks=blocks,
                                            text=f"Auto-check: {out['summary']['verdict']}")
                it.update(card_ts=r["ts"], reply=final_reply(out["summary"]), label=out["summary"]["label"])
        elif status in ("error", "skipped"):
            print(f"   {out}", file=sys.stderr)
            it["note"] = out
        it.update(status="posted" if status == "ready" else status, last_try=now)
        st["issues"][key] = it
    if not dry and not only_ts:
        # merge-save: the status job and the relay bot write this file too
        fresh = load_state()
        fresh["last_ts"], fresh["alerted_day"] = st["last_ts"], st.get("alerted_day", "")
        for k, it in st["issues"].items():
            if k not in fresh["issues"] or k in {k2 for k2, _ in todo}:
                fresh["issues"][k] = {**fresh["issues"].get(k, {}), **it}
        save_state(fresh)
        revive_status_job(cfg, fresh)


def revive_status_job(cfg, st):
    """Watchdog. After sleep or shutdown, launchd resumes this 2-minute job reliably but has
    left the 10-minute status job idle for hours. If no ack sweep has run for
    status_stale_minutes, kick the status job (a no-op if it is already running)."""
    lim = cfg.get("limits") or {}
    stale = lim.get("status_stale_minutes", 30) * 60
    if time.time() - st.get("last_ack_sweep", 0) < stale:
        return
    label = str(lim.get("status_job_label") or "").strip()
    if not label:
        print("limits.status_job_label is not set, so there is no status job to kick")
        return
    res = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
    print(f"status job stale (last ack sweep {int((time.time() - st.get('last_ack_sweep', 0)) // 60)} min ago):"
          f" kicked {label} → {'ok' if res.returncode == 0 else res.stderr.strip()[:120]}")


def status_sweep(dry=False):
    """Status job (its own LaunchAgent, every 10 min): auto-acknowledge on fresh oncall
    replies every run, and the LLM closure loop every closure.sweep_minutes. Kept out of
    the 2-minute skill job so slow status work never delays a txn card."""
    from slack_sdk import WebClient
    cfg = load_cfg()
    client = WebClient(token=secret("relay_slack_bot_token"))
    st = load_state()
    try:
        ack_sweep(client, cfg, st, dry, force=True)
    except Exception as e:
        print(f"ack sweep failed: {e}", file=sys.stderr)
    import oncall_closure               # lazy: it imports this module
    if (cfg.get("closure") or {}).get("enabled") or dry:
        try:
            oncall_closure.closure_sweep(client, cfg, st, dry)
        except Exception as e:
            print(f"closure sweep failed: {e}", file=sys.stderr)
    if not dry:
        try:
            oncall_closure.retire_stale_cards(client, cfg, st)
        except Exception as e:
            print(f"card cleanup failed: {e}", file=sys.stderr)
    if not dry:
        fresh = load_state()
        for k in ("last_ack_sweep", "last_closure_sweep"):
            if k in st:
                fresh[k] = st[k]
        for k, it in st.get("issues", {}).items():
            if it.get("retired") and k in fresh.get("issues", {}):
                fresh["issues"][k]["retired"] = True
        mine, theirs = st.get("closure", {}), fresh.setdefault("closure", {})
        for k, v in mine.items():   # button decisions made meanwhile (relay bot) win
            keep = {x: theirs[k][x] for x in ("done", "kept_at", "undone", "decided_by", "decided", "undone_by")
                    if x in theirs.get(k, {})}
            theirs[k] = {**v, **keep}
        save_state(fresh)


def main():
    a = sys.argv[1:]
    if "--issue" in a:
        link = a[a.index("--issue") + 1]
        m = re.search(r"p(\d{10})(\d{6})", link)
        scan(dry=True, only_ts=f"{m.group(1)}.{m.group(2)}")
    elif "--status" in a:
        status_sweep(dry="--dry" in a)
    elif "--once" in a:
        scan(dry="--dry" in a)
    else:
        print(__doc__); sys.exit(1)


if __name__ == "__main__":
    main()

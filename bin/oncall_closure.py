#!/usr/bin/env python3
"""oncall_closure.py — LLM read of an oncall issue's threads to decide its closing status.

  --eval <days>     : score the classifier against issues people already closed in the
                      last <days> days (ground truth = their final status). Prints accuracy,
                      the confusion counts and every mismatch with links. Writes nothing.
  --issue <ts>      : classify one oncall issue (by its oncall-channel message ts) and print.

The LLM is Claude Code headless (`claude -p`, the owner's own login), run lean: no tools,
no MCP, no settings/hooks, a fixed system prompt and a JSON schema. ~$0.01-0.03 a thread.

What it sees: the source thread + the oncall thread, human messages only, each labelled by
ROLE (requester / oncall / team dev / other) instead of name, long numbers and emails
masked. Bot messages and "@Oncall Reporter <command>" messages are dropped, so the answer
can't be read off the Reporter's own status lines.

Labels (the owner's definitions):
  RESOLVED       we had to fix or change something on our side
  NOT_OUR_ISSUE  our systems worked as designed and we only gave clarity, or another team
                 / the requester's side owned the fix
  BLOCKED        waiting on someone else, nothing for us to do yet
  STILL_OPEN     work or an answer is still pending from us
"""
import json, os, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import txn_watch as tw  # noqa: E402

def reporter_uid(cfg=None):
    """The Oncall Reporter bot, whose own posts are not thread replies to read.

    An id names the workspace it lives in, so it comes from config. Blank simply means
    nothing is filtered out, which is the safe direction: a reply too many is read, not
    a real one dropped.
    """
    c = tw.load_cfg() if cfg is None else cfg
    return str(((c.get("slack") or {}).get("reporter_uid")) or "").strip()
WORKDIR = os.path.join(tw.ROOT, "work-context/state/closure_llm")   # empty cwd: no CLAUDE.md pickup
SCHEMA = {"type": "object", "required": ["status", "confidence", "reason", "evidence"],
          "properties": {
              "status": {"type": "string", "enum": ["RESOLVED", "NOT_OUR_ISSUE", "BLOCKED", "AWAITING_REQUESTER", "STILL_OPEN"]},
              "confidence": {"type": "number", "minimum": 0, "maximum": 1},
              "reason": {"type": "string", "description": "one sentence, no names"},
              "evidence": {"type": "integer", "description": "message number that settles it"}}}
SYSTEM_TMPL = """You review a bank's internal oncall support threads and decide how the issue should be closed in OUR team's oncall tracker.
Our team owns these systems: {our_systems}. Every other system belongs to another team, including: {not_our_systems}.
A fix counts as ours only when the thread shows [ONCALL] or [TEAM] making it in one of OUR systems. If another team ([OTHER]) shipped it, or it is unclear who shipped it, it is not ours; lower your confidence when ownership is unclear.

Labels:
- RESOLVED: the defect or change was in OUR systems and our team fixed, corrected, re-ran or changed it.
- NOT_OUR_ISSUE: our systems worked as designed and we only explained or clarified (refund already credited, wrong IFSC or beneficiary details, user error, expected behaviour), OR the defect or fix was in ANOTHER team's system, even if code was changed there.
- BLOCKED: we are waiting on another team before anything more can happen, and nothing has been handed off yet.
- AWAITING_REQUESTER: our team asked the requester (or reporting side) a question or for details and they never replied.
- STILL_OPEN: our team still owes an answer or work, and it has NOT been handed off anywhere.

Team conventions:
- Label by WHOSE SYSTEM had the defect or was changed, not by whether code changed.
- Handed off = closed: if the follow-up moved to a Jira ticket, a sprint item or another tracking thread, the oncall issue is closed. Use RESOLVED if the follow-up is in our systems, NOT_OUR_ISSUE if it is in another team's.
- A shipped fix with only cleanup left (recovery, lien removal, re-verification) counts as done, not STILL_OPEN.

Messages are labelled by role. [ONCALL] and [TEAM] are our team; [REQUESTER] raised it; [OTHER] is anyone else (often another team).
Judge from what actually happened, not from politeness.
confidence = how sure you are a careful human on our team would pick the same label. Use below 0.7 when the thread is ambiguous or thin."""


def system_prompt():
    c = tw.load_cfg().get("closure") or {}
    return SYSTEM_TMPL.format(our_systems=", ".join(c.get("our_systems") or ["our team's services"]),
                              not_our_systems=", ".join(c.get("not_our_systems") or ["anything not listed above"]))

NUM_RE = re.compile(r"\b\d{9,}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _mask(t):
    t = re.sub(r"<!subteam\^\w+[^>]*>", "@group", t)
    t = EMAIL_RE.sub("<email>", t)
    return NUM_RE.sub("<num>", t)


def transcript(client, cfg, issue_ts, team, emails):
    """(text, numbered msgs, meta) for one issue; None when the source thread isn't readable."""
    ch = cfg["slack"]["channel_id"]
    msg = client.conversations_history(channel=ch, latest=issue_ts, inclusive=True, limit=1)["messages"][0]
    src = tw.source_of(msg)
    if not (src and tw.ensure_member(client, src[0], ["*"])):
        return None
    src_msgs = tw.thread_msgs(client, *src)
    if tw.is_fyi(src_msgs, cfg["slack"]["trigger_subteams"][0]):
        return None                                   # FYI-type alert: not this loop's job
    requester = src_msgs[0].get("user")
    rows = []
    for where, msgs in (("source", src_msgs), ("oncall", tw.thread_msgs(client, ch, issue_ts)[1:])):
        for r in msgs:
            uid = reporter_uid()
            if r.get("bot_id") or not r.get("user") or (uid and f"<@{uid}" in r.get("text", "")):
                continue
            u = r["user"]
            if u not in emails:
                try:
                    emails[u] = (client.users_info(user=u)["user"]["profile"].get("email") or "").lower()
                except Exception:
                    emails[u] = ""
            ts = float(r["ts"])
            role = ("ONCALL" if tw.was_oncall(emails[u], ts, cfg, float(issue_ts)) else "TEAM" if u in team
                    else "REQUESTER" if u == requester else "OTHER")
            rows.append((ts, role, where, _mask(r.get("text", ""))[:700], r["ts"], src[0] if where == "source" else ch))
    rows.sort()
    if len(rows) > 40:
        rows = rows[:5] + rows[-35:]
    lines = [f"#{i} {datetime.fromtimestamp(t).strftime('%d %b %H:%M')} [{role}] ({where} thread): {text}"
             for i, (t, role, where, text, _, _) in enumerate(rows, 1)]
    return "\n".join(lines), rows, {"src": src, "requester": requester}


def classify(text):
    os.makedirs(WORKDIR, exist_ok=True)
    # launchd jobs get a bare PATH, so resolve the CLI explicitly
    claude = shutil.which("claude") or "/opt/homebrew/bin/claude"
    cmd = [claude, "-p", "--model", "sonnet", "--output-format", "json", "--no-session-persistence",
           "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--exclude-dynamic-system-prompt-sections",
           "--system-prompt", system_prompt(), "--json-schema", json.dumps(SCHEMA)]
    res = subprocess.run(cmd, input=f"Thread (oldest first):\n{text}\n\nDecide the closing label.",
                         capture_output=True, text=True, timeout=180, cwd=WORKDIR)
    d = json.loads(res.stdout or "{}")
    if d.get("is_error") or not d.get("structured_output"):
        raise RuntimeError((d.get("result") or res.stderr or "no output")[:200])
    out = d["structured_output"]
    out["cost"] = d.get("total_cost_usd", 0)
    return out


def _link(ch, ts):
    """A permalink to one message. The workspace host names the org, so it is configured.

    Slack's own chat_getPermalink is the authority; this builds the same URL for the
    places that have no client to hand. With no host configured it returns the empty
    string rather than a link to nowhere, and callers already treat that as "no link".
    """
    host = str(((tw.load_cfg().get("slack") or {}).get("workspace_host")) or "").strip()
    if not host:
        return ""
    return f"https://{host.rstrip('/')}/archives/{ch}/p{ts.replace('.', '')}"


def run_eval(days):
    from slack_sdk import WebClient
    cfg = tw.load_cfg()
    client = WebClient(token=tw.secret("relay_slack_bot_token"))
    ch, team_id = cfg["slack"]["channel_id"], cfg["slack"]["trigger_subteams"][0]
    team = tw.team_members(client, cfg)
    issues = tw.reporter_issues(team_id, time.time() - days * 86400, ("RESOLVED", "NOT_OUR_ISSUE", "BLOCKED"), None, 10)
    import yaml
    ov_path = os.path.join(tw.ROOT, "work-context/config/closure_truth_overrides.yaml")
    overrides = (yaml.safe_load(open(ov_path)) or {}) if os.path.exists(ov_path) else {}
    emails, cases = {}, []
    for it in issues:
        ts = it["issue_id"].split("_", 1)[1]
        try:
            m = client.conversations_history(channel=ch, latest=ts, inclusive=True, limit=1)["messages"][0]
            truth = overrides.get(ts) or tw.slack_status(m) or it["status"]
            tr = transcript(client, cfg, ts, team, emails)
        except Exception as e:
            print(f"skip {ts}: {e}", file=sys.stderr); continue
        if tr and truth in ("RESOLVED", "NOT_OUR_ISSUE", "BLOCKED"):
            cases.append((ts, truth, tr))
    print(f"{len(issues)} closed issues · {len(cases)} human-raised with a readable thread", flush=True)

    def one(c):
        ts, truth, (text, rows, _) = c
        try:
            return ts, truth, classify(text), rows
        except Exception as e:
            return ts, truth, {"status": "ERROR", "confidence": 0, "reason": str(e), "evidence": 0, "cost": 0}, rows

    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(one, cases))
    thr = (cfg.get("closure") or {}).get("auto_close_confidence", 0.85)
    ok = sum(1 for _, t, p, _ in results if p["status"] == t)
    sure = [(t, p) for _, t, p, _ in results if p["confidence"] >= thr and p["status"] in ("RESOLVED", "NOT_OUR_ISSUE")]
    sure_ok = sum(1 for t, p in sure if p["status"] == t)
    cards = sum(1 for _, t, p, _ in results if p["status"] in ("AWAITING_REQUESTER", "BLOCKED")
                or (p["status"] in ("RESOLVED", "NOT_OUR_ISSUE") and p["confidence"] < thr))
    print(f"routing: auto-close {len(sure)} · card {cards} · left open {len(results) - len(sure) - cards}")
    conf = {}
    for _, t, p, _ in results:
        conf[(t, p["status"])] = conf.get((t, p["status"]), 0) + 1
    print(f"accuracy {ok}/{len(results)} · auto-close set (conf≥{thr}, resolved/not-ours): {sure_ok}/{len(sure)} correct"
          f" · cost ${sum(p.get('cost', 0) for _, _, p, _ in results):.2f}")
    print("human → llm counts:", {f"{a}→{b}": n for (a, b), n in sorted(conf.items())})
    out = os.path.join(tw.ROOT, "work-context/state/closure_eval.jsonl")
    with open(out, "w") as f:
        for ts, t, p, _ in results:
            route = ("auto" if p["confidence"] >= thr and p["status"] in ("RESOLVED", "NOT_OUR_ISSUE")
                     else "card" if p["status"] in ("AWAITING_REQUESTER", "BLOCKED", "RESOLVED", "NOT_OUR_ISSUE")
                     else "open")
            f.write(json.dumps({"link": _link(ch, ts), "human": t, "llm": p["status"], "confidence": p["confidence"],
                                "route": route, "reason": p["reason"]}) + "\n")
    print(f"per-issue results: {out}")
    for ts, t, p, rows in results:
        if p["status"] != t:
            ev = rows[p["evidence"] - 1] if 0 < p.get("evidence", 0) <= len(rows) else None
            print(f"MISMATCH human={t} llm={p['status']} ({p['confidence']:.2f}) {_link(ch, ts)}\n   {p['reason']}"
                  + (f"\n   evidence: {_link(ev[5], ev[4])}" if ev else ""))


# ---------- live closure loop (called from txn_watch.scan) ----------
LABEL_HUMAN = {"RESOLVED": "Resolved", "NOT_OUR_ISSUE": "Not our issue", "BLOCKED": "Blocked",
               "AWAITING_REQUESTER": "Waiting on requester", "STILL_OPEN": "Still open", "FYI": "FYI"}
CARD_CHOICES = ("RESOLVED", "NOT_OUR_ISSUE", "BLOCKED")


def _evidence_link(client, rows, n):
    if 0 < (n or 0) <= len(rows):
        _, _, _, _, ts, ch = rows[n - 1]
        try:
            return client.chat_getPermalink(channel=ch, message_ts=ts)["permalink"]
        except Exception:
            pass
    return None


def note_blocks(key, out, ev):
    """Auto-close note: what was set, why, the deciding message, and an Undo."""
    txt = (f"🤖 *Closed as {LABEL_HUMAN[out['status']]}* ({out['confidence']:.2f}): {out['reason']}"
           + (f" · <{ev}|deciding message>" if ev else ""))
    return [{"type": "section", "text": {"type": "mrkdwn", "text": txt}},
            {"type": "actions", "block_id": f"txcact_{key}", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "↩ Undo"},
                 "action_id": f"txc:{key}:undo", "value": key}]}]


def card_blocks(key, out, ev, oncall_uid=None):
    """Close-out card: the LLM's read, then one button per closing label (suggestion first)."""
    sug = out["status"] if out["status"] in CARD_CHOICES else None
    head = {"AWAITING_REQUESTER": "We asked the requester and got no reply.",
            "BLOCKED": "Looks like this is waiting on another team.",
            "STILL_OPEN": "Quiet for a day with no clear end."}.get(out["status"], "Looks done, but I'm not sure enough to close it.")
    who = f"<@{oncall_uid}> " if oncall_uid else ""
    txt = (f"🤖 {who}*How should this close?* {head}\n_Read:_ {LABEL_HUMAN[out['status']]} ({out['confidence']:.2f}). "
           f"{out['reason']}" + (f" · <{ev}|deciding message>" if ev else ""))
    order = ([sug] if sug else []) + [c for c in CARD_CHOICES if c != sug]
    buttons = [{"type": "button", **({"style": "primary"} if c == sug else {}),
                "text": {"type": "plain_text", "text": LABEL_HUMAN[c]},
                "action_id": f"txc:{key}:set:{c}", "value": key} for c in order]
    buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Keep open"},
                    "action_id": f"txc:{key}:keep", "value": key})
    return [{"type": "section", "text": {"type": "mrkdwn", "text": txt}},
            {"type": "actions", "block_id": f"txcact_{key}", "elements": buttons}]


def live_card(m):
    """The issue's close-out card is still waiting for a click: it exists and was posted
    after the last Keep-open (an answered card's buttons are already gone)."""
    return bool(m.get("card_ts")) and float(m.get("card_at") or m["card_ts"]) > float(m.get("kept_at") or 0)


def retire_card(client, ch, issue_ts, card_ts, note):
    """Strip a card's buttons and append a one-line note (cards are thread replies)."""
    cur = [x for x in tw.thread_msgs(client, ch, issue_ts) if x.get("ts") == card_ts]
    if not cur:
        return False
    blocks = [b for b in (cur[0].get("blocks") or []) if b.get("type") != "actions"]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    client.chat_update(channel=ch, ts=card_ts, blocks=blocks, text=note)
    return True


def retire_stale_cards(client, cfg, st):
    """Cards still waiting for a click whose issue got closed some other way (Reporter
    buttons, TARS, API): remove their buttons so nobody acts on a closed issue."""
    ch = cfg["slack"]["channel_id"]
    pending = [(k, m["card_ts"], "closure") for k, m in st.get("closure", {}).items()
               if live_card(m) and not m.get("done") and not m.get("retired")]
    pending += [(k, it["card_ts"], "txn") for k, it in st.get("issues", {}).items()
                if it.get("card_ts") and it.get("status") == "posted" and not it.get("retired")]
    for key, card_ts, kind in pending:
        issue_ts = key.replace("_", ".")
        try:
            msg = client.conversations_history(channel=ch, latest=issue_ts, inclusive=True, limit=1)["messages"][0]
        except Exception:
            continue
        shown = tw.slack_status(msg)
        if shown not in tw.CLOSED_STATUSES:
            continue
        try:
            if retire_card(client, ch, issue_ts, card_ts, f"Issue already closed as *{LABEL_HUMAN.get(shown, shown.title())}*; nothing to do here."):
                (st["closure"][key] if kind == "closure" else st["issues"][key]).update(retired=True)
                print(f"retired {kind} card on {issue_ts} (issue {shown})")
        except Exception as e:
            print(f"retire {card_ts} failed: {e}", file=sys.stderr)


def closure_sweep(client, cfg, st, dry=False):
    """For ACKNOWLEDGED / BLOCKED issues, read the threads with the LLM once they have new
    messages AND have been quiet for quiet_hours (one read per quiet spell keeps cost down).
    A confident RESOLVED / NOT_OUR_ISSUE closes the issue then; anything else becomes a
    close-out card (a STILL_OPEN read only after a quiet day). "Keep open"
    silences an issue until new messages arrive after the click; Undo silences it for good."""
    c = cfg.get("closure") or {}
    lim = cfg.get("limits") or {}
    now = time.time()
    if not dry and now - st.get("last_closure_sweep", 0) < c.get("sweep_minutes", 30) * 60:
        return
    st["last_closure_sweep"] = now
    ch, team_id = cfg["slack"]["channel_id"], cfg["slack"]["trigger_subteams"][0]
    thr, quiet_h, open_h = c.get("auto_close_confidence", 0.75), c.get("quiet_hours", 2), c.get("still_open_card_hours", 24)
    mem = st.setdefault("closure", {})
    team, emails, budget = None, {}, c.get("max_reads_per_run", 3)
    for it in tw.reporter_issues(team_id, now - lim.get("ack_lookback_days", 30) * 86400, ("ACKNOWLEDGED", "BLOCKED"), None, 10):
        if budget <= 0:
            break
        issue_ts = it["issue_id"].split("_", 1)[1]
        key = issue_ts.replace(".", "_")
        m = mem.setdefault(key, {})
        if m.get("done") or m.get("undone"):
            continue                                  # closed, or a human overruled an auto-close
        try:
            msg = client.conversations_history(channel=ch, latest=issue_ts, inclusive=True, limit=1)["messages"][0]
        except Exception:
            continue
        if tw.slack_status(msg) in tw.CLOSED_STATUSES:
            m["done"] = True
            continue
        team = team if team is not None else tw.team_members(client, cfg)
        tr = transcript(client, cfg, issue_ts, team, emails)
        if not tr:
            continue
        text, rows, _ = tr
        last = max((r[0] for r in rows), default=0)
        if m.get("kept_at") and last <= m["kept_at"]:
            continue                                  # "Keep open": silent until the thread moves
        if now - last < quiet_h * 3600:
            continue                                  # read only once the thread settles: one LLM read per quiet spell
        fresh = last > m.get("last_seen", 0)
        if fresh:                                     # new messages since the last read
            budget -= 1
            try:
                out = classify(text)
            except Exception as e:
                print(f"closure classify {issue_ts} failed: {e}", file=sys.stderr)
                continue
            out["evidence_link"] = _evidence_link(client, rows, out.get("evidence"))
            m.update(last_seen=last, read=out["status"], out=out, at=now, pending=True)
        elif not m.get("pending"):
            continue                                  # this read was already acted on
        out = m["out"]
        ev = out.get("evidence_link")
        auto = out["status"] in ("RESOLVED", "NOT_OUR_ISSUE") and out["confidence"] >= thr
        if not auto:
            # a clear close happens at once; anything needing a human waits for the thread to
            # settle (2h quiet, or a day for a still-open read) so we don't nag mid-discussion
            wait_h = open_h if out["status"] == "STILL_OPEN" else quiet_h
            if now - last < wait_h * 3600:
                if fresh:
                    print(f"closure {issue_ts}: {out['status']} {out['confidence']:.2f} → card once quiet {wait_h}h")
                continue
        line = f"closure {issue_ts}: {out['status']} {out['confidence']:.2f} → {'AUTO' if auto else 'CARD'} | {out['reason'][:120]}"
        if dry:
            print(line)
            continue
        m["pending"] = False
        if auto:
            ok, note = tw.set_issue_status(issue_ts, out["status"], f"relay-bot auto-close ({out['confidence']:.2f})", ch, team_id)
            if ok:
                client.chat_postMessage(channel=ch, thread_ts=issue_ts, blocks=note_blocks(key, out, ev),
                                        text=f"Closed as {LABEL_HUMAN[out['status']]}")
                m.update(done=True, auto=out["status"], prev=it.get("status"))
                if live_card(m):                      # retire the unanswered close-out card
                    try:
                        retire_card(client, ch, issue_ts, m["card_ts"],
                                    f"Superseded: the thread moved on and I closed it as *{LABEL_HUMAN[out['status']]}*.")
                    except Exception as e:
                        print(f"retire card {m['card_ts']} failed: {e}", file=sys.stderr)
            print(line + f" | {note}")
        else:                                         # first card, or a fresh read for an unanswered one
            oncall = None
            try:
                ems = tw.oncall_periods(cfg, now)
                cur = next((who for s, e, who in ems if s <= now <= e), None)
                if cur:
                    oncall = client.users_lookupByEmail(email=cur)["user"]["id"]
            except Exception:
                pass
            blocks = card_blocks(key, out, ev, oncall)
            if live_card(m):                          # unanswered card already there: refresh it, don't repost
                client.chat_update(channel=ch, ts=m["card_ts"], blocks=blocks, text="How should this close?")
                m.update(card_read=out["status"])
                print(line + " (card updated in place)")
            else:
                r = client.chat_postMessage(channel=ch, thread_ts=issue_ts, blocks=blocks, text="How should this close?")
                m.update(card_ts=r["ts"], card_at=float(r["ts"]), card_read=out["status"])
                print(line)


def main():
    a = sys.argv[1:]
    if "--eval" in a:
        run_eval(int(a[a.index("--eval") + 1]))
    elif "--issue" in a:
        from slack_sdk import WebClient
        cfg = tw.load_cfg()
        client = WebClient(token=tw.secret("relay_slack_bot_token"))
        tr = transcript(client, cfg, a[a.index("--issue") + 1], tw.team_members(client, cfg), {})
        if not tr:
            sys.exit("source thread unreadable or an FYI alert")
        print(tr[0], "\n---"); print(json.dumps(classify(tr[0]), indent=1))
    else:
        print(__doc__); sys.exit(1)


if __name__ == "__main__":
    main()

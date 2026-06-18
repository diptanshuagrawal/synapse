#!/usr/bin/env python3
"""relay_bot.py — shared owner-only Slack bot (Socket Mode) for the work-context skills.

Consumers: /ticketize and /doc-sync-sweep. Roles:
  --post <date>          : read management/standup/<date>/ticket-candidates.md and post the
                           OPEN candidates to the ticketize channel with Approve/Reject buttons.
  --post-docsync <run-id>: read state/doc_sync_discovered.json and post one Approve/Reject card
                           per newly-discovered doc to the doc-sweep channel (config doc_sync.yaml).
                           Approve → promote needs_confirm→monitor; Reject → move to excluded
                           (both via derive/doc_sync_state.py move). Owner-gated, RELAY_APPLY_MODE-gated.
  (no args)              : run the Socket Mode listener — handle tkz: and dsc: button clicks
                           (owner-only), apply the decision, and update the message.

Config: work-context/config/ticketize.yaml (channel_id, owner_slack_id, fallback_epic, …).
Tokens: ~/.secrets/relay_slack_bot_token (xoxb), ~/.secrets/relay_slack_app_token (xapp).

APPLY SAFETY: env RELAY_APPLY_MODE = dry (default) | live.
  dry  → clicks are acknowledged + echoed ("would apply …"); NO Jira write.
  live → clicks invoke the deterministic apply (bin/ticketize_apply.py).
This lets us test the Slack loop with zero Jira risk, then flip to live.
"""
import os, re, sys, json, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "work-context/config/ticketize.yaml")
SECRETS = os.path.expanduser("~/.secrets")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ticketize_apply as ta  # reuse jira_auth/epic_title/search_epic (no main run on import)


def load_cfg():
    import yaml
    return yaml.safe_load(open(CONFIG))


def secret(name):
    return open(os.path.join(SECRETS, name)).read().strip()


def candidates_path(date):
    return os.path.join(ROOT, f"management/standup/{date}/ticket-candidates.md")


HEAD_RE = re.compile(r"^##\s+([CG]\d+)\s+·\s+(.*?)\s*(?:—\s*(\w+).*)?$")
FIELD_RE = re.compile(r"^-\s+([a-z_]+):\s*(.*?)\s*(?:#.*)?$")


def parse_candidates(date):
    """Return list of dicts for every candidate block in the md (label,name,fields…)."""
    path = candidates_path(date)
    if not os.path.exists(path):
        return None
    out, cur, last = [], None, None
    for raw in open(path):
        line = raw.rstrip()
        h = HEAD_RE.match(line)
        if h:
            if cur:
                out.append(cur)
            cur = {"label": h.group(1), "name": h.group(2).strip(), "status_tag": (h.group(3) or "").lower()}
            last = None
            continue
        if cur is None:
            continue
        f = FIELD_RE.match(line)
        if f:
            cur[f.group(1)] = f.group(2).strip(); last = f.group(1)
        elif last and raw.startswith(("  ", "\t")) and line.strip():
            cur[last] += " " + line.strip()   # multi-line field value (e.g. a wrapped `why:`)
        else:
            last = None
    if cur:
        out.append(cur)
    return out


def open_candidates(date):
    cands = parse_candidates(date)
    if cands is None:
        return None
    return [c for c in cands if (c.get("decision", "pending").lower() == "pending")]


# ---------- posting ----------
TIER_GLOSS = {"🔴": "🔴 touches money / ledger — human-driven",
              "🟡": "🟡 feature work", "🟢": "🟢 small / mechanical"}


def fmt_refs(ev):
    """Turn the evidence string into nice labelled links."""
    out = []
    for tok in re.split(r"[\s·,]+", ev or ""):
        if not tok.startswith("http"):
            continue
        m = re.search(r"/browse/([A-Z]+-\d+)", tok)
        if m:
            out.append(f"<{tok}|{m.group(1)}>"); continue
        m = re.search(r"/pull/(\d+)", tok)
        if m:
            out.append(f"<{tok}|PR #{m.group(1)}>"); continue
        out.append(f"<{tok}|link>")
    return "  ·  ".join(out)


def build_blocks(date, opens):
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🎫 Ticket candidates — {date}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "Untracked work I found. *Approve* → I create the Jira ticket. *Reject* → I drop it. (only you can act)"}]},
        {"type": "divider"},
    ]
    for c in opens:
        tier = c.get("code_tier", "")
        gloss = next((v for k, v in TIER_GLOSS.items() if k in tier), tier)
        lines = [f"*{c['label']}  ·  {c.get('summary','(no summary)')}*", ""]
        if c.get("why"):
            why = c["why"]
            why = (why[:300].rstrip() + "…") if len(why) > 300 else why
            lines.append(f"📌 *Why it matters*\n{why}")
            lines.append("")
        lines.append(f"✅ *Proposed ticket:*  a *{c.get('type','Task')}*  →  *{c.get('name','?')}*")
        ek, et, base = c.get("epic_key"), c.get("epic_title"), c.get("base_url", "")
        if ek:
            epic_md = f"<{base}/browse/{ek}|{ek}>" + (f"  _{et}_" if et else "")
        else:
            epic_md = (c.get("epic") or "—").split(" (")[0]
        lines.append(f"📂 *Epic (suggested, editable on approve):*  {epic_md}")
        if gloss:
            lines.append(f"⚖️ *Risk:*  {gloss}")
        refs = fmt_refs(c.get("evidence", ""))
        if refs:
            lines.append(f"🔗 *Refs:*  {refs}")
        fp = c.get("fingerprint", "")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        blocks.append({"type": "actions", "block_id": f"act_{c['label']}", "elements": [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": f"✓ Approve {c['label']}"},
             "action_id": f"tkz:{date}:{fp}:approve:{c['label']}", "value": c["label"]},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "✕ Reject"},
             "action_id": f"tkz:{date}:{fp}:reject:{c['label']}", "value": c["label"]},
        ]})
        blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"{len(opens)} open · created tickets land in the active sprint"}]})
    return blocks


def do_post(date):
    from slack_sdk import WebClient
    cfg = load_cfg()
    opens = open_candidates(date)
    client = WebClient(token=secret("relay_slack_bot_token"))
    ch = cfg["slack"]["channel_id"]
    if opens is None:
        print(f"no candidate file for {date}"); return
    if not opens:
        client.chat_postMessage(channel=ch, text=f"No new ticketable gaps for {date}. ✅")
        print("posted: none-open"); return
    # enrich each candidate with its suggested epic's title (read-only Jira)
    try:
        base, auth = cfg["jira"]["base_url"], ta.jira_auth()
        for c in opens:
            ke = (c.get("epic") or "").split()[0]
            if re.match(r"^[A-Z]+-\d+$", ke):
                c["epic_key"], c["base_url"] = ke, base
                c["epic_title"] = ta.epic_title(base, auth, ke)
    except Exception as e:
        print(f"epic-title enrich skipped: {e}", file=sys.stderr)
    r = client.chat_postMessage(channel=ch, text=f"Ticket candidates — {date} ({len(opens)} open)",
                                blocks=build_blocks(date, opens))
    print(f"posted {len(opens)} candidates to {ch} ts={r['ts']}")


# ---------- doc-sync discovery cards ----------
DOCSYNC_CFG = os.path.join(ROOT, "work-context/config/doc_sync.yaml")
DOCSYNC_INVENTORY = os.path.join(ROOT, "work-context/config/doc_sync_inventory.yaml")
DOCSYNC_DISCOVERED = os.path.join(ROOT, "work-context/state/doc_sync_discovered.json")
DOCSYNC_STATE = os.path.join(ROOT, "work-context/derive/doc_sync_state.py")


def load_docsync_cfg():
    import yaml
    return yaml.safe_load(open(DOCSYNC_CFG))


def _docsync_discovered():
    """This run's NEW discovered docs: [{id, title, author, repo}]."""
    if not os.path.exists(DOCSYNC_DISCOVERED):
        return None
    d = json.load(open(DOCSYNC_DISCOVERED))
    return d.get("candidates", d) if isinstance(d, dict) else d


def build_docsync_blocks(run_id, docs):
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🔎 Newly-discovered docs — {run_id}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "Team-authored design docs I found. *Approve* → I add it to the monitored set (next sweep checks it). *Reject* → I drop it to excluded. (only you can act)"}]},
        {"type": "divider"},
    ]
    for c in docs:
        pid = str(c.get("id"))
        url = c.get("webUrl") or c.get("page_url") or ""
        title = c.get("title", "(untitled)")
        title_md = f"<{url}|{title}>" if url else title
        lines = [f"*{title_md}*",
                 f"✍️ {c.get('author') or c.get('owner') or '?'}   ·   📦 `{c.get('repo','?')}`   ·   `{pid}`"]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        blocks.append({"type": "actions", "block_id": f"dscact_{pid}", "elements": [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "✓ Approve → monitor"},
             "action_id": f"dsc:{run_id}:{pid}:approve", "value": pid},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "✕ Reject → excluded"},
             "action_id": f"dsc:{run_id}:{pid}:reject", "value": pid},
        ]})
        blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"{len(docs)} discovered · Approve promotes to the monitor list · Reject excludes it"}]})
    return blocks


def do_post_docsync(run_id):
    from slack_sdk import WebClient
    cfg = load_docsync_cfg()
    ch = cfg["slack"]["channel_id"]
    if not ch:
        print("no doc_sync channel_id configured — skipping", file=sys.stderr); sys.exit(1)
    docs = _docsync_discovered()
    client = WebClient(token=secret("relay_slack_bot_token"))
    if not docs:
        print("posted: no newly-discovered docs this run"); return
    r = client.chat_postMessage(channel=ch, text=f"{len(docs)} newly-discovered docs — {run_id}",
                                blocks=build_docsync_blocks(run_id, docs))
    print(f"posted {len(docs)} discovery cards to {ch} ts={r['ts']}")


# ---------- listener ----------
def run_listener():
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    cfg = load_cfg()
    owner = cfg["slack"]["owner_slack_id"]
    mode = os.environ.get("RELAY_APPLY_MODE", "dry")
    app = App(token=secret("relay_slack_bot_token"))

    def run_apply(date, fp, decision, epic_input=None):
        if mode != "live":
            return True, f"🧪 dry — would {decision} (epic='{epic_input or ''}'); set RELAY_APPLY_MODE=live to action"
        cmd = [sys.executable, os.path.join(ROOT, "bin/ticketize_apply.py"),
               "--date", date, "--fingerprint", fp, "--decision", decision]
        if epic_input:
            cmd += ["--epic-input", epic_input]
        res = subprocess.run(cmd, capture_output=True, text=True)
        out = (res.stdout.strip() or res.stderr.strip() or "")
        note = out.splitlines()[-1] if out else ""
        return res.returncode == 0, note

    def is_owner(body, client):
        uid = body["user"]["id"]
        if uid != owner:
            ch = (body.get("channel") or {}).get("id")
            if ch:
                client.chat_postEphemeral(channel=ch, user=uid, text="Only the owner can action these.")
            return False
        return True

    @app.action(re.compile(r"^tkz:.*:approve:"))
    def on_approve(ack, body, client, action):
        ack()
        if not is_owner(body, client):
            return
        _, date, fp, verb, label = action["action_id"].split(":", 4)
        cands = parse_candidates(date) or []
        c = next((x for x in cands if x.get("fingerprint") == fp), {})
        suggested = (c.get("epic") or "").split(" (")[0]
        client.views_open(trigger_id=body["trigger_id"], view={
            "type": "modal", "callback_id": "tkz_apply",
            "private_metadata": json.dumps({"date": date, "fp": fp, "label": label,
                                            "channel": body["channel"]["id"], "msg_ts": body["message"]["ts"]}),
            "title": {"type": "plain_text", "text": "Create ticket"},
            "submit": {"type": "plain_text", "text": "Create"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"*{label}* — {c.get('summary','')}\nCreating a *{c.get('type','Task')}* for *{c.get('name','?')}*."}},
                {"type": "input", "block_id": "epic",
                 "label": {"type": "plain_text", "text": "Epic"},
                 "element": {"type": "plain_text_input", "action_id": "epic_val",
                             "initial_value": suggested,
                             "placeholder": {"type": "plain_text", "text": "EX-1234  or  keywords e.g. atm charges"}},
                 "hint": {"type": "plain_text", "text": "A key (EX-1234) is used as-is. Keywords search open epics and pick the best match."}},
            ],
        })

    @app.view("tkz_apply")
    def on_submit(ack, body, client, view):
        ack()
        m = json.loads(view["private_metadata"])
        if body["user"]["id"] != owner:
            return
        epic_in = (view["state"]["values"]["epic"]["epic_val"].get("value") or "").strip()
        ok, note = run_apply(m["date"], m["fp"], "approve", epic_in or None)
        client.chat_postMessage(channel=m["channel"], thread_ts=m["msg_ts"],
                                text=f"{'✅' if ok else '⚠️'} *{m['label']}* approved — {note}")

    @app.action(re.compile(r"^tkz:.*:reject:"))
    def on_reject(ack, body, client, action):
        ack()
        if not is_owner(body, client):
            return
        _, date, fp, verb, label = action["action_id"].split(":", 4)
        ok, note = run_apply(date, fp, "reject")
        client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"],
                                text=f"🗑️ *{label}* rejected — {note}")

    # ---- doc-sync discovery: Approve → monitor, Reject → excluded ----
    def docsync_apply(page_id, decision, run_id):
        to = "promote" if decision == "approve" else "exclude"
        if mode != "live":
            return True, f"🧪 dry — would {to} {page_id}; set RELAY_APPLY_MODE=live to action"
        cmd = [sys.executable, DOCSYNC_STATE, "move",
               "--inventory", DOCSYNC_INVENTORY, "--id", page_id, "--to", to]
        if run_id:
            cmd += ["--run-id", run_id]
        res = subprocess.run(cmd, capture_output=True, text=True)
        out = (res.stdout.strip() or res.stderr.strip() or "")
        return res.returncode == 0, (out.splitlines()[-1] if out else "")

    @app.action(re.compile(r"^dsc:.*:approve$"))
    def on_docsync_approve(ack, body, client, action):
        ack()
        if not is_owner(body, client):
            return
        _, run_id, pid, verb = action["action_id"].split(":", 3)
        ok, note = docsync_apply(pid, "approve", run_id)
        client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"],
                                text=f"{'✅' if ok else '⚠️'} `{pid}` promoted to *monitor* — {note}")

    @app.action(re.compile(r"^dsc:.*:reject$"))
    def on_docsync_reject(ack, body, client, action):
        ack()
        if not is_owner(body, client):
            return
        _, run_id, pid, verb = action["action_id"].split(":", 3)
        ok, note = docsync_apply(pid, "reject", run_id)
        client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"],
                                text=f"{'🗑️' if ok else '⚠️'} `{pid}` moved to *excluded* — {note}")

    print(f"relay_bot listening (apply mode = {mode}) …")
    SocketModeHandler(app, secret("relay_slack_app_token")).start()


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--post":
        do_post(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "--post-docsync":
        do_post_docsync(sys.argv[2])
    elif len(sys.argv) == 1 or sys.argv[1] == "--listen":
        run_listener()
    else:
        print(__doc__); sys.exit(1)


if __name__ == "__main__":
    main()

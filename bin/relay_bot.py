#!/usr/bin/env python3
"""relay_bot.py — shared owner-only Slack bot (Socket Mode) for the work-context skills.

First consumer: /ticketize. Two roles:
  --post <date>   : read management/standup/<date>/ticket-candidates.md and post the OPEN
                    candidates to the ticketize channel with Approve/Reject buttons.
  (no args)       : run the Socket Mode listener — handle button clicks (owner-only),
                    apply the decision, and update the message.

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
    out, cur = [], None
    for line in open(path):
        h = HEAD_RE.match(line.rstrip())
        if h:
            if cur:
                out.append(cur)
            cur = {"label": h.group(1), "name": h.group(2).strip(), "status_tag": (h.group(3) or "").lower()}
            continue
        if cur:
            f = FIELD_RE.match(line.rstrip())
            if f:
                cur[f.group(1)] = f.group(2).strip()
    if cur:
        out.append(cur)
    return out


def open_candidates(date):
    cands = parse_candidates(date)
    if cands is None:
        return None
    return [c for c in cands if (c.get("decision", "pending").lower() == "pending")]


# ---------- posting ----------
def build_blocks(date, opens):
    blocks = [{
        "type": "header",
        "text": {"type": "plain_text", "text": f"Ticket candidates — {date}"},
    }, {
        "type": "context",
        "elements": [{"type": "mrkdwn",
                      "text": "Tap *Approve* to create (active sprint · Tech-Misc epic) or *Reject*. Owner-only."}],
    }, {"type": "divider"}]
    for c in opens:
        tier = c.get("code_tier", "")
        ev = c.get("evidence", "")
        txt = f"*{c['label']} — {c.get('summary','(no summary)')}*"
        if c.get("why"):
            txt += f"\n{c['why']}"
        txt += f"\nProposing a *{c.get('type','Task')}* for *{c.get('assignee','?')}*"
        if tier:
            txt += f"  ·  {tier}"
        if ev:
            txt += f"  ·  <{ev}|evidence>"
        fp = c.get("fingerprint", "")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
        blocks.append({"type": "actions", "block_id": f"act_{c['label']}", "elements": [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": f"Approve {c['label']}"},
             "action_id": f"tkz:{date}:{fp}:approve:{c['label']}", "value": c["label"]},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "Reject"},
             "action_id": f"tkz:{date}:{fp}:reject:{c['label']}", "value": c["label"]},
        ]})
    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"mode: *{os.environ.get('RELAY_APPLY_MODE','dry')}* · {len(opens)} open"}]})
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
    r = client.chat_postMessage(channel=ch, text=f"Ticket candidates — {date} ({len(opens)} open)",
                                blocks=build_blocks(date, opens))
    print(f"posted {len(opens)} candidates to {ch} ts={r['ts']}")


# ---------- listener ----------
def run_listener():
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    cfg = load_cfg()
    owner = cfg["slack"]["owner_slack_id"]
    mode = os.environ.get("RELAY_APPLY_MODE", "dry")
    app = App(token=secret("relay_slack_bot_token"))

    @app.action(re.compile(r"^tkz:"))
    def handle(ack, body, client, action, logger):
        ack()
        uid = body["user"]["id"]
        if uid != owner:
            client.chat_postEphemeral(channel=body["channel"]["id"], user=uid,
                                      text="Not authorized — only the owner can action these.")
            return
        _, date, fp, verb, label = action["action_id"].split(":", 4)
        ts = body["message"]["ts"]; ch = body["channel"]["id"]
        if mode == "live":
            res = subprocess.run([sys.executable, os.path.join(ROOT, "bin/ticketize_apply.py"),
                                  "--date", date, "--fingerprint", fp, "--decision", verb],
                                 capture_output=True, text=True)
            ok = res.returncode == 0
            note = (res.stdout.strip() or res.stderr.strip() or "").splitlines()[-1] if (res.stdout or res.stderr) else ""
            msg = f"{'✅' if ok else '⚠️'} {label} {verb} — {note}"
        else:
            msg = f"🧪 (dry) would {verb} {label} [{fp}] for {date} — set RELAY_APPLY_MODE=live to action"
        client.chat_postMessage(channel=ch, thread_ts=ts, text=msg)

    print(f"relay_bot listening (apply mode = {mode}) …")
    SocketModeHandler(app, secret("relay_slack_app_token")).start()


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--post":
        do_post(sys.argv[2])
    elif len(sys.argv) == 1 or sys.argv[1] == "--listen":
        run_listener()
    else:
        print(__doc__); sys.exit(1)


if __name__ == "__main__":
    main()

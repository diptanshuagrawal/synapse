#!/usr/bin/env python3
"""ic_rota_bot.py — Incident-Commander rota reminders + #on-call topic sync.

Problem: org-wide oncall Incident Commanders (Opsgenie Incident-Commanders_schedule)
miss two manual steps — calling out leave BEFORE their stint, and updating the
#on-call channel topic at handover. This bot automates both.

Roles:
  --remind      : if the next IC stint starts within remind_days_before, post a
                  heads-up in the IC channel tagging that person: confirm
                  availability, call out leaves, arrange swap + Opsgenie override.
                  Posted once per (stint start, person) — deduped via state file.
  --sync-topic  : compare the #on-call channel topic against the CURRENT IC from
                  the rota; on mismatch set the topic (live mode) and post a
                  handover note tagging old + new IC.
  --status      : print rota, current/next IC, topic state. No posts.
  --post-test   : post one sample of each message to the test channel (format review).

MODE SAFETY: config `mode: test` (default) routes every post to `test_channel`
(owner-only) and NEVER touches the real topic — topic changes are echoed as a
"would set" message. `mode: live` posts to the real channels. There is no
CLI override to force live; flip it in the config deliberately.

Rota sources (config `rota_source`):
  opsgenie : GET /v2/schedules/{id}/timeline — finalTimeline already folds in
             overrides, so approved swaps are reflected automatically.
             Key: ~/.secrets/opsgenie_ic_api_key, else ~/.secrets/opsgenie_api_key.
             NOTE: the team-scoped CBS key gets 40301 on this schedule; needs a
             key with read access to Incident-Commanders_schedule.
  static   : explicit [{start, end, email}] list in the config (dates IST,
             end-date inclusive). Stopgap until the Opsgenie key exists —
             does NOT see swaps unless you edit the yaml.

Slack: relay bot token (~/.secrets/relay_slack_bot_token). Live mode needs
scopes the app does not have yet — see README block in config/ic_rota.yaml.

Config: work-context/config/ic_rota.yaml   State: work-context/state/ic_rota_state.json
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "work-context/config/ic_rota.yaml")
STATE = os.path.join(ROOT, "work-context/state/ic_rota_state.json")
PEOPLE = os.path.join(ROOT, "work-context/config/people.yaml")
SECRETS = os.path.expanduser("~/.secrets")
IST = timezone(timedelta(hours=5, minutes=30))


def load_cfg(path=None):
    import yaml
    with open(path or os.environ.get("IC_ROTA_CONFIG", CONFIG)) as f:
        return yaml.safe_load(f)


def secret(name):
    with open(os.path.join(SECRETS, name)) as f:
        return f.read().strip()


def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {"reminded": {}, "last_topic": None}


def save_state(st):
    tmp = f"{STATE}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE)


# ---------------------------------------------------------------- slack api
def slack_call(method, tok, **params):
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(params).encode(),
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read())
    if not out.get("ok"):
        raise RuntimeError(f"slack {method}: {out.get('error')} "
                           f"(needed scope: {out.get('needed', '?')})")
    return out


def post(cfg, tok, channel, text):
    """Post text; in test mode every post is rerouted to test_channel with a banner."""
    if cfg["mode"] != "live":
        text = f":test_tube: *[IC-ROTA TEST — would post to <#{channel}>]*\n{text}"
        channel = cfg["test_channel"]
    return slack_call("chat.postMessage", tok, channel=channel, text=text,
                      unfurl_links=False)


def get_topic(tok, channel):
    req = urllib.request.Request(
        f"https://slack.com/api/conversations.info?channel={channel}",
        headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read())
    if not out.get("ok"):
        raise RuntimeError(f"slack conversations.info: {out.get('error')}")
    return out["channel"].get("topic", {}).get("value", "")


# ---------------------------------------------------------------- identity
def email_to_slack(cfg, email):
    """cfg slack_ids override map first, then people.yaml. Fail-loud if unmapped."""
    override = (cfg.get("slack_ids") or {}).get(email)
    if override:
        return override
    import yaml
    if os.path.exists(PEOPLE):
        doc = yaml.safe_load(open(PEOPLE)) or {}
        for p in doc.get("people", []) if isinstance(doc, dict) else doc:
            if p.get("email") == email and p.get("slack_id"):
                return p["slack_id"]
    raise RuntimeError(
        f"no Slack ID for {email} — add it under slack_ids: in ic_rota.yaml")


# ---------------------------------------------------------------- rota sources
def rota_periods(cfg):
    """Return [{start,end,email}] sorted by start; datetimes tz-aware IST."""
    src = cfg.get("rota_source", "static")
    if src == "opsgenie":
        return _opsgenie_periods(cfg)
    if src == "static":
        return _static_periods(cfg)
    raise RuntimeError(f"unknown rota_source: {src}")


def _static_periods(cfg):
    out = []
    for e in cfg.get("static_rota") or []:
        start = datetime.strptime(str(e["start"]), "%Y-%m-%d").replace(tzinfo=IST)
        # end date is INCLUSIVE: stint covers end-day until midnight
        end = datetime.strptime(str(e["end"]), "%Y-%m-%d").replace(tzinfo=IST) \
            + timedelta(days=1)
        out.append({"start": start, "end": end, "email": e["email"]})
    return sorted(out, key=lambda p: p["start"])


def _opsgenie_key():
    for name in ("opsgenie_ic_api_key", "opsgenie_api_key"):
        p = os.path.join(SECRETS, name)
        if os.path.exists(p):
            return open(p).read().strip()
    raise RuntimeError("no Opsgenie key in ~/.secrets")


def _opsgenie_periods(cfg):
    og = cfg["opsgenie"]
    url = (f"https://api.opsgenie.com/v2/schedules/{og['schedule_id']}/timeline?"
           + urllib.parse.urlencode({
               "scheduleIdentifierType": "id",
               "interval": og.get("interval_weeks", 4),
               "intervalUnit": "weeks"}))
    req = urllib.request.Request(
        url, headers={"Authorization": f"GenieKey {_opsgenie_key()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    # The schedule mixes the real rota with permanent "Always Oncall" escalation
    # rotations — keep only the one named in rotation_filter.
    want_rot = og.get("rotation_filter")
    periods = []
    for rot in d["data"]["finalTimeline"]["rotations"]:
        if want_rot and rot.get("name") != want_rot:
            continue
        for p in rot.get("periods", []):
            periods.append({
                # Opsgenie ISO stamps carry their own offset; normalize to IST
                "start": datetime.fromisoformat(p["startDate"]).astimezone(IST),
                "end": datetime.fromisoformat(p["endDate"]).astimezone(IST),
                "email": p["recipient"]["name"],
            })
    periods.sort(key=lambda p: p["start"])
    # The timeline splits a stint at 'now' (historical/default) — merge adjacent
    # periods for the same person so date math sees whole stints.
    merged = []
    for p in periods:
        if merged and merged[-1]["email"] == p["email"] \
                and abs((p["start"] - merged[-1]["end"]).total_seconds()) < 60:
            merged[-1]["end"] = p["end"]
        else:
            merged.append(dict(p))
    return merged


def current_and_next(periods, now):
    cur = nxt = None
    for p in periods:
        if p["start"] <= now < p["end"]:
            cur = p
        elif p["start"] > now and (nxt is None or p["start"] < nxt["start"]):
            nxt = p
    return cur, nxt


def fmt_range(p):
    """Opsgenie stints hand over mid-day (12:00 IST); static ones at midnight.
    Show times only when the boundary isn't midnight."""
    def f(dt, midnight_adjust):
        if dt.hour == 0 and dt.minute == 0:
            return (dt - timedelta(minutes=1) if midnight_adjust else dt) \
                .strftime("%a %-d %b")
        return dt.strftime("%a %-d %b %H:%M")
    return f"{f(p['start'], False)} → {f(p['end'], True)} (IST)"


# ---------------------------------------------------------------- actions
def do_remind(cfg, tok, now, force=False):
    periods = rota_periods(cfg)
    _, nxt = current_and_next(periods, now)
    if not nxt:
        print("remind: no upcoming stint in rota window")
        return
    lead = timedelta(days=cfg.get("remind_days_before", 2))
    if not force and not (timedelta(0) < nxt["start"] - now <= lead):
        print(f"remind: next stint ({nxt['email']} @ {nxt['start']:%Y-%m-%d}) "
              f"outside {lead.days}-day window — nothing to do")
        return
    # An override mid-stint splits the on-duty person's stint in finalTimeline;
    # the resumption fragment then reads as a fresh stint starting inside the
    # window, with a new start date → new dedup key → spurious re-reminder.
    # Overrides aren't tagged in finalTimeline (type is only historical/default),
    # so detect resumption by adjacency: if this person already held a period
    # ending within the reminder window of this start, they're resuming, not
    # starting — a heads-up would be noise.
    if not force:
        prev_end = max((p["end"] for p in periods
                        if p["email"] == nxt["email"] and p["end"] <= nxt["start"]),
                       default=None)
        if prev_end is not None and nxt["start"] - prev_end <= lead:
            print(f"remind: {nxt['email']} resumes {nxt['start']:%Y-%m-%d %H:%M} "
                  f"after an override fragment (prev period ended "
                  f"{prev_end:%Y-%m-%d %H:%M}) — skipping")
            return
    st = load_state()
    key = f"{nxt['start']:%Y-%m-%d}|{nxt['email']}"
    if not force and key in st["reminded"]:
        print(f"remind: already posted for {key}")
        return
    uid = email_to_slack(cfg, nxt["email"])
    og_link = cfg.get("opsgenie", {}).get("schedule_url", "")
    sched_ref = f"<{og_link}|Incident-Commanders schedule>" if og_link \
        else "the Incident-Commanders Opsgenie schedule"
    text = (
        f":rotating_light: <@{uid}> — you're *Incident Commander* "
        f"*{fmt_range(nxt)}*.\n\n"
        f"Fully available (weekend too)? React :thumbsup: — done.\n\n"
        f"On leave? Reply here to swap, then add an *Override* on "
        f"{sched_ref}."
    )
    post(cfg, tok, cfg["slack"]["ic_channel"], text)
    st["reminded"][key] = now.isoformat()
    save_state(st)
    print(f"remind: posted for {key} (mode={cfg['mode']})")


def do_sync_topic(cfg, tok, now):
    periods = rota_periods(cfg)
    cur, _ = current_and_next(periods, now)
    if not cur:
        print("sync-topic: no current IC in rota window — skipping")
        return
    uid = email_to_slack(cfg, cur["email"])
    want = cfg.get("topic_template", "Incident Commander: {mention}") \
        .format(mention=f"<@{uid}>")
    chan = cfg["slack"]["oncall_channel"]
    have = get_topic(tok, chan)
    if have.strip() == want.strip():
        print(f"sync-topic: topic already correct ({want})")
        return
    st = load_state()
    if cfg["mode"] != "live" and st.get("last_topic") == want:
        # test mode can't fix the real topic, so the mismatch persists —
        # echo each desired value once instead of every scheduled run
        print(f"sync-topic: test mode, already echoed → {want}")
        return
    old_uid = None
    import re
    m = re.search(r"<@(U[A-Z0-9]+)>", have)
    if m:
        old_uid = m.group(1)
    if cfg["mode"] == "live":
        slack_call("conversations.setTopic", tok, channel=chan, topic=want)
    handover = (f":mega: *IC handover:* "
                f"{f'<@{old_uid}> → ' if old_uid and old_uid != uid else ''}"
                f"<@{uid}> is now Incident Commander ({fmt_range(cur)}).\n"
                f"<#{chan}> topic "
                f"{'updated automatically' if cfg['mode'] == 'live' else 'WOULD be set to: ' + want}.")
    # One-shot: if state carries edit_handover_ts, update that earlier handover
    # message in place instead of posting a new one (falls back to posting if
    # the message was deleted meanwhile). Cleared after use either way.
    edit_ts = st.pop("edit_handover_ts", None)
    edited = False
    if cfg["mode"] == "live" and edit_ts:
        try:
            slack_call("chat.update", tok, channel=cfg["slack"]["ic_channel"],
                       ts=edit_ts, text=handover)
            edited = True
        except RuntimeError as e:
            print(f"sync-topic: edit of {edit_ts} failed ({e}) — posting fresh")
    if not edited:
        post(cfg, tok, cfg["slack"]["ic_channel"], handover)
    st["last_topic"] = want
    save_state(st)
    print(f"sync-topic: {'set' if cfg['mode'] == 'live' else 'test-echoed'} → {want}"
          f"{' (handover edited in place)' if edited else ''}")


def do_status(cfg, now):
    periods = rota_periods(cfg)
    cur, nxt = current_and_next(periods, now)
    print(f"mode={cfg['mode']}  rota_source={cfg.get('rota_source')}  "
          f"now={now:%Y-%m-%d %H:%M} IST")
    for p in periods:
        tag = " <- CURRENT" if p is cur else (" <- NEXT" if p is nxt else "")
        print(f"  {p['start']:%Y-%m-%d} → {(p['end'] - timedelta(minutes=1)):%Y-%m-%d}"
              f"  {p['email']}{tag}")
    if cur:
        print(f"current IC: {cur['email']}")
    if nxt:
        print(f"next IC   : {nxt['email']} (starts {nxt['start']:%Y-%m-%d}, "
              f"in {(nxt['start'] - now).days}d)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--remind", action="store_true")
    ap.add_argument("--sync-topic", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--post-test", action="store_true",
                    help="force-post samples of both messages (test channel)")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    now = datetime.now(IST)
    if args.status:
        do_status(cfg, now)
        return
    tok = secret("relay_slack_bot_token")
    if args.post_test:
        if cfg["mode"] == "live":
            sys.exit("--post-test refused in live mode")
        do_remind(cfg, tok, now, force=True)
        do_sync_topic(cfg, tok, now)
        return
    if args.remind:
        do_remind(cfg, tok, now)
    if args.sync_topic:
        do_sync_topic(cfg, tok, now)
    if not (args.remind or args.sync_topic):
        ap.print_help()


if __name__ == "__main__":
    main()

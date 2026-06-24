#!/usr/bin/env python3
"""relay_bot.py — shared owner-only Slack bot (Socket Mode) for the work-context skills.

Consumers: /ticketize and /doc-sync-sweep. Roles:
  --post <date>          : read management/standup/<date>/ticket-candidates.md and post the
                           OPEN candidates to the ticketize channel with Approve/Reject buttons.
  --post-docsync <run-id>: read state/doc_sync_discovered.json and post one Approve/Reject card
                           per newly-discovered doc to the doc-sweep channel (config doc_sync.yaml).
                           Approve → promote needs_confirm→monitor; Reject → move to excluded
                           (both via derive/doc_sync_state.py move). Owner-gated, RELAY_APPLY_MODE-gated.
  --post-findings <run-id>: read state/doc_sync_findings_<run-id>.json and post one Approve/Reject
                           card per drift finding (already-open dupes excluded). Approve → post the
                           inline Confluence comment via bin/doc_sync_apply.py (footer fallback) +
                           record; Reject → record rejected. Owner-gated, RELAY_APPLY_MODE-gated.
  --post-usergroups <run-id>: read state/last_slack_discover_usergroups.json and post one card
                           per pending discovered user-group to the channel in that JSON
                           (slack.usergroup_discover_channel). Buttons: Manager → owner_member,
                           Team → ingest filter, Reject → skiplist. Owner-gated, RELAY_APPLY_MODE-gated.
  --post-housekeeping <run-id>: read state/housekeeping_suggestions_<run-id>.json (written by the
                           Claude classification layer) and post one Approve/Reject card per
                           cleanup suggestion to the #rollup channel (sources.yaml rollup_channel).
                           Approve → bin/housekeeping_apply.py git-safe delete/truncate; Reject →
                           recorded so it's never re-proposed. Owner-gated, RELAY_APPLY_MODE-gated.
  (no args)              : run the Socket Mode listener — handle tkz:, dsc:, dsf:, ugd: and hk:
                           button clicks (owner-only), apply the decision, and update the message.

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


HEAD_RE = re.compile(r"^##\s+([A-Z]\d+)\s+·\s+(.*?)\s*(?:—\s*(\w+).*)?$")
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


def all_open_candidates():
    """Every still-`pending` candidate across all daily files, newest date first,
    deduped by fingerprint. Each row is tagged with its origin `date` so its buttons
    apply against the right file."""
    import glob
    base = os.path.join(ROOT, "management/standup")
    rows, seen = [], set()
    paths = sorted(glob.glob(os.path.join(base, "*", "ticket-candidates.md")), reverse=True)
    for p in paths:
        d = os.path.basename(os.path.dirname(p))
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            continue
        for c in (parse_candidates(d) or []):
            if c.get("decision", "pending").lower() != "pending":
                continue
            fp = c.get("fingerprint")
            if fp and fp in seen:
                continue
            if fp:
                seen.add(fp)
            c["date"] = d
            rows.append(c)
    return rows


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


def linkify_keys(text, base):
    """Wrap bare Jira keys (e.g. EX-1234) in Slack links to their browse URL."""
    if not text or not base:
        return text
    base = base.rstrip("/")
    return re.sub(r"\b([A-Z][A-Z0-9]+-\d+)\b",
                  lambda m: f"<{base}/browse/{m.group(1)}|{m.group(1)}>", text)


def resolve_card_blocks(blocks, act_block_id, status_md):
    """Replace the clicked card's button row (the actions block whose block_id ==
    `act_block_id`) with a resolution line, leaving every other card's buttons live.
    Also refreshes the leading 'N open' count in the footer (block_id 'footer_open'),
    keeping whatever per-card-type suffix that footer carries. Shared by ticketize
    (act_<fp>) and the doc-sync discovery (dscact_<id>) / findings (dsfact_<key>) cards."""
    new = []
    for b in (blocks or []):
        if b.get("block_id") == act_block_id:
            new.append({"type": "context", "block_id": f"done_{act_block_id}",
                        "elements": [{"type": "mrkdwn", "text": status_md}]})
        else:
            new.append(b)
    remaining = sum(1 for b in new if b.get("type") == "actions")
    for b in new:
        if b.get("block_id") == "footer_open":
            cur = b["elements"][0]["text"]
            b["elements"][0]["text"] = re.sub(r"^\d+\s+open", f"{remaining} open", cur)
    return new


def fetch_message_blocks(client, channel, ts):
    """Fetch a posted message's current blocks (the view-submission body lacks them)."""
    try:
        r = client.conversations_history(channel=channel, latest=ts, inclusive=True, limit=1)
        msgs = r.get("messages", [])
        if msgs and msgs[0].get("ts") == ts:
            return msgs[0].get("blocks")
    except Exception as e:
        print(f"fetch blocks failed: {e}", file=sys.stderr)
    return None


def update_card(client, channel, ts, act_block_id, status_md, blocks=None):
    """In-place message edit: resolve the clicked card (its actions block_id == act_block_id).
    Best-effort — never raises. Shared by ticketize + doc-sync discovery/findings cards."""
    blocks = blocks if blocks is not None else fetch_message_blocks(client, channel, ts)
    if not blocks:
        return
    try:
        client.chat_update(channel=channel, ts=ts, text="(updated)",
                           blocks=resolve_card_blocks(blocks, act_block_id, status_md))
    except Exception as e:
        print(f"card update failed: {e}", file=sys.stderr)


def build_blocks(date, opens):
    """`date` = the as-of date (newest run). `opens` may include carried-over candidates
    from earlier days; each carries its own `c['date']` used for its buttons + label."""
    carried = sum(1 for c in opens if c.get("date", date) != date)
    sub = "Everything still open. *Approve* → I create the Jira ticket. *Reject* → I drop it. (only you can act)"
    if carried:
        sub += f"\n_{carried} carried over from earlier days._"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🎫 Ticket candidates — as of {date}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": sub}]},
        {"type": "divider"},
    ]
    for c in opens:
        origin = c.get("date", date)
        mmdd = origin[5:]
        tier = c.get("code_tier", "")
        gloss = next((v for k, v in TIER_GLOSS.items() if k in tier), tier)
        head = f"*{c['label']}  ·  {c.get('summary','(no summary)')}*"
        head += f"   🗓️ {origin}" + ("  · _carried over_" if origin != date else "")
        lines = [head, ""]
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
        asg = c.get("assignee") or "—"
        sug = (c.get("suggested_assignee") or "").split(" (")[0]
        asg_line = f"👤 *Assignee (editable on approve):*  {asg}"
        if sug:
            asg_line += f"   ·   _suggested: {sug}_"
        lines.append(asg_line)
        if gloss:
            lines.append(f"⚖️ *Risk:*  {gloss}")
        refs = fmt_refs(c.get("evidence", ""))
        if refs:
            lines.append(f"🔗 *Refs:*  {refs}")
        fp = c.get("fingerprint", "")
        suffix = f" · {mmdd}" if origin != date else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        blocks.append({"type": "actions", "block_id": f"act_{fp}", "elements": [
            {"type": "button", "style": "primary",
             "text": {"type": "plain_text", "text": f"✓ Approve {c['label']}{suffix}"},
             "action_id": f"tkz:{origin}:{fp}:approve:{c['label']}", "value": c["label"]},
            {"type": "button", "style": "danger",
             "text": {"type": "plain_text", "text": f"✕ Reject {c['label']}{suffix}"},
             "action_id": f"tkz:{origin}:{fp}:reject:{c['label']}", "value": c["label"]},
        ]})
        blocks.append({"type": "divider"})
    blocks.append({"type": "context", "block_id": "footer_open", "elements": [
        {"type": "mrkdwn", "text": f"{len(opens)} open · created tickets land in the active sprint"}]})
    return blocks


def do_post(date):
    from slack_sdk import WebClient
    cfg = load_cfg()
    opens = all_open_candidates()          # every still-open candidate, all days, newest first
    today_exists = os.path.exists(candidates_path(date))
    client = WebClient(token=secret("relay_slack_bot_token"))
    ch = cfg["slack"]["channel_id"]
    if not opens:
        if today_exists:
            client.chat_postMessage(channel=ch, text=f"No open ticket candidates as of {date}. ✅")
            print("posted: none-open")
        else:
            print(f"no candidate file for {date}")
        return
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
    # Slack caps a message at 50 blocks; ~3 blocks/candidate + framing → chunk at 15
    # (mirrors do_post_findings). In-place resolution edits whichever chunk message the
    # clicked button lives in, using the ts Slack sends in the interaction payload — so
    # splitting across messages does not break the act_<fp> → card mapping.
    CHUNK = 15
    chunks = [opens[i:i + CHUNK] for i in range(0, len(opens), CHUNK)]
    for n, chunk in enumerate(chunks, 1):
        suffix = f" [{n}/{len(chunks)}]" if len(chunks) > 1 else ""
        client.chat_postMessage(
            channel=ch, text=f"Ticket candidates — {date} ({len(opens)} open){suffix}",
            blocks=build_blocks(date, chunk))
    print(f"posted {len(opens)} candidates to {ch} in {len(chunks)} message(s)")


# ---------- doc-sync discovery cards ----------
DOCSYNC_CFG = os.path.join(ROOT, "work-context/config/doc_sync.yaml")
DOCSYNC_INVENTORY = os.path.join(ROOT, "work-context/config/doc_sync_inventory.yaml")
DOCSYNC_STATE = os.path.join(ROOT, "work-context/derive/doc_sync_state.py")
DOCSYNC_APPLY = os.path.join(ROOT, "bin/doc_sync_apply.py")
def _docsync_findings_path(run_id):
    return os.path.join(ROOT, f"work-context/state/doc_sync_findings_{run_id}.json")
SEV_DOT = {"major": "🔴", "schema_drift": "🔴", "medium": "🟡", "minor": "🔵"}


def load_docsync_cfg():
    import yaml
    return yaml.safe_load(open(DOCSYNC_CFG))


def _docsync_pending():
    """Every doc still awaiting a decision — the inventory's full `needs_confirm` bucket,
    not just this run's new finds. Mirrors ticketize's all_open_candidates: an ignored
    discovery keeps re-appearing each sweep until Approve (→monitor) or Reject (→excluded)
    clears it out of needs_confirm. Each entry: {id, title, author, repo, webUrl}."""
    import yaml
    inv = yaml.safe_load(open(DOCSYNC_INVENTORY)) or {}
    try:                                   # host from sources.yaml via the apply helper — no hardcoded org
        import doc_sync_apply as dsa
        base = f"https://{dsa.host()}/wiki/pages/viewpage.action?pageId="
    except Exception:
        base = ""
    out = []
    for e in (inv.get("needs_confirm") or []):
        pid = str(e.get("id"))
        out.append({"id": pid, "title": e.get("title", "(untitled)"),
                    "author": e.get("author"), "repo": e.get("repo", "?"),
                    "webUrl": (base + pid) if base else ""})
    return out


def build_docsync_blocks(run_id, docs):
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🔎 Docs awaiting review — {run_id}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "Team-authored design docs still awaiting your call (new + carried over from earlier sweeps). *Approve* → I add it to the monitored set (next sweep checks it). *Reject* → I drop it to excluded. (only you can act)"}]},
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
    blocks.append({"type": "context", "block_id": "footer_open", "elements": [
        {"type": "mrkdwn", "text": f"{len(docs)} open · Approve → monitor list · Reject → excluded"}]})
    return blocks


def do_post_docsync(run_id):
    from slack_sdk import WebClient
    cfg = load_docsync_cfg()
    ch = cfg["slack"]["channel_id"]
    if not ch:
        print("no doc_sync channel_id configured — skipping", file=sys.stderr); sys.exit(1)
    docs = _docsync_pending()
    client = WebClient(token=secret("relay_slack_bot_token"))
    if not docs:
        print("posted: no docs awaiting review (needs_confirm is empty)"); return
    r = client.chat_postMessage(channel=ch, text=f"{len(docs)} docs awaiting review — {run_id}",
                                blocks=build_docsync_blocks(run_id, docs))
    print(f"posted {len(docs)} discovery cards to {ch} ts={r['ts']}")


# ---------- doc-sync drift-finding cards ----------
def _docsync_findings(run_id):
    p = _docsync_findings_path(run_id)
    if not os.path.exists(p):
        return None
    d = json.load(open(p))
    return d.get("findings", d) if isinstance(d, dict) else d


def build_findings_blocks(run_id, findings):
    sev = lambda f: SEV_DOT.get(f.get("severity"), "•")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📝 Doc-sync drift findings — {run_id}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "Approve → I post the inline comment on the doc. Reject → I drop it. (only you can act)"}]},
        {"type": "divider"},
    ]
    for f in findings:
        edit = (f.get("suggested_edit") or "").strip()
        edit = (edit[:280].rstrip() + "…") if len(edit) > 280 else edit
        lines = [f"{sev(f)} *{f.get('page_title','?')}*  ·  _{f.get('severity','')}/{f.get('check_type','')}_",
                 f.get("finding_title", ""), f"📄 <{f.get('page_url','')}|open doc>"]
        if edit:
            lines.insert(2, f"_fix:_ {edit}")
        dup = f.get("possible_dup_of")
        if dup:
            lines.insert(1, f"⚠️ _possible repeat of an open comment: “{(dup.get('title') or '')[:70]}” — Reject if so_")
        k = f["finding_key"]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        blocks.append({"type": "actions", "block_id": f"dsfact_{k[:20]}", "elements": [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "✓ Approve → comment"},
             "action_id": f"dsf:{run_id}:{k}:approve", "value": k},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "✕ Reject"},
             "action_id": f"dsf:{run_id}:{k}:reject", "value": k},
        ]})
        blocks.append({"type": "divider"})
    blocks.append({"type": "context", "block_id": "footer_open", "elements": [
        {"type": "mrkdwn", "text": f"{len(findings)} open · Approve posts a live Confluence inline comment"}]})
    return blocks


def do_post_findings(run_id):
    from slack_sdk import WebClient
    cfg = load_docsync_cfg()
    ch = cfg["slack"]["channel_id"]
    if not ch:
        print("no doc_sync channel_id configured — skipping", file=sys.stderr); sys.exit(1)
    findings = _docsync_findings(run_id)
    if findings is None:
        print(f"no findings file for {run_id}", file=sys.stderr); sys.exit(1)
    actionable = [f for f in findings if not f.get("already_open")]
    client = WebClient(token=secret("relay_slack_bot_token"))
    if not actionable:
        client.chat_postMessage(channel=ch, text=f"Doc-sync {run_id}: no new drift findings to action. ✅")
        print("posted: none-actionable"); return
    # Slack caps a message at 50 blocks; each finding is 3 blocks (section+actions+divider)
    # plus ~4 framing blocks, so chunk at 15 findings/message to stay under the limit.
    CHUNK = 15
    chunks = [actionable[i:i + CHUNK] for i in range(0, len(actionable), CHUNK)]
    for n, chunk in enumerate(chunks, 1):
        suffix = f" [{n}/{len(chunks)}]" if len(chunks) > 1 else ""
        r = client.chat_postMessage(
            channel=ch,
            text=f"Doc-sync drift findings — {run_id} ({len(actionable)}){suffix}",
            blocks=build_findings_blocks(run_id, chunk))
    print(f"posted {len(actionable)} finding cards to {ch} in {len(chunks)} message(s)")


# ---------- user-group (subteam) discovery cards ----------
UGD_PROPOSAL = os.path.join(ROOT, "work-context/state/last_slack_discover_usergroups.json")
UGD_SCRIPT = os.path.join(ROOT, "work-context/derive/slack_discover_usergroups.py")
UGD_PY = os.path.join(ROOT, "work-context/.venv/bin/python")


def _ugd_pending():
    """Pending discovered user-groups + target channel from the proposal JSON.
    Returns (channel, [groups]) where each group carries a `_proposed` bucket tag."""
    if not os.path.exists(UGD_PROPOSAL):
        return None, []
    d = json.load(open(UGD_PROPOSAL))
    pending = []
    for bucket in ("manager", "team", "ambiguous"):
        for g in d.get(bucket, []) or []:
            pending.append({**g, "_proposed": bucket})
    return d.get("channel") or "", pending


def build_ugd_blocks(run_id, groups):
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"👥 Discovered Slack user-groups — {run_id}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "User-groups you/your team belong to, not yet tracked. *Manager* → adds it as `owner_member` (a ping counts as an ask to you). *Team* → adds it to the team-involved ingest filter only. *Reject* → never proposed again. (only you can act)"}]},
        {"type": "divider"},
    ]
    for g in groups:
        gid = str(g.get("id"))
        handle = g.get("handle", gid)
        size = g.get("size", 0)
        reps = g.get("reports", 0)
        proposed = g.get("_proposed", "")
        flags = []
        if g.get("owner_in"):
            flags.append("you're a member")
        flags.append(f"{reps} of your reports")
        if g.get("broad"):
            flags.append("⚠ broad/likely-skip")
        lines = [f"*@{handle}*   ·   `{gid}`",
                 f"👥 {size} members · {' · '.join(flags)}   ·   _proposed: {proposed}_"]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        blocks.append({"type": "actions", "block_id": f"ugdact_{gid}", "elements": [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "👔 Manager"},
             "action_id": f"ugd:{run_id}:{gid}:manager", "value": handle},
            {"type": "button", "text": {"type": "plain_text", "text": "👥 Team"},
             "action_id": f"ugd:{run_id}:{gid}:team", "value": handle},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "✕ Reject"},
             "action_id": f"ugd:{run_id}:{gid}:reject", "value": handle},
        ]})
        blocks.append({"type": "divider"})
    blocks.append({"type": "context", "block_id": "ugdfooter", "elements": [
        {"type": "mrkdwn", "text": f"{len(groups)} pending · Manager→owner_member · Team→ingest filter · Reject→never re-proposed"}]})
    return blocks


def resolve_ugd_blocks(blocks, gid, status_md):
    """Replace one group's button row (block_id ugdact_<gid>) with a resolution line."""
    new = []
    for b in (blocks or []):
        if b.get("block_id") == f"ugdact_{gid}":
            new.append({"type": "context", "block_id": f"ugddone_{gid}",
                        "elements": [{"type": "mrkdwn", "text": status_md}]})
        else:
            new.append(b)
    remaining = sum(1 for b in new if b.get("type") == "actions")
    for b in new:
        if b.get("block_id") == "ugdfooter":
            b["elements"][0]["text"] = f"{remaining} pending · Manager→owner_member · Team→ingest filter · Reject→never re-proposed"
    return new


def update_ugd_card(client, channel, ts, gid, status_md, blocks=None):
    """In-place message edit: resolve one group's card. Best-effort — never raises."""
    blocks = blocks if blocks is not None else fetch_message_blocks(client, channel, ts)
    if not blocks:
        return
    try:
        client.chat_update(channel=channel, ts=ts, text="User-groups (updated)",
                           blocks=resolve_ugd_blocks(blocks, gid, status_md))
    except Exception as e:
        print(f"ugd card update failed: {e}", file=sys.stderr)


def do_post_usergroups(run_id):
    from slack_sdk import WebClient
    ch, pending = _ugd_pending()
    if not ch:
        print("no usergroup_discover_channel configured — skipping", file=sys.stderr)
        return
    if not pending:
        print("posted: no pending user-groups this run")
        return
    client = WebClient(token=secret("relay_slack_bot_token"))
    r = client.chat_postMessage(channel=ch, text=f"{len(pending)} user-groups to review — {run_id}",
                                blocks=build_ugd_blocks(run_id, pending))
    print(f"posted {len(pending)} user-group cards to {ch} ts={r['ts']}")


# ---------- housekeeping suggestion cards ----------
SOURCES_CFG = os.path.join(ROOT, "work-context/config/sources.yaml")
HK_APPLY = os.path.join(ROOT, "bin/housekeeping_apply.py")
HK_CAT_EMOJI = {"db_backup": "💾", "log": "📜", "pycache": "🐍", "cache": "♻️", "derived_stale": "📊",
                "state_orphan": "🗂️", "preview_bloat": "🖼️", "untracked_large": "📦",
                "worktree": "🌳", "large_file": "🐘"}
HK_RISK_DOT = {"low": "🟢", "medium": "🟡", "high": "🔴"}


def _hk_suggestions_path(run_id):
    return os.path.join(ROOT, f"work-context/state/housekeeping_suggestions_{run_id}.json")


def rollup_channel():
    """#rollup channel id — env ROLLUP_CHANNEL wins, else config/sources.yaml slack.rollup_channel."""
    if os.environ.get("ROLLUP_CHANNEL"):
        return os.environ["ROLLUP_CHANNEL"]
    try:
        import yaml
        cfg = yaml.safe_load(open(SOURCES_CFG)) or {}
        return (cfg.get("slack") or {}).get("rollup_channel", "")
    except Exception:
        return ""


def _hk_suggestions(run_id):
    p = _hk_suggestions_path(run_id)
    if not os.path.exists(p):
        return None
    d = json.load(open(p))
    return d.get("suggestions", d) if isinstance(d, dict) else d


def build_housekeeping_blocks(run_id, sugs):
    total = human_bytes(sum(int(s.get("size_bytes") or 0) for s in sugs))
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🧹 Housekeeping suggestions — {run_id}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"Regenerable artifacts I judged safe to clean (*{total}* reclaimable). "
                    "*Approve* → I delete/truncate it. *Reject* → I skip it (won't re-propose). "
                    "Only git-ignored/untracked files are ever touched. (only you can act)"}]},
        {"type": "divider"},
    ]
    for s in sugs:
        cat = s.get("category", "")
        emoji = HK_CAT_EMOJI.get(cat, "🧹")
        dot = HK_RISK_DOT.get(s.get("risk", "low"), "🟢")
        age = s.get("age_days")
        age_md = f" · {age}d old" if isinstance(age, int) and age >= 0 else ""
        verb = {"truncate": "truncate", "worktree_remove": "remove worktree"}.get(s.get("action"), "delete")
        lines = [f"{emoji} *`{s.get('path','?')}`*",
                 f"{dot} {s.get('size_h','?')}{age_md} · _{cat}_ · git: `{s.get('git','?')}`"]
        if s.get("reason"):
            lines.append(f"💡 {s['reason']}")
        key = s.get("key", "")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        blocks.append({"type": "actions", "block_id": f"hkact_{key}", "elements": [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": f"✓ Approve ({verb})"},
             "action_id": f"hk:{run_id}:{key}:approve", "value": key},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "✕ Reject"},
             "action_id": f"hk:{run_id}:{key}:reject", "value": key},
        ]})
        blocks.append({"type": "divider"})
    blocks.append({"type": "context", "block_id": "footer_open", "elements": [
        {"type": "mrkdwn", "text": f"{len(sugs)} open · Approve reclaims space · only ignored/untracked artifacts are deletable"}]})
    return blocks


def human_bytes(b):
    for unit, scale in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if b >= scale:
            return f"{b / scale:.1f}{unit}"
    return f"{b}B"


def do_post_housekeeping(run_id):
    from slack_sdk import WebClient
    ch = rollup_channel()
    if not ch:
        print("no rollup_channel configured (sources.yaml slack.rollup_channel / $ROLLUP_CHANNEL) — skipping",
              file=sys.stderr)
        sys.exit(1)
    sugs = _hk_suggestions(run_id)
    if sugs is None:
        print(f"no housekeeping suggestions file for {run_id}", file=sys.stderr)
        sys.exit(1)
    client = WebClient(token=secret("relay_slack_bot_token"))
    if not sugs:
        client.chat_postMessage(channel=ch, text=f"Housekeeping {run_id}: nothing further to clean. ✅")
        print("posted: none-actionable")
        return
    # Slack caps a message at 50 blocks; each suggestion is 3 blocks + ~4 framing → chunk at 15.
    CHUNK = 15
    chunks = [sugs[i:i + CHUNK] for i in range(0, len(sugs), CHUNK)]
    for n, chunk in enumerate(chunks, 1):
        suffix = f" [{n}/{len(chunks)}]" if len(chunks) > 1 else ""
        client.chat_postMessage(
            channel=ch,
            text=f"Housekeeping suggestions — {run_id} ({len(sugs)}){suffix}",
            blocks=build_housekeeping_blocks(run_id, chunk))
    print(f"posted {len(sugs)} housekeeping cards to {ch} in {len(chunks)} message(s)")


# ---------- listener ----------
def run_listener():
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    cfg = load_cfg()
    owner = cfg["slack"]["owner_slack_id"]
    jira_base = (cfg.get("jira") or {}).get("base_url", "")
    mode = os.environ.get("RELAY_APPLY_MODE", "dry")
    app = App(token=secret("relay_slack_bot_token"))

    def run_apply(date, fp, decision, epic_input=None, assignee_input=None):
        if mode != "live":
            extra = f", assignee='{assignee_input}'" if assignee_input else ""
            return True, f"🧪 dry — would {decision} (epic='{epic_input or ''}'{extra}); set RELAY_APPLY_MODE=live to action"
        cmd = [sys.executable, os.path.join(ROOT, "bin/ticketize_apply.py"),
               "--date", date, "--fingerprint", fp, "--decision", decision]
        if epic_input:
            cmd += ["--epic-input", epic_input]
        if assignee_input:
            cmd += ["--assignee-input", assignee_input]
        res = subprocess.run(cmd, capture_output=True, text=True)
        out = (res.stdout.strip() or res.stderr.strip() or "")
        note = out.splitlines()[-1] if out else ""
        return res.returncode == 0, linkify_keys(note, jira_base)

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
        cand_assignee = (c.get("assignee") or "")
        suggested_dev = (c.get("suggested_assignee") or "").split(" (")[0]
        asg_hint = (f"Default = {cand_assignee or 'unassigned'}."
                    + (f" Suggested delegate: {suggested_dev}." if suggested_dev else "")
                    + " A people.yaml canonical handle (e.g. jane-doe); blank keeps the default.")
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
                {"type": "input", "block_id": "assignee", "optional": True,
                 "label": {"type": "plain_text", "text": "Assignee"},
                 "element": {"type": "plain_text_input", "action_id": "assignee_val",
                             "initial_value": cand_assignee,
                             "placeholder": {"type": "plain_text", "text": "canonical handle e.g. jane-doe"}},
                 "hint": {"type": "plain_text", "text": asg_hint}},
            ],
        })

    @app.view("tkz_apply")
    def on_submit(ack, body, client, view):
        ack()
        m = json.loads(view["private_metadata"])
        if body["user"]["id"] != owner:
            return
        vals = view["state"]["values"]
        epic_in = (vals["epic"]["epic_val"].get("value") or "").strip()
        asg_in = (vals.get("assignee", {}).get("assignee_val", {}).get("value") or "").strip()
        ok, note = run_apply(m["date"], m["fp"], "approve", epic_in or None, asg_in or None)
        client.chat_postMessage(channel=m["channel"], thread_ts=m["msg_ts"],
                                text=f"{'✅' if ok else '⚠️'} *{m['label']}* approved — {note}")
        if ok and mode == "live":
            update_card(client, m["channel"], m["msg_ts"], f"act_{m['fp']}",
                        f"✅ *{m['label']}* approved — {note}")

    @app.action(re.compile(r"^tkz:.*:reject:"))
    def on_reject(ack, body, client, action):
        ack()
        if not is_owner(body, client):
            return
        _, date, fp, verb, label = action["action_id"].split(":", 4)
        ok, note = run_apply(date, fp, "reject")
        client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"],
                                text=f"🗑️ *{label}* rejected — {note}")
        if ok and mode == "live":
            update_card(client, body["channel"]["id"], body["message"]["ts"], f"act_{fp}",
                        f"🗑️ *{label}* rejected", blocks=body["message"]["blocks"])

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
        if ok and mode == "live":
            update_card(client, body["channel"]["id"], body["message"]["ts"], f"dscact_{pid}",
                        f"✅ `{pid}` → *monitor*", blocks=body["message"]["blocks"])

    @app.action(re.compile(r"^dsc:.*:reject$"))
    def on_docsync_reject(ack, body, client, action):
        ack()
        if not is_owner(body, client):
            return
        _, run_id, pid, verb = action["action_id"].split(":", 3)
        ok, note = docsync_apply(pid, "reject", run_id)
        client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"],
                                text=f"{'🗑️' if ok else '⚠️'} `{pid}` moved to *excluded* — {note}")
        if ok and mode == "live":
            update_card(client, body["channel"]["id"], body["message"]["ts"], f"dscact_{pid}",
                        f"🗑️ `{pid}` → *excluded*", blocks=body["message"]["blocks"])

    # ---- doc-sync drift findings: Approve → post Confluence inline comment, Reject → drop ----
    def findings_apply(run_id, key, decision):
        if mode != "live":
            verb = "post the inline comment for" if decision == "approve" else "drop"
            return True, f"🧪 dry — would {verb} {key[:10]}…; set RELAY_APPLY_MODE=live to action"
        cmd = [sys.executable, DOCSYNC_APPLY, "--finding-file", _docsync_findings_path(run_id),
               "--key", key, "--decision", decision, "--run-id", run_id]
        res = subprocess.run(cmd, capture_output=True, text=True)
        out = (res.stdout.strip() or res.stderr.strip() or "")
        return res.returncode == 0, (out.splitlines()[-1] if out else "")

    @app.action(re.compile(r"^dsf:.*:approve$"))
    def on_finding_approve(ack, body, client, action):
        ack()
        if not is_owner(body, client):
            return
        _, run_id, key, verb = action["action_id"].split(":", 3)
        ok, note = findings_apply(run_id, key, "approve")
        client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"],
                                text=f"{'✅ commented' if ok else '⚠️'} — {note}")
        if ok and mode == "live":
            update_card(client, body["channel"]["id"], body["message"]["ts"], f"dsfact_{key[:20]}",
                        f"✅ comment posted — {note}", blocks=body["message"]["blocks"])

    @app.action(re.compile(r"^dsf:.*:reject$"))
    def on_finding_reject(ack, body, client, action):
        ack()
        if not is_owner(body, client):
            return
        _, run_id, key, verb = action["action_id"].split(":", 3)
        ok, note = findings_apply(run_id, key, "reject")
        client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"],
                                text=f"{'🗑️ dropped' if ok else '⚠️'} — {note}")
        if ok and mode == "live":
            update_card(client, body["channel"]["id"], body["message"]["ts"], f"dsfact_{key[:20]}",
                        f"🗑️ dropped — {note}", blocks=body["message"]["blocks"])

    # ---- user-group discovery: Manager → owner_member, Team → ingest filter, Reject → skiplist ----
    UGD_FLAG = {"manager": "--apply-manager", "team": "--apply-team", "reject": "--skip"}

    def ugd_apply(gid, layer):
        if mode != "live":
            verb = {"manager": "apply @%s as MANAGER (owner_member)" % gid,
                    "team": "apply @%s as TEAM (ingest filter)" % gid,
                    "reject": "skip @%s (never re-proposed)" % gid}[layer]
            return True, f"🧪 dry — would {verb}; set RELAY_APPLY_MODE=live to action"
        # Strip LLM creds — slack_discover_usergroups asserts they're absent.
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
        cmd = [UGD_PY, UGD_SCRIPT, UGD_FLAG[layer], gid]
        res = subprocess.run(cmd, capture_output=True, text=True, env=env,
                             cwd=os.path.join(ROOT, "work-context"))
        out = (res.stdout.strip() or res.stderr.strip() or "")
        return res.returncode == 0, (out.splitlines()[-1] if out else "")

    def _on_ugd(layer, emoji, label):
        def handler(ack, body, client, action):
            ack()
            if not is_owner(body, client):
                return
            _, run_id, gid, _layer = action["action_id"].split(":", 3)
            handle = action.get("value") or gid
            ok, note = ugd_apply(gid, layer)
            client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"],
                                    text=f"{emoji if ok else '⚠️'} @{handle} → *{label}* — {note}")
            if ok and mode == "live":
                update_ugd_card(client, body["channel"]["id"], body["message"]["ts"], gid,
                                f"{emoji} @{handle} → {label} — {note}", blocks=body["message"]["blocks"])
        return handler

    app.action(re.compile(r"^ugd:.*:manager$"))(_on_ugd("manager", "👔", "manager (owner_member)"))
    app.action(re.compile(r"^ugd:.*:team$"))(_on_ugd("team", "👥", "team (ingest filter)"))
    app.action(re.compile(r"^ugd:.*:reject$"))(_on_ugd("reject", "🗑️", "rejected (skiplisted)"))

    # ---- housekeeping: Approve → git-safe delete/truncate, Reject → skip (won't re-propose) ----
    def hk_apply(run_id, key, decision):
        if mode != "live":
            verb = "delete/truncate" if decision == "approve" else "skip"
            return True, f"🧪 dry — would {verb} {key}; set RELAY_APPLY_MODE=live to action"
        cmd = [sys.executable, HK_APPLY, "--run-id", run_id, "--key", key, "--decision", decision]
        res = subprocess.run(cmd, capture_output=True, text=True)
        out = (res.stdout.strip() or res.stderr.strip() or "")
        return res.returncode == 0, (out.splitlines()[-1] if out else "")

    @app.action(re.compile(r"^hk:.*:approve$"))
    def on_hk_approve(ack, body, client, action):
        ack()
        if not is_owner(body, client):
            return
        _, run_id, key, verb = action["action_id"].split(":", 3)
        ok, note = hk_apply(run_id, key, "approve")
        client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"],
                                text=f"{'✅ cleaned' if ok else '⚠️'} — {note}")
        if ok and mode == "live":
            update_card(client, body["channel"]["id"], body["message"]["ts"], f"hkact_{key}",
                        f"✅ cleaned — {note}", blocks=body["message"]["blocks"])

    @app.action(re.compile(r"^hk:.*:reject$"))
    def on_hk_reject(ack, body, client, action):
        ack()
        if not is_owner(body, client):
            return
        _, run_id, key, verb = action["action_id"].split(":", 3)
        ok, note = hk_apply(run_id, key, "reject")
        client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"],
                                text=f"{'🗑️ skipped' if ok else '⚠️'} — {note}")
        if ok and mode == "live":
            update_card(client, body["channel"]["id"], body["message"]["ts"], f"hkact_{key}",
                        f"🗑️ skipped — {note}", blocks=body["message"]["blocks"])

    print(f"relay_bot listening (apply mode = {mode}) …")
    SocketModeHandler(app, secret("relay_slack_app_token")).start()


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--post":
        do_post(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "--post-docsync":
        do_post_docsync(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "--post-findings":
        do_post_findings(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "--post-usergroups":
        do_post_usergroups(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "--post-housekeeping":
        do_post_housekeeping(sys.argv[2])
    elif len(sys.argv) == 1 or sys.argv[1] == "--listen":
        run_listener()
    else:
        print(__doc__); sys.exit(1)


if __name__ == "__main__":
    main()

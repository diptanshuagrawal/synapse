#!/usr/bin/env python3
"""doc_sync_apply.py — headless applier for Relay drift-finding Approve/Reject.

The Relay bot (relay_bot.py) calls this when the owner clicks a drift-finding button in
#doc-sweep. It is NETWORK-capable (unlike the chat/MCP path): it posts the inline Confluence
comment via the Confluence Cloud REST API using the Atlassian API token, then records the
result in doc_sync.db. This is the doc-sweep equivalent of bin/ticketize_apply.py.

  --finding-file <path> --key <finding_key> --decision approve|reject [--run-id <id>]

approve → post inline comment on the finding's page (footer-comment fallback if the anchor
          can't be matched) + record resolution_status=open.
reject  → record resolution_status=rejected (no Confluence write).

Auth: ~/.secrets/atlassian_email + ~/.secrets/atlassian_token (Basic). Host + cc from config.
"""
import os, re, sys, json, html, base64, argparse, datetime, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEC = os.path.expanduser("~/.secrets")
DOCSYNC_CFG = os.path.join(ROOT, "work-context/config/doc_sync.yaml")
SOURCES_CFG = os.path.join(ROOT, "work-context/config/sources.yaml")
STATE_PY = os.path.join(ROOT, "work-context/derive/doc_sync_state.py")
PEOPLE = os.path.join(ROOT, "work-context/config/people.yaml")


def _yaml(path):
    import yaml
    return yaml.safe_load(open(path)) or {}


def auth():
    email = open(os.path.join(SEC, "atlassian_email")).read().strip()
    token = open(os.path.join(SEC, "atlassian_token")).read().strip()
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def host():
    # Config-driven (work-context/config/sources.yaml is the source of truth);
    # the fallback is a sanitized placeholder — no real org host in this tracked,
    # public-mirrored file. Mirrors derive/sources_config.atlassian_host()'s default.
    try:
        return _yaml(SOURCES_CFG).get("atlassian", {}).get("host", "your-org.atlassian.net")
    except Exception:
        return "your-org.atlassian.net"


def api(method, path, a, body=None):
    url = f"https://{host()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": a, "Content-Type": "application/json",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or "{}")


def owner_name(canonical):
    try:
        for p in _yaml(PEOPLE).get("people", []):
            if p.get("canonical") == canonical:
                return p.get("name") or canonical
    except Exception:
        pass
    return canonical or ""


def owner_display():
    """Owner's display name, resolved from config (sources.yaml org.owner_email →
    matching people.yaml entry) — no hardcoded identity in this public-mirrored file."""
    try:
        email = (_yaml(SOURCES_CFG).get("org", {}) or {}).get("owner_email")
        if email:
            for p in _yaml(PEOPLE).get("people", []):
                if p.get("email") == email:
                    return p.get("name") or p.get("canonical") or ""
    except Exception:
        pass
    return ""


def clean_anchor(anchor):
    """Pick the longest plain-text line from a (possibly markdown) anchor."""
    lines = [re.sub(r"[#*`>]", "", ln).strip() for ln in (anchor or "").splitlines()]
    lines = [ln for ln in lines if ln]
    return max(lines, key=len) if lines else ""


def page_plaintext(page_id, a):
    try:
        p = api("GET", f"/wiki/api/v2/pages/{page_id}?body-format=view", a)
        raw = (p.get("body", {}).get("view", {}) or {}).get("value", "")
        return html.unescape(re.sub(r"<[^>]+>", " ", raw))
    except Exception:
        return ""


def comment_value(f, cc_name):
    owner = owner_name(f.get("owner_canonical"))
    edit = f.get("suggested_edit") or ""
    return (f"<p><strong>@{html.escape(owner)}</strong> — {html.escape(f.get('finding_title',''))}</p>"
            f"<p>{html.escape(edit)}</p>"
            f"<p><em>Reply to discuss, Resolve when handled.</em> cc @{html.escape(cc_name)}</p>")


def post_comment(f, a, cc_name):
    """Return (comment_id, kind). Try inline; fall back to footer."""
    body = {"representation": "storage", "value": comment_value(f, cc_name)}
    phrase = clean_anchor(f.get("anchor"))
    if phrase:
        text = page_plaintext(f["page_id"], a)
        count = text.count(phrase)
        if count >= 1:
            try:
                r = api("POST", "/wiki/api/v2/inline-comments", a, {
                    "pageId": str(f["page_id"]), "body": body,
                    "inlineCommentProperties": {
                        "textSelection": phrase,
                        "textSelectionMatchCount": count,
                        "textSelectionMatchIndex": 0,
                    }})
                return str(r.get("id")), "inline"
            except urllib.error.HTTPError as e:
                sys.stderr.write(f"inline failed ({e.code}): {e.read().decode()[:200]} — falling back to footer\n")
    r = api("POST", "/wiki/api/v2/footer-comments", a, {"pageId": str(f["page_id"]), "body": body})
    return str(r.get("id")), "footer"


def record(f, comment_id, comment_url, status, run_id):
    import subprocess, tempfile
    row = {
        "comment_id": comment_id, "finding_key": f.get("finding_key"),
        "page_id": str(f["page_id"]), "page_title": f.get("page_title"),
        "page_url": f.get("page_url"), "comment_url": comment_url,
        "owner_account": f.get("owner_account"), "severity": f.get("severity"),
        "check_type": f.get("check_type"), "finding_title": f.get("finding_title"),
        "anchor": f.get("anchor"),
        "created_ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "resolution_status": status,
        "last_checked_ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "sweep_run_id": run_id or "",
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump({"comments": [row]}, fh)
    subprocess.run([sys.executable, STATE_PY, "record", "--file", path], check=True)
    os.unlink(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--finding-file", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--decision", required=True, choices=["approve", "reject"])
    ap.add_argument("--run-id")
    ap.add_argument("--dry", action="store_true", help="resolve anchor + choose inline/footer, but do NOT post or record")
    args = ap.parse_args()

    data = json.load(open(args.finding_file))
    findings = data.get("findings", data) if isinstance(data, dict) else data
    f = next((x for x in findings if x.get("finding_key") == args.key), None)
    if f is None:
        print(json.dumps({"ok": False, "error": f"finding {args.key} not found"})); sys.exit(2)
    if f.get("already_open"):
        print(json.dumps({"ok": False, "error": "finding is an already-open dupe — not actionable"})); sys.exit(2)

    cfg = _yaml(DOCSYNC_CFG)
    run_id = args.run_id or data.get("run_id") if isinstance(data, dict) else args.run_id
    cc_acc = (cfg.get("slack", {}) or {}).get("cc_account_id")
    cc_name = (owner_display() if cc_acc else "") or "owner"

    if args.decision == "reject":
        record(f, f"reject:{args.key}", "", "rejected", run_id)
        print(json.dumps({"ok": True, "decision": "reject", "page_id": f["page_id"],
                          "finding": f.get("finding_title")}))
        return

    a = auth()
    if args.dry:
        phrase = clean_anchor(f.get("anchor"))
        count = page_plaintext(f["page_id"], a).count(phrase) if phrase else 0
        print(json.dumps({"ok": True, "dry": True, "page_id": f["page_id"],
                          "anchor_phrase": phrase, "anchor_matches": count,
                          "would_post": "inline" if count >= 1 else "footer",
                          "finding": f.get("finding_title")}))
        return
    cid, kind = post_comment(f, a, cc_name)
    comment_url = f"{f.get('page_url','')}?focusedCommentId={cid}"
    record(f, cid, comment_url, "open", run_id)
    print(json.dumps({"ok": True, "decision": "approve", "kind": kind, "comment_id": cid,
                      "comment_url": comment_url, "page_id": f["page_id"],
                      "finding": f.get("finding_title")}))


if __name__ == "__main__":
    main()

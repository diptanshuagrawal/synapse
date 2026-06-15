#!/usr/bin/env python3
"""ticketize_apply.py — DETERMINISTIC apply of one ticketize decision (no LLM).

By the time a candidate is approved, every field was pre-filled at DETECT (type, assignee,
epic, links_cmr). So applying is mechanical: resolve assignee accountId + active sprint,
create the Jira issue via REST, link the CMR, commit state, write the key back to the md.
Called by relay_bot (button click) or by hand. Jira REST auth reuses ~/.secrets.

Usage:
  python3 bin/ticketize_apply.py --date YYYY-MM-DD --fingerprint <fp> --decision approve|reject [--dry-run]

--dry-run resolves everything and prints the would-be payload but does NOT write to Jira/state.
Idempotent: a fingerprint already 'created' is skipped (prints existing key).
"""
import os, sys, re, json, base64, subprocess, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "work-context/config/ticketize.yaml")
PEOPLE = os.path.join(ROOT, "work-context/config/people.yaml")
SEC = os.path.expanduser("~/.secrets")


def arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def load_yaml(p):
    import yaml
    return yaml.safe_load(open(p))


def md_path(date):
    return os.path.join(ROOT, f"management/standup/{date}/ticket-candidates.md")


HEAD = re.compile(r"^##\s+([CG]\d+)\s+·\s+(.*?)\s*(?:—.*)?$")
FIELD = re.compile(r"^-\s+([a-z_]+):\s*(.*?)\s*(?:#.*)?$")


def find_candidate(date, fp):
    cur = None
    for line in open(md_path(date)):
        h = HEAD.match(line.rstrip())
        if h:
            cur = {"label": h.group(1)}
        elif cur:
            f = FIELD.match(line.rstrip())
            if f:
                cur[f.group(1)] = f.group(2).strip()
        if cur and cur.get("fingerprint") == fp:
            # keep reading until block ends? fields already captured up to here; continue to gather all
            pass
    # simpler: re-parse fully then pick
    blocks, cur = [], None
    for line in open(md_path(date)):
        h = HEAD.match(line.rstrip())
        if h:
            cur = {"label": h.group(1)}; blocks.append(cur)
        elif cur:
            f = FIELD.match(line.rstrip())
            if f:
                cur[f.group(1)] = f.group(2).strip()
    return next((b for b in blocks if b.get("fingerprint") == fp), None)


def accountid_for(canonical):
    d = load_yaml(PEOPLE); ppl = d.get("people", d)
    for p in (ppl if isinstance(ppl, list) else ppl.values()):
        if isinstance(p, dict) and p.get("canonical") == canonical:
            return p.get("jira_id")
    return None


def jira_auth():
    email = open(os.path.join(SEC, "atlassian_email")).read().strip()
    token = open(os.path.join(SEC, "atlassian_token")).read().strip()
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def jira(method, base, path, auth, body=None):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": auth, "Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"jira {method} {path} -> {e.code}: {e.read().decode()[:300]}")


def active_sprint_id(base, auth, project, sprint_field):
    jql = f"project = {project} AND sprint in openSprints()"
    r = jira("POST", base, "/rest/api/3/search/jql", auth,
             {"jql": jql, "maxResults": 1, "fields": [sprint_field]})
    for iss in r.get("issues", []):
        for sp in (iss["fields"].get(sprint_field) or []):
            if isinstance(sp, dict) and sp.get("state") == "active":
                return sp["id"]
    return None


def adf(text):
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def commit_state(date, fp, decision, key=None):
    rec = [{"fingerprint": fp, "decision": decision, "jira_key": key}]
    subprocess.run([sys.executable, os.path.join(ROOT, "bin/ticketize_state.py"), "commit", "--date", date],
                   input=json.dumps(rec), text=True, check=True)


def write_back(date, fp, decision, key=None):
    path = md_path(date); lines = open(path).read().splitlines()
    out, in_block = [], False
    for line in lines:
        if HEAD.match(line):
            in_block = False
        if re.match(r"^-\s+fingerprint:\s*" + re.escape(fp), line):
            in_block = True
        if in_block and re.match(r"^-\s+decision:", line):
            line = f"- decision: {decision}" + (f"  # {key}" if key else "")
            if key:
                out.append(line); out.append(f"- jira_key: {key}"); in_block = False; continue
        out.append(line)
    open(path, "w").write("\n".join(out) + "\n")


def main():
    date, fp, decision = arg("--date"), arg("--fingerprint"), arg("--decision")
    dry = "--dry-run" in sys.argv
    if not (date and fp and decision):
        print(__doc__); sys.exit(1)
    cfg = load_yaml(CFG); j = cfg["jira"]
    c = find_candidate(date, fp)
    if not c:
        raise SystemExit(f"candidate fp={fp} not found in {md_path(date)}")

    # idempotency
    chk = subprocess.run([sys.executable, os.path.join(ROOT, "bin/ticketize_state.py"), "annotate", "--date", date],
                         input=json.dumps([{"fingerprint": fp, "person": c.get("assignee"),
                                            "summary": c.get("summary"), "link": c.get("evidence")}]),
                         text=True, capture_output=True)
    prior = json.loads(chk.stdout or "[{}]")[0].get("prior_status", "new") if chk.stdout else "new"
    if prior == "created":
        pk = json.loads(chk.stdout)[0].get("prior_jira_key")
        print(f"already created: {pk}"); return

    if decision == "reject":
        if not dry:
            commit_state(date, fp, "reject"); write_back(date, fp, "rejected")
        print(f"rejected {c['label']} ({fp})"); return

    # approve → build payload
    epic = (c.get("epic") or "").split()[0] if c.get("epic") else j["fallback_epic"]
    if not re.match(r"^[A-Z]+-\d+$", epic):
        epic = j["fallback_epic"]
    assignee = accountid_for(c.get("assignee", ""))
    itype = c.get("type", "Task")
    fields = {
        "project": {"key": j["project"]},
        "issuetype": {"name": itype},
        "summary": c.get("summary", c["label"]),
        "parent": {"key": epic},
        "description": adf((c.get("why") or c.get("summary") or "") +
                           f"\n\nAuto-applied by /ticketize on {date} (decision via Slack). "
                           f"Parent = {epic} (reattach + add story points at planning)."),
    }
    if assignee:
        fields["assignee"] = {"accountId": assignee}
    if itype == "Bug":
        fields[j["fields"]["environment"]] = [{"value": j["environment_default"]}]

    if dry:
        print("DRY-RUN payload:"); print(json.dumps({"fields": fields,
              "epic": epic, "assignee_canonical": c.get("assignee"), "links_cmr": c.get("links_cmr")}, indent=2))
        return

    base, auth = j["base_url"], jira_auth()
    sid = active_sprint_id(base, auth, j["project"], j["fields"]["sprint"])
    if sid:
        fields[j["fields"]["sprint"]] = sid
    created = jira("POST", base, "/rest/api/3/issue", auth, {"fields": fields})
    key = created["key"]
    # link CMR(s)
    for cmr in re.findall(r"[A-Z]+-\d+", c.get("links_cmr", "")):
        jira("POST", base, "/rest/api/3/issueLink", auth,
             {"type": {"name": cfg.get("link", {}).get("cmr_link_type", "Associated")},
              "inwardIssue": {"key": key}, "outwardIssue": {"key": cmr}})
    commit_state(date, fp, "approve", key)
    write_back(date, fp, "created", key)
    print(f"created {key} ({c['label']}) sprint={sid or 'none'} epic={epic}")


if __name__ == "__main__":
    import urllib.parse  # noqa: E402
    main()

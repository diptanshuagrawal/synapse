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


HEAD = re.compile(r"^##\s+([A-Z]\d+)\s+·\s+(.*?)\s*(?:—.*)?$")
FIELD = re.compile(r"^-\s+([a-z_]+):\s*(.*?)\s*(?:#.*)?$")


def find_candidate(date, fp):
    blocks, cur, last = [], None, None
    block_field = None      # field currently absorbing a `|` block scalar
    block_lines = None      # raw (indent-stripped) lines of that block, newlines preserved
    def _flush_block():
        nonlocal block_field, block_lines
        if block_field is not None:
            # strip the common leading indent so the stored markdown is clean
            body = [l for l in block_lines]
            indents = [len(l) - len(l.lstrip(" ")) for l in body if l.strip()]
            cut = min(indents) if indents else 0
            cur[block_field] = "\n".join(l[cut:] if len(l) >= cut else l for l in body).strip("\n")
            block_field = block_lines = None
    for raw in open(md_path(date)):
        line = raw.rstrip()
        # inside a `|` block: a non-blank line at column 0 ends it; everything else is body
        if block_field is not None:
            if line.strip() and not raw[:1].isspace():
                _flush_block()
            else:
                block_lines.append(line); continue
        h = HEAD.match(line)
        if h:
            cur = {"label": h.group(1)}; blocks.append(cur); last = None
            continue
        if cur is None:
            continue
        f = FIELD.match(line)
        if f:
            if f.group(2).strip() == "|":     # YAML-style block scalar: capture verbatim
                block_field, block_lines, last = f.group(1), [], None
            else:
                cur[f.group(1)] = f.group(2).strip(); last = f.group(1)
        elif last and raw.startswith(("  ", "\t")) and line.strip():
            cur[last] += " " + line.strip()   # wrapped multi-line value (e.g. `why:`)
        else:
            last = None
    _flush_block()
    return next((b for b in blocks if b.get("fingerprint") == fp), None)


def accountid_for(token):
    """Resolve a free-text person token to a Jira accountId, leniently. Matches (case-
    insensitive) against canonical, name, slack_id, github, email, the email local-part,
    and any aliases — so 'sumit', 'Sumit Kumar', 'sumit.k', or the slack/github handle all
    resolve, not just the exact canonical. Returns the jira_id or None."""
    t = (token or "").strip().lower()
    if not t:
        return None
    d = load_yaml(PEOPLE); ppl = d.get("people", d)
    people = [p for p in (ppl if isinstance(ppl, list) else ppl.values()) if isinstance(p, dict)]
    # 1) exact match against any full identifier
    for p in people:
        email = (p.get("email") or "")
        cands = {str(p.get("canonical") or ""), str(p.get("name") or ""),
                 str(p.get("slack_id") or ""), str(p.get("github") or ""),
                 email, email.split("@")[0]}
        al = p.get("aliases") or []
        cands.update(str(a) for a in (al if isinstance(al, list) else [al]))
        if t in {c.strip().lower() for c in cands if c}:
            return p.get("jira_id")
    # 2) first-name / canonical-prefix match — ONLY if it's unambiguous (one person)
    def first_tokens(p):
        return {str(p.get("name") or "").strip().lower().split(" ")[0],
                str(p.get("canonical") or "").strip().lower().split("-")[0]} - {""}
    hits = [p for p in people if t in first_tokens(p)]
    return hits[0].get("jira_id") if len(hits) == 1 else None


def resolve_assignee(override, cand):
    """Resolve the assignee to a Jira accountId. The approver override (from the Slack
    modal) wins over the candidate default. Returns (accountId_or_None, warning_or_None).

    FAIL LOUD: if an override was supplied but cannot be resolved, do NOT silently fall
    back to the candidate default (that's how a delegated ticket lands back on the owner).
    Leave it unassigned and return a warning the caller surfaces in the Slack note."""
    ov = (override or "").strip()
    if ov:
        acct = accountid_for(ov)
        if acct:
            return acct, None
        if ":" in ov or len(ov) >= 16:        # already looks like a raw accountId
            return ov, None
        return None, f"couldn't resolve assignee '{ov}' — left UNASSIGNED, set it manually"
    cd = (cand or "").strip()
    if cd:
        acct = accountid_for(cd)
        if acct:
            return acct, None
        if ":" in cd or len(cd) >= 16:
            return cd, None
        return None, f"candidate assignee '{cd}' didn't resolve — left UNASSIGNED"
    return None, None


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


def search_epic(base, auth, project, query):
    """Find an Epic by free-text keywords (e.g. 'atm charges'). Returns (key, summary) or None."""
    q = query.replace('"', "").strip()
    jql = f'project = {project} AND issuetype = Epic AND statusCategory != Done AND summary ~ "{q}*" ORDER BY updated DESC'
    r = jira("POST", base, "/rest/api/3/search/jql", auth, {"jql": jql, "maxResults": 5, "fields": ["summary"]})
    issues = r.get("issues", [])
    if not issues:
        return None
    return issues[0]["key"], issues[0]["fields"]["summary"]


def epic_title(base, auth, key):
    try:
        r = jira("GET", base, f"/rest/api/3/issue/{key}?fields=summary", auth)
        return r.get("fields", {}).get("summary", "")
    except SystemExit:
        return ""


_LINK_MD = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BARE_URL = re.compile(r"(https?://[^\s)]+)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _inline(text):
    """Render a single line of markdown-subset into ADF inline nodes:
    [label](url), bare URLs → link marks; **bold** → strong. Returns a list of nodes."""
    nodes, i = [], 0
    # tokenize on markdown links first, then handle bare urls / bold inside the gaps
    for m in _LINK_MD.finditer(text):
        if m.start() > i:
            nodes += _inline_plain(text[i:m.start()])
        nodes.append({"type": "text", "text": m.group(1),
                      "marks": [{"type": "link", "attrs": {"href": m.group(2)}}]})
        i = m.end()
    if i < len(text):
        nodes += _inline_plain(text[i:])
    return nodes or [{"type": "text", "text": text}]


def _inline_plain(text):
    nodes, i = [], 0
    for m in _BARE_URL.finditer(text):
        if m.start() > i:
            nodes += _bold(text[i:m.start()])
        nodes.append({"type": "text", "text": m.group(1),
                      "marks": [{"type": "link", "attrs": {"href": m.group(1)}}]})
        i = m.end()
    if i < len(text):
        nodes += _bold(text[i:])
    return nodes


def _bold(text):
    nodes, i = [], 0
    for m in _BOLD.finditer(text):
        if m.start() > i:
            nodes.append({"type": "text", "text": text[i:m.start()]})
        nodes.append({"type": "text", "text": m.group(1), "marks": [{"type": "strong"}]})
        i = m.end()
    if i < len(text):
        nodes.append({"type": "text", "text": text[i:]})
    return [n for n in nodes if n["text"]]


def md_to_adf(text):
    """Render a markdown subset (headings ##/###, bullet & checkbox lists, --- rules,
    blank-line paragraphs, links, **bold**) into Atlassian Document Format."""
    content, para, bullets = [], [], []
    def flush_para():
        nonlocal para
        if para:
            content.append({"type": "paragraph", "content": _inline(" ".join(para))})
            para = []
    def flush_bullets():
        nonlocal bullets
        if bullets:
            content.append({"type": "bulletList", "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": _inline(b)}]}
                for b in bullets]})
            bullets = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            flush_para(); flush_bullets(); continue
        if s == "---":
            flush_para(); flush_bullets(); content.append({"type": "rule"}); continue
        hm = re.match(r"^(#{2,4})\s+(.*)$", s)
        if hm:
            flush_para(); flush_bullets()
            content.append({"type": "heading", "attrs": {"level": len(hm.group(1))},
                            "content": _inline(hm.group(2))})
            continue
        bm = re.match(r"^[-*]\s+(.*)$", s)
        if bm:
            flush_para()
            item = bm.group(1)
            item = re.sub(r"^\[ \]\s*", "☐ ", item)
            item = re.sub(r"^\[[xX]\]\s*", "☑ ", item)
            bullets.append(item); continue
        flush_bullets(); para.append(s)
    flush_para(); flush_bullets()
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": text or " "}]}]
    return {"type": "doc", "version": 1, "content": content}


def adf(text):
    return md_to_adf(text)


def commit_state(date, fp, decision, key=None):
    rec = [{"fingerprint": fp, "decision": decision, "jira_key": key}]
    subprocess.run([sys.executable, os.path.join(ROOT, "bin/ticketize_state.py"), "commit", "--date", date],
                   input=json.dumps(rec), text=True, check=True)


def write_back(date, fp, decision, key=None):
    """Rewrite the decision (+ jira_key) of the block whose fingerprint == fp. Block-aware:
    decision: sits ABOVE fingerprint: in a block, so locate the block first, then edit it."""
    path = md_path(date); lines = open(path).read().splitlines()
    heads = [i for i, l in enumerate(lines) if HEAD.match(l)] + [len(lines)]
    for a, b in zip(heads, heads[1:]):
        block = lines[a:b]
        if not any(re.match(r"^-\s+fingerprint:\s*" + re.escape(fp), l) for l in block):
            continue
        for k, l in enumerate(block):
            if re.match(r"^-\s+decision:", l):
                block[k] = f"- decision: {decision}" + (f"  # {key}" if key else "")
                if key and not any(re.match(r"^-\s+jira_key:", x) for x in block):
                    block.insert(k + 1, f"- jira_key: {key}")
                break
        lines[a:b] = block
        break
    open(path, "w").write("\n".join(lines) + "\n")


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

    # approve → resolve epic. precedence: --epic-input (key or keyword-search) > candidate epic > fallback
    def is_key(s):
        return bool(re.match(r"^[A-Z]+-\d+$", (s or "").strip()))
    ei = arg("--epic-input")
    cand_epic = (c.get("epic") or "").split()[0]
    epic = ei.strip() if is_key(ei) else (cand_epic if is_key(cand_epic) else j["fallback_epic"])
    needs_search = bool(ei) and not is_key(ei)
    ai = (arg("--assignee-input") or "").strip()
    assignee, asg_warn = resolve_assignee(ai, c.get("assignee", ""))
    itype = c.get("type", "Task")
    # placement → sprint vs backlog. Requests (future-ask / owner-ask) land in the backlog.
    placement = (c.get("placement") or "").strip().lower()
    source = (c.get("source") or "").strip().lower()
    to_backlog = placement == "backlog" or source in ("future-ask", "owner-ask")

    if dry:
        print("DRY-RUN:", json.dumps({"epic": epic, "epic_search_query": ei if needs_search else None,
              "type": itype, "assignee_input": ai or None, "assignee_canonical": c.get("assignee"),
              "assignee_resolved": assignee, "assignee_warning": asg_warn,
              "placement": "backlog" if to_backlog else "active-sprint", "summary": c.get("summary"),
              "links_cmr": c.get("links_cmr")}, indent=2))
        return

    base, auth = j["base_url"], jira_auth()
    if needs_search:
        found = search_epic(base, auth, j["project"], ei)
        if found:
            epic = found[0]
            print(f"epic search '{ei}' -> {found[0]} ({found[1]})", file=sys.stderr)
        else:
            print(f"epic search '{ei}' -> no match; using fallback {j['fallback_epic']}", file=sys.stderr)
            epic = j["fallback_epic"]
    fields = {
        "project": {"key": j["project"]},
        "issuetype": {"name": itype},
        "summary": c.get("summary", c["label"]),
        "parent": {"key": epic},
        # Prefer the maker's dev-facing `desc:` block (full requirements + links). Fall
        # back to the owner-facing `why` only if no dev spec was authored. The provenance
        # footer is appended either way (rewriting {epic} so it points at the real parent).
        "description": md_to_adf(
            (c.get("desc") or c.get("why") or c.get("summary") or "")
            + f"\n\n---\n_Auto-applied by /ticketize on {date} (decision via Slack). "
              f"Parent = {epic} — reattach to the right epic + add story points at planning._"),
    }
    if assignee:
        fields["assignee"] = {"accountId": assignee}
    if itype == "Bug":
        fields[j["fields"]["environment"]] = [{"value": j["environment_default"]}]
    sid = None
    if not to_backlog:
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
    note = f"created {key} ({c['label']}) sprint={sid or ('backlog' if to_backlog else 'none')} epic={epic}"
    if asg_warn:
        note += f" — ⚠️ {asg_warn}"
    print(note)


if __name__ == "__main__":
    import urllib.parse  # noqa: E402
    main()

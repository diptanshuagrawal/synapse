#!/usr/bin/env python3
"""
restore_people_ids.py — fill missing jira_id + github fields on people.yaml
entries restored after an accidental git checkout.

For each entry missing jira_id and/or github:
  - Jira:   GET /rest/api/3/user/search?query=<email> → accountId.
            Auth: ATLASSIAN_EMAIL + ATLASSIAN_TOKEN (or ~/.secrets/atlassian_token).
  - GitHub: GET /search/users?q=<email>+in:email → handle (only works for
            users with public-email visibility).
            Fallback: scan events.db for commit author git_name match by
            canonical (best-effort heuristic).
            Auth: ~/.secrets/github_pat.

Dry-run by default. --apply does in-place line-based yaml edit (inserts
new fields above the `canonical:` line of each matched entry, preserving
comments + formatting).

Usage:
    python -m derive.restore_people_ids                    # dry, all missing
    python -m derive.restore_people_ids --apply
    python -m derive.restore_people_ids --canonicals vs example-dev2  # subset
    python -m derive.restore_people_ids --skip-jira              # only github
    python -m derive.restore_people_ids --skip-github            # only jira
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH  # noqa: E402
from derive.sources_config import (  # noqa: E402
    atlassian_host,
    github_org,
    github_handle_prefixes,
    owner_email,
)

PEOPLE_YAML = _REPO_ROOT / "config" / "people.yaml"
ATLASSIAN_DOMAIN = os.environ.get("JIRA_DOMAIN", atlassian_host())
GITHUB_ORG = os.environ.get("GITHUB_ORG", github_org())


def _atlassian_creds() -> tuple[str, str]:
    email = os.environ.get("ATLASSIAN_EMAIL")
    token = os.environ.get("ATLASSIAN_TOKEN")
    if not email:
        secrets_email = Path.home() / ".secrets" / "atlassian_email"
        if secrets_email.exists():
            email = secrets_email.read_text().strip()
        else:
            email = owner_email()
    if not token:
        secrets_token = Path.home() / ".secrets" / "atlassian_token"
        if secrets_token.exists():
            token = secrets_token.read_text().strip()
    if not token:
        raise RuntimeError("ATLASSIAN_TOKEN not found in env or ~/.secrets/atlassian_token")
    return email, token


def _github_token() -> str:
    pat = os.environ.get("GITHUB_PAT") or os.environ.get("GITHUB_TOKEN")
    if not pat:
        for fname in ("github_pat", "github_pa"):
            p = Path.home() / ".secrets" / fname
            if p.exists():
                pat = p.read_text().strip()
                break
    if not pat:
        raise RuntimeError("GITHUB_PAT not found in env or ~/.secrets/")
    return pat


def _jira_lookup_account_id(email: str, auth_email: str, token: str) -> str | None:
    """Atlassian user-search by email. Returns accountId or None."""
    qs = urllib.parse.urlencode({"query": email})
    url = f"https://{ATLASSIAN_DOMAIN}/rest/api/3/user/search?{qs}"
    creds = base64.b64encode(f"{auth_email}:{token}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {creds}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        return None
    if not data:
        return None
    # Filter to atlassian-user accounts; pick exact email match.
    for u in data:
        if u.get("emailAddress", "").lower() == email.lower():
            return u.get("accountId")
    # No email match — pick first if it's an atlassian-user
    for u in data:
        if u.get("accountType") == "atlassian":
            return u.get("accountId")
    return None


def _github_org_members(org: str, pat: str) -> list[dict]:
    """Return [{login, name?, email?}, ...] for every member of the org.

    Two-step:
      1. /orgs/<org>/members?per_page=100 (paginated) → list of {login}
      2. /users/<login> per member → enriched with name, email (if public)
    """
    members: list[dict] = []
    page = 1
    while True:
        url = f"https://api.github.com/orgs/{org}/members?per_page=100&page={page}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            print(f"  [org-members] page={page} err: {e}", file=sys.stderr)
            break
        if not data:
            break
        for m in data:
            login = m.get("login")
            if login:
                members.append({"login": login})
        if len(data) < 100:
            break
        page += 1
    # Enrich each member's name + email via /users/<login>
    enriched: list[dict] = []
    for i, m in enumerate(members, 1):
        login = m["login"]
        url = f"https://api.github.com/users/{login}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                u = json.loads(r.read().decode())
                enriched.append({
                    "login": login,
                    "name": u.get("name"),
                    "email": u.get("email"),
                })
        except (urllib.error.HTTPError, urllib.error.URLError):
            enriched.append({"login": login, "name": None, "email": None})
        if i % 25 == 0:
            print(f"  [org-members] enriched {i}/{len(members)}", flush=True)
    return enriched


def _norm_name(s: str) -> str:
    return "".join(ch.lower() for ch in (s or "") if ch.isalnum())


def _github_match_org(canonical: str, real_name: str | None, email: str | None,
                      org_members: list[dict]) -> str | None:
    """Strict-match canonical + real_name against org member display names.

    Strategy:
      1. Exact email match → strongest signal (rare in practice — GitHub
         user.email is usually private).
      2. Full normalised real_name == member.name (case/punct-insensitive).
      3. Full normalised real_name == login-suffix after an org-prefix.
      4. Both real_name TOKENS (first AND last) appear in member.name OR login.
      Returns the best match's login; rejects if any heuristic only
      partially matched (avoid first-name collisions like example-dev2→example-devexample).
    """
    if not real_name:
        return None
    name_norm = _norm_name(real_name)
    name_tokens = [_norm_name(t) for t in real_name.split() if len(t) >= 3]
    if len(name_tokens) < 2:
        return None  # single-token canonicals too ambiguous for safe match

    for m in org_members:
        login = m["login"]
        m_name = m.get("name") or ""
        m_email = (m.get("email") or "").lower()
        m_norm_name = _norm_name(m_name)
        m_norm_login = _norm_name(login)

        # 1. Email exact
        if email and m_email == email.lower():
            return login

        if not name_norm:
            continue

        # 2. Display name exact (normalised)
        if m_norm_name and m_norm_name == name_norm:
            return login

        # 3. Login == <org-prefix><fullname-normalised>
        for prefix in github_handle_prefixes():
            if m_norm_login == f"{prefix}{name_norm}":
                return login

        # 4. ALL real-name tokens present in display name OR login
        haystack = m_norm_name + " " + m_norm_login
        if all(tok in haystack for tok in name_tokens):
            return login

    return None


def _github_search_by_email(email: str, pat: str) -> str | None:
    """GitHub /search/users?q=<email>+in:email. Only returns handle if user
    has email publicly visible. Most users don't."""
    qs = urllib.parse.urlencode({"q": f"{email} in:email"})
    url = f"https://api.github.com/search/users?{qs}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None
    items = data.get("items", [])
    if not items:
        return None
    return items[0].get("login")


def _github_db_fallback(canonical: str, real_name: str | None) -> str | None:
    """Scan events.db for a github actor whose canonical resolves to this name.
    Returns a github handle or None.

    Heuristic: commits often have an actor (github login) + a co-located
    git_name in the raw payload. Without raw_payload parsing, we do the next
    best thing: match by actor LIKE '%canonical%' for github-source rows.
    Catches `<org-prefix>-<canonical>` patterns common at this org.
    """
    if not canonical:
        return None
    if not DB_PATH.exists():
        return None
    prefixes = github_handle_prefixes()
    primary = prefixes[0] if prefixes else ""
    try:
        conn = sqlite3.connect(DB_PATH)
        # Org convention: github handles are `<org-prefix>-<name>` or `<name>`
        candidates = [
            f"{primary}-{canonical}",
            canonical,
            canonical.replace("-", ""),
        ]
        # Real-name fragments (e.g. "Example Name" → "example", "name")
        if real_name:
            for part in real_name.lower().replace(".", "").split():
                if len(part) >= 4:
                    candidates.append(f"{primary}-{part}")
                    candidates.append(part)
        for cand in candidates:
            row = conn.execute(
                "SELECT actor FROM events WHERE source='github' AND actor=? LIMIT 1",
                (cand,),
            ).fetchone()
            if row:
                return cand
        conn.close()
    except sqlite3.Error:
        return None
    return None


def _load_people() -> tuple[list[dict], str]:
    """Returns (parsed people list, raw text). Raw text used for line-edits."""
    raw = PEOPLE_YAML.read_text()
    with PEOPLE_YAML.open() as f:
        cfg = yaml.safe_load(f)
    return cfg.get("people", []), raw


def _insert_fields_above_canonical(raw: str, canonical: str,
                                   new_lines: list[str]) -> tuple[str, bool]:
    """In-place insert of new yaml lines above the `canonical: <canonical>`
    line for a specific person. Returns (modified-raw, changed-bool).

    `new_lines` should be just the field text like ['    jira_id: "abc"',
    '    github: example-foo']. We don't add a trailing newline (handled below).
    """
    needle = f"    canonical: {canonical}"
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line == needle:
            for inj in reversed(new_lines):
                lines.insert(i, inj)
            return "\n".join(lines) + ("\n" if raw.endswith("\n") else ""), True
    return raw, False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonicals", nargs="*",
                    help="restrict to specific canonicals (default: all missing jira+github)")
    ap.add_argument("--skip-jira", action="store_true")
    ap.add_argument("--skip-github", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="write in-place yaml edits; default is dry-run")
    args = ap.parse_args()

    people, raw = _load_people()

    targets: list[dict] = []
    for p in people:
        cn = p.get("canonical")
        if not cn:
            continue
        if args.canonicals and cn not in args.canonicals:
            continue
        missing_jira = not p.get("jira_id") and not args.skip_jira
        missing_github = not p.get("github") and not args.skip_github
        if missing_jira or missing_github:
            targets.append(p)

    if not targets:
        print("nothing to do — every entry has both jira_id + github")
        return 0

    print(f"[targets] {len(targets)} entries missing at least one of jira_id/github\n",
          flush=True)

    # Lazy-init clients
    j_email = j_tok = None
    g_pat = None
    org_members: list[dict] = []
    if not args.skip_jira:
        try:
            j_email, j_tok = _atlassian_creds()
            print(f"[atlassian] ready as {j_email}", flush=True)
        except RuntimeError as e:
            print(f"[atlassian] DISABLED — {e}", file=sys.stderr)
    if not args.skip_github:
        try:
            g_pat = _github_token()
            print(f"[github] ready (PAT present)", flush=True)
            print(f"[github] fetching {GITHUB_ORG} org members for name-based matching...",
                  flush=True)
            org_members = _github_org_members(GITHUB_ORG, g_pat)
            print(f"[github] org members enriched: {len(org_members)}", flush=True)
        except RuntimeError as e:
            print(f"[github] DISABLED — {e}", file=sys.stderr)

    proposals: list[dict] = []
    for i, p in enumerate(targets, 1):
        cn = p.get("canonical")
        email = p.get("email") or ""
        prop: dict = {"canonical": cn, "email": email, "name": p.get("name")}
        if not p.get("jira_id") and j_email and j_tok and email:
            aid = _jira_lookup_account_id(email, j_email, j_tok)
            prop["new_jira_id"] = aid
        if not p.get("github") and g_pat and email:
            handle = _github_search_by_email(email, g_pat)
            source = "github-search-by-email"
            if not handle and org_members:
                handle = _github_match_org(cn, p.get("name"), email, org_members)
                source = "org-member-match" if handle else source
            if not handle:
                handle = _github_db_fallback(cn, p.get("name"))
                source = "db-heuristic" if handle else "none"
            prop["github_source"] = source
            prop["new_github"] = handle
        proposals.append(prop)
        if i % 5 == 0:
            print(f"  ... {i}/{len(targets)}", flush=True)
        time.sleep(0.2)  # gentle throttle

    # ── Report ──
    print(f"\n{'canonical':<22}  {'jira_id (new)':<46}  {'github (new)':<25}  source")
    print("-" * 110)
    apply_set: list[tuple[dict, list[str]]] = []
    for p in proposals:
        cn = p["canonical"][:22]
        nj = (p.get("new_jira_id") or "")[:46]
        ng = (p.get("new_github") or "")[:25]
        src = p.get("github_source", "")
        print(f"{cn:<22}  {nj:<46}  {ng:<25}  {src}")

        new_lines = []
        if p.get("new_jira_id"):
            new_lines.append(f'    jira_id: "{p["new_jira_id"]}"')
        if p.get("new_github"):
            new_lines.append(f'    github: {p["new_github"]}')
        if new_lines:
            apply_set.append((p, new_lines))

    print(f"\n[summary] {len(apply_set)}/{len(proposals)} entries have ≥1 new field")

    if not args.apply:
        print("\n[dry] re-run with --apply to write in-place yaml edits")
        return 0

    # ── Apply ──
    modified = raw
    n_changed = 0
    for p, new_lines in apply_set:
        modified, changed = _insert_fields_above_canonical(modified, p["canonical"], new_lines)
        if changed:
            n_changed += 1
        else:
            print(f"  WARN: canonical={p['canonical']} not found in raw yaml — skipped",
                  file=sys.stderr)

    PEOPLE_YAML.write_text(modified)
    print(f"\n[apply] inserted fields for {n_changed} entries in {PEOPLE_YAML}")
    print("[apply] verify diff with: git diff work-context/config/people.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Leak-gate scanner — shared by .githooks/pre-commit and pre-push.

Uses Python's `re` (which honours \\b); the previous grep -E gate silently
mishandled \\b on BSD/macOS and passed real leaks (\\bCBST\\b, \\b[UCS]0…, \\bcbs\\b).

Patterns = generic .githooks/leak-patterns.txt + optional gitignored
.publish-denylist.txt (real tokens, local-only; absent on public clones).
Lines carrying an ALLOW marker (intentional placeholders) are skipped.

Modes:
  staged                 scan staged (ACM) blob content + filenames  [pre-commit]
  range <new> [<base>]   scan every UNIQUE blob introduced by commits reachable
                         from <new> but not <base> (the push range). With no
                         <base> (new branch) scan all blobs of <new>.  [pre-push]

Exit 0 = clean, 1 = leak found (prints file:line + pattern).
"""
import os
import re
import subprocess
import sys

# Intentional placeholders — a line containing any of these is not a leak.
ALLOW_RE = re.compile(
    r"EXAMPLE|PLACEHOLDER|CHANGEME|your-org|yourorg|yourcompany|sample|noreply|"
    r"OWNER|ALICE|BOB|CAROL|DAN|EVE|FRANK|GRACE|HENRY|IVAN",
    re.I,
)


def _root():
    return subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()


def _config_values(root):
    """Scalar identity values configured in the REAL gitignored config/sources.yaml
    (email domain, host, project keys, repos, team names/title, channel ids,
    workspace, handles). Parsed with plain regex (no PyYAML — this hook runs under
    bare system python); values only, never keys or comment prose. Used by `audit`
    to flag any identity value the denylist does not yet cover, so a human curates
    a precise (word-boundaried) pattern. NOT auto-promoted to patterns: blanket
    blocking over-reaches badly (generic tokens like a 'slice' handle-prefix match
    English prose / JS .slice(); the MCP-server uuid lives in tracked settings.json).
    """
    fp = os.path.join(root, "work-context", "config", "sources.yaml")
    if not os.path.exists(fp):
        return []
    vals, seen = [], set()
    for raw in open(fp, encoding="utf-8", errors="replace"):
        line = raw.rstrip("\n")
        m = re.match(r"^\s*[\w.\-]+:\s*(.*)$", line)        # key: value
        if m:
            rhs = m.group(1)
        elif re.match(r"^\s*-\s+", line):                   # - list item
            rhs = re.sub(r"^\s*-\s+", "", line)
        else:
            continue
        quoted = re.findall(r'"([^"]+)"|\'([^\']+)\'', rhs)
        if quoted:
            found = [a or b for a, b in quoted]
        else:
            bare = rhs.split(" #", 1)[0].strip().strip("[]")  # drop inline comment / flow brackets
            found = [bare] if bare and not bare.startswith("#") else []
        for v in found:
            v = v.strip()
            if len(v) >= 4 and v not in seen:
                seen.add(v)
                vals.append(v)
    return vals


def _load_patterns(root):
    # Two tiers:
    #   HARD = real org tokens (.publish-denylist.txt). A match is ALWAYS a leak —
    #          an ALLOW marker on the line never excuses it (a real project key /
    #          employee name / workspace id has no business in a tracked file,
    #          even on a line that also says "placeholder").
    #   SOFT = generic format heuristics (.githooks/leak-patterns.txt: email/UUID/
    #          key/path shapes) that legitimately appear in doc examples; an ALLOW
    #          marker on the line suppresses these.
    # A pattern present in both files is treated as HARD (stricter wins).
    by_pat, order = {}, []
    for fp, hard in ((os.path.join(root, ".githooks/leak-patterns.txt"), False),
                     (os.path.join(root, ".publish-denylist.txt"), True)):
        if os.path.exists(fp):
            for line in open(fp, encoding="utf-8", errors="replace"):
                s = line.strip()
                if s and not s.startswith("#"):
                    if s not in by_pat:
                        order.append(s)
                    by_pat[s] = by_pat.get(s, False) or hard
    out = []
    for p in order:
        try:
            out.append((p, re.compile(p, re.I), by_pat[p]))
        except re.error:
            pass  # a malformed pattern shouldn't disable the whole gate
    return out


def _scan_lines(label, text, rx, content_hits):
    """Line-by-line content scan (shared by file blobs AND commit messages)."""
    for i, line in enumerate(text.split("\n"), 1):
        allowed = bool(ALLOW_RE.search(line))  # an intentional-placeholder line
        for p, r, hard in rx:
            if not r.search(line):
                continue
            if allowed and not hard:
                continue  # soft format hit on a placeholder line — not a leak
            # hard hits fire even on placeholder lines (real token = always a leak)
            content_hits.append((label, i, p, line.strip()[:100]))
            break


def _scan(path, text, rx, content_hits, name_hits):
    if path.endswith("leak-patterns.txt"):
        return  # the pattern file would self-match
    for p, r, _hard in rx:
        if r.search(path):
            name_hits.append((path, p))
            break
    _scan_lines(path, text, rx, content_hits)


def _report(content_hits, name_hits):
    if content_hits:
        print("leak-gate: BLOCKED — sensitive content:", file=sys.stderr)
        for path, i, p, txt in content_hits[:40]:
            print(f"   {path}:{i}: [{p}]  {txt}", file=sys.stderr)
    if name_hits:
        print("leak-gate: BLOCKED — sensitive filename:", file=sys.stderr)
        for path, p in name_hits[:20]:
            print(f"   {path}: [{p}]", file=sys.stderr)
    return 1 if (content_hits or name_hits) else 0


def _batch_blobs(shas):
    """git cat-file --batch over a list of blob shas -> {sha: bytes}."""
    out = {}
    if not shas:
        return out
    proc = subprocess.run(["git", "cat-file", "--batch"],
                          input=("\n".join(shas) + "\n").encode(), capture_output=True)
    data, pos = proc.stdout, 0
    while pos < len(data):
        nl = data.index(b"\n", pos)
        sha, _typ, size = data[pos:nl].decode("utf-8", "replace").split()
        start = nl + 1
        out[sha] = data[start:start + int(size)]
        pos = start + int(size) + 1
    return out


def cmd_staged(root, rx):
    names = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"], text=True).split("\n")
    content_hits, name_hits = [], []
    for f in names:
        if not f:
            continue
        blob = subprocess.run(["git", "show", f":{f}"], capture_output=True).stdout
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary
        _scan(f, text, rx, content_hits, name_hits)
    return _report(content_hits, name_hits)


def cmd_range(root, rx, new, base):
    revargs = ["git", "rev-list", "--objects", new]
    if base:
        revargs += ["--not", base]
    rev = subprocess.run(revargs, capture_output=True, text=True).stdout.splitlines()
    sha_path = {}
    for line in rev:
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            sha_path.setdefault(parts[0], parts[1])
    # keep only blobs
    check_in = "".join(line.split()[0] + "\n" for line in rev if line.strip())
    chk = subprocess.run(["git", "cat-file", "--batch-check"], input=check_in,
                         capture_output=True, text=True).stdout.splitlines()
    blobs = [p[0] for line in chk if (p := line.split()) and len(p) >= 2 and p[1] == "blob"]
    blobs = list(dict.fromkeys(blobs))
    content_hits, name_hits = [], []
    for sha, blob in _batch_blobs(blobs).items():
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue
        _scan(sha_path.get(sha, sha), text, rx, content_hits, name_hits)
    # Also scan the COMMIT MESSAGES in the range: a secret pasted into a commit
    # body (token, email, denylisted org term) lives in message metadata, not in
    # any file blob, so blob scanning alone can't catch it. (Note: this is still
    # PATTERN-based — an arbitrary personal name isn't caught here; human review
    # is the backstop for those.)
    logargs = ["git", "rev-list", new] + (["--not", base] if base else [])
    for csha in subprocess.run(logargs, capture_output=True, text=True).stdout.split():
        msg = subprocess.run(["git", "log", "-1", "--format=%B", csha],
                             capture_output=True, text=True).stdout
        _scan_lines(f"commit {csha[:10]} (message)", msg, rx, content_hits)
    return _report(content_hits, name_hits)


def cmd_audit(root, rx):
    """Drift check: list real config identity values NOT covered by any denylist
    pattern. These are the candidates that could slip into a tracked file undetected
    (how a newly-added team display name slipped past). Informational — prints a
    suggested entry and exits 0 so it can run in a routine without blocking; a human
    adds a precise pattern to .publish-denylist.txt.
    """
    uncovered = []
    for v in _config_values(root):
        if not any(r.search(v) for _p, r, _h in rx):
            uncovered.append(v)
    if uncovered:
        print("leak-gate audit: config identity NOT covered by the denylist "
              "(add a precise, word-boundaried pattern to .publish-denylist.txt):",
              file=sys.stderr)
        for v in uncovered:
            print(f"   uncovered: {v!r}   e.g.  \\b{re.escape(v)}\\b", file=sys.stderr)
    else:
        print("leak-gate audit: all config identity values are covered.", file=sys.stderr)
    return 0


def main():
    if len(sys.argv) < 2:
        print("usage: leak_scan.py staged | range <new> [<base>] | audit", file=sys.stderr)
        return 2
    root = _root()
    rx = _load_patterns(root)
    mode = sys.argv[1]
    if mode == "audit":
        return cmd_audit(root, rx)
    if not rx:
        return 0  # no patterns -> nothing to enforce
    if mode == "staged":
        return cmd_staged(root, rx)
    if mode == "range":
        new = sys.argv[2]
        base = sys.argv[3] if len(sys.argv) > 3 else None
        return cmd_range(root, rx, new, base)
    print(f"unknown mode {mode!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

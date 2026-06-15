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


def _load_patterns(root):
    pats, seen, out = [], set(), []
    for fp in (os.path.join(root, ".githooks/leak-patterns.txt"),
               os.path.join(root, ".publish-denylist.txt")):
        if os.path.exists(fp):
            for line in open(fp, encoding="utf-8", errors="replace"):
                s = line.strip()
                if s and not s.startswith("#"):
                    pats.append(s)
    for p in pats:
        if p in seen:
            continue
        seen.add(p)
        try:
            out.append((p, re.compile(p, re.I)))
        except re.error:
            pass  # a malformed pattern shouldn't disable the whole gate
    return out


def _scan(path, text, rx, content_hits, name_hits):
    if path.endswith("leak-patterns.txt"):
        return  # the pattern file would self-match
    for p, r in rx:
        if r.search(path):
            name_hits.append((path, p))
            break
    for i, line in enumerate(text.split("\n"), 1):
        if ALLOW_RE.search(line):
            continue
        for p, r in rx:
            if r.search(line):
                content_hits.append((path, i, p, line.strip()[:100]))


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
    return _report(content_hits, name_hits)


def main():
    if len(sys.argv) < 2:
        print("usage: leak_scan.py staged | range <new> [<base>]", file=sys.stderr)
        return 2
    root = _root()
    rx = _load_patterns(root)
    if not rx:
        return 0  # no patterns -> nothing to enforce
    mode = sys.argv[1]
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

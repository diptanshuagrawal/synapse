#!/usr/bin/env python3
"""
check_test_coverage.py — fail a push when changed source lacks co-changed tests.

The gate, in one sentence: if a push touches an in-scope source module, then a
test that imports that module must be in the same push — otherwise the push is
blocked.

Why import-based (not filename-based): the suite maps many test files to one
module (test_common_enrich_refs.py + test_common_insert_event.py both cover
ingest/common.py). We discover the source↔test edges by parsing the `import`
lines of every test, so the mapping is always accurate to reality.

Scope
-----
In-scope = tracked .py under work-context/{ingest,derive,bin}, excluding tests/,
__init__.py, and anything matching a glob in the exempt file
(.githooks/test-coverage-exempt.txt, committed). Exempt = "we consciously
decided this file needs no test" (CLI drivers, one-shot backfills, legacy not
yet covered). Add a glob there to grandfather a file; remove it once you write
the test.

Decision per changed in-scope source file M (not exempt):
  - no test imports M at all          -> FAIL  (write a test, or exempt M)
  - test(s) import M but none changed -> FAIL  (update the test with the code)
  - a test importing M also changed   -> OK

Modes
-----
  check_test_coverage.py range <base> [<head>]   # diff base..head (head=HEAD if omitted)
  check_test_coverage.py staged                   # diff --cached (commit-time)
  check_test_coverage.py worktree                 # diff working tree (manual)
  check_test_coverage.py --write-exempt           # (re)seed exempt file with
                                                  #   today's uncovered in-scope files

Escape hatch: SKIP_TEST_GATE=1 in the environment bypasses the gate (the
pre-push hook documents this). Prefer it over --no-verify, which also skips the
leak-scan.

Exit: 0 = pass / nothing to check, 1 = blocked, 2 = usage error.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

# ── layout ──────────────────────────────────────────────────────────────────
# Repo root is the git toplevel; source lives under work-context/.
SRC_PREFIX = "work-context/"
IN_SCOPE_DIRS = ("work-context/ingest/", "work-context/derive/", "work-context/bin/")
TESTS_DIR = "work-context/tests/"
EXEMPT_FILE = ".githooks/test-coverage-exempt.txt"

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's canonical empty tree

_IMPORT_RE = re.compile(r"^\s*import\s+([\w.]+)", re.MULTILINE)
_FROM_RE = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+(.+)$", re.MULTILINE)


def _root() -> str:
    return subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()


def _git(root: str, *args: str) -> str:
    return subprocess.check_output(["git", "-C", root, *args], text=True)


def _changed_files(root: str, mode: str, argv: list[str]) -> list[str]:
    if mode == "staged":
        out = _git(root, "diff", "--cached", "--name-only", "--diff-filter=d")
    elif mode == "worktree":
        out = _git(root, "diff", "--name-only", "--diff-filter=d", "HEAD")
        # `git diff HEAD` omits untracked new files — fold them in so a brand-new
        # uncovered module is caught in a manual worktree check too.
        out += "\n" + _git(root, "ls-files", "--others", "--exclude-standard")
    elif mode == "range":
        if not argv:
            print("usage: check_test_coverage.py range <base> [<head>]", file=sys.stderr)
            sys.exit(2)
        base = argv[0]
        head = argv[1] if len(argv) > 1 else "HEAD"
        # New branch: base is the zero sha / empty → diff against origin/main if
        # it exists, else the empty tree (lists every added file).
        if not base or set(base) == {"0"}:
            base = _merge_base_or_empty(root, head)
        out = _git(root, "diff", "--name-only", "--diff-filter=d", f"{base}..{head}")
    else:
        print(f"unknown mode: {mode}", file=sys.stderr)
        sys.exit(2)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _merge_base_or_empty(root: str, head: str) -> str:
    for ref in ("origin/main", "main"):
        try:
            return _git(root, "merge-base", ref, head).strip()
        except subprocess.CalledProcessError:
            continue
    return EMPTY_TREE


def _load_exempt(root: str) -> list[str]:
    fp = Path(root) / EXEMPT_FILE
    if not fp.exists():
        return []
    pats = []
    for line in fp.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            pats.append(s)
    return pats


def _is_in_scope(path: str) -> bool:
    if not path.endswith(".py"):
        return False
    if path.startswith(TESTS_DIR):
        return False
    if path.endswith("/__init__.py") or path.endswith("__init__.py"):
        return False
    return any(path.startswith(d) for d in IN_SCOPE_DIRS)


def _module_tokens(path: str) -> set[str]:
    """Import tokens a test would use to reference the source file at `path`.

    work-context/derive/jira_metrics.py        -> {"derive.jira_metrics", "jira_metrics"}
    work-context/ingest/common.py              -> {"ingest.common", "common"}
    work-context/derive/sub/x.py               -> {"derive.sub.x", "x"}
    work-context/bin/_run_health.py            -> {"_run_health"}  (imported bare)
    """
    rel = path[len(SRC_PREFIX):] if path.startswith(SRC_PREFIX) else path
    stem = rel[:-3] if rel.endswith(".py") else rel
    parts = stem.split("/")
    basename = parts[-1]
    if parts[0] == "bin":
        # bin/ modules are imported by basename (tests put bin/ on sys.path).
        return {basename}
    return {".".join(parts), basename}


def _test_provides(text: str) -> set[str]:
    """Set of import tokens a test file provides (modules it pulls in)."""
    provided: set[str] = set()
    for m in _IMPORT_RE.finditer(text):
        dotted = m.group(1)
        provided.add(dotted)
        provided.update(_prefixes(dotted))
    for m in _FROM_RE.finditer(text):
        pkg = m.group(1)
        provided.add(pkg)
        provided.update(_prefixes(pkg))
        for name in re.split(r"[,\s]+", m.group(2).replace("(", " ").replace(")", " ")):
            name = name.strip()
            if name and name not in ("import", "as", "*"):
                provided.add(name)            # `from ingest import common` → "common"
                provided.add(f"{pkg}.{name}")  # → "ingest.common"
    return provided


def _prefixes(dotted: str) -> set[str]:
    segs = dotted.split(".")
    return {".".join(segs[: i + 1]) for i in range(len(segs))} | {segs[-1]}


def _build_import_index(root: str) -> dict[str, set[str]]:
    """token -> set(test file paths that import it)."""
    idx: dict[str, set[str]] = {}
    tdir = Path(root) / TESTS_DIR
    if not tdir.is_dir():
        return idx
    for tf in sorted(tdir.glob("test_*.py")):
        try:
            text = tf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(tf.relative_to(root))
        for tok in _test_provides(text):
            idx.setdefault(tok, set()).add(rel)
    return idx


def _run_gate(root: str, mode: str, argv: list[str]) -> int:
    if os.environ.get("SKIP_TEST_GATE") == "1":
        print("test-coverage gate: SKIPPED (SKIP_TEST_GATE=1)", file=sys.stderr)
        return 0

    changed = _changed_files(root, mode, argv)
    exempt = _load_exempt(root)
    changed_src = [
        p for p in changed
        if _is_in_scope(p) and not any(fnmatch(p, pat) for pat in exempt)
    ]
    if not changed_src:
        return 0

    changed_tests = {p for p in changed if p.startswith(TESTS_DIR)}
    idx = _build_import_index(root)

    failures: list[str] = []
    for src in changed_src:
        toks = _module_tokens(src)
        tests_for = set()
        for t in toks:
            tests_for |= idx.get(t, set())
        if not tests_for:
            failures.append(
                f"  {src}\n      no test imports it. Add tests/test_…py that imports it, "
                f"or exempt via {EXEMPT_FILE}.")
        elif not (tests_for & changed_tests):
            sample = ", ".join(sorted(tests_for)[:3])
            failures.append(
                f"  {src}\n      changed, but its test(s) were not updated in this push "
                f"(expected one of: {sample}).")

    if failures:
        print("test-coverage gate: BLOCKED — source changed without matching tests:",
              file=sys.stderr)
        for f in failures:
            print(f, file=sys.stderr)
        print("\n  Fix: add/update the test, exempt the file, or SKIP_TEST_GATE=1 to override.",
              file=sys.stderr)
        return 1
    return 0


def _write_exempt(root: str) -> int:
    """(Re)generate the exempt file = every in-scope tracked .py with no test."""
    idx = _build_import_index(root)
    tracked = _git(root, "ls-files", "work-context/ingest", "work-context/derive",
                   "work-context/bin").splitlines()
    uncovered = []
    for p in tracked:
        p = p.strip()
        if not _is_in_scope(p):
            continue
        toks = _module_tokens(p)
        if not any(idx.get(t) for t in toks):
            uncovered.append(p)
    header = (
        "# test-coverage-exempt — globs (repo-relative) skipped by the\n"
        "# pre-push test-coverage gate (bin/check_test_coverage.py).\n"
        "# Each entry = \"this source file consciously needs no test\".\n"
        "# Remove an entry once you add its test. Seeded with the modules that\n"
        "# had no test when the gate was introduced (grandfathered).\n"
    )
    body = "\n".join(sorted(uncovered))
    (Path(root) / EXEMPT_FILE).write_text(header + body + "\n", encoding="utf-8")
    print(f"wrote {len(uncovered)} exempt entries → {EXEMPT_FILE}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = _root()
    mode = sys.argv[1]
    if mode == "--write-exempt":
        return _write_exempt(root)
    return _run_gate(root, mode, sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())

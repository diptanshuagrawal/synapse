#!/usr/bin/env bash
# Claude Code PreToolUse(Bash) gate — IN-SESSION repo-docs refresh before a publish.
#
# Fires BEFORE a `git push` / `git publish` Bash command runs, AHEAD of the review
# gate. Full publish chain:
#   order:  [this docs pass]  ->  review  ->  leak gate  ->  tests
#
# Docs MUST be updated before the review is approved: the docs commit moves HEAD,
# and the review marker is HEAD-scoped — approving first would just be invalidated.
# Because the docs commit lands before the push retry, the updated docs ride along
# in the SAME push that triggered this gate.
#
# Decision protocol (PreToolUse):
#   exit 0, no output  -> no objection (push proceeds to the review gate)
#   permissionDecision=deny -> blocked; reason tells the session how to update+approve
#
# Approval marker (gitignored, HEAD-scoped): work-context/state/.publish_docs_ok
# Bypass docs only:      SKIP_DOCS=1 git publish
# Full emergency bypass: SKIP_REVIEW=1 git publish   (skips this gate too, or --no-verify)
set -uo pipefail

input="$(cat)"

# Fast path: ignore any Bash command that can't be a push/publish (keeps the
# per-Bash-call overhead near-zero for the common case).
case "$input" in
  *push*|*publish*) ;;
  *) exit 0 ;;
esac

python3 - "$input" <<'PY'
import json, os, re, shlex, subprocess, sys

try:
    payload = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)  # unparseable input -> don't interfere

cmd = (payload.get("tool_input") or {}).get("command", "") or ""

# Explicit bypass / non-publishing variants — never gate these. SKIP_REVIEW=1 is
# the documented full-emergency bypass, so it skips this gate as well.
if any(t in cmd for t in ("--dry-run", "--help", " -h", "SKIP_DOCS=1",
                          "SKIP_REVIEW=1", "--no-verify")):
    sys.exit(0)

# Fire ONLY when the command actually runs `git push` / `git publish` as a
# SUBCOMMAND — not when "push"/"publish" merely appears in a path arg or a commit
# message. So `git status .githooks/pre-publish-docs.sh` and
# `git commit -m "fix push bug"` must pass; `git push origin main` must not.
_VAL_OPTS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace",
             "--exec-path", "--config-env", "--super-prefix"}


def _runs_git_push(statement):
    try:
        toks = shlex.split(statement)
    except ValueError:
        toks = statement.split()
    i, n = 0, len(toks)
    # Strip a leading `env ...` wrapper (the git publish alias uses one) and any
    # VAR=value assignments before the program name.
    while i < n:
        t = toks[i]
        if t == "env":
            i += 1
            while i < n:
                u = toks[i]
                if u in ("-u", "--unset", "-S", "--split-string"):
                    i += 2  # option + its value
                elif u.startswith("-") or re.match(r"^[^/=]+=", u):
                    i += 1
                else:
                    break
            continue
        if re.match(r"^\w+=", t):  # FOO=bar assignment before the program
            i += 1
            continue
        break
    if i >= n:
        return False
    prog = toks[i]
    if prog != "git" and not prog.endswith("/git"):
        return False
    i += 1
    while i < n:  # skip git's global options to reach the subcommand
        t = toks[i]
        if t.startswith("-"):
            i += 1
            if t in _VAL_OPTS:
                i += 1  # this global option consumes the next token as its value
            continue
        return t in ("push", "publish")  # first bare token = the subcommand
    return False


# Split into shell statements so `… ; git push` / `… && git push` are caught.
if not any(_runs_git_push(s) for s in re.split(r"&&|\|\||[;\n|]", cmd)):
    sys.exit(0)


def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()


root = git("rev-parse", "--show-toplevel")
# Repo-scope: only enforce in repos that have this gate installed (i.e. this one).
# A global registration is therefore a silent no-op everywhere else.
if not root or not os.path.exists(os.path.join(root, ".githooks", "pre-publish-docs.sh")):
    sys.exit(0)

head = git("rev-parse", "HEAD")
marker = os.path.join(root, "work-context", "state", ".publish_docs_ok")

# Approved already? Marker must name the exact commit being pushed. Left in place
# (not consumed) so a same-HEAD retry after a downstream gate is not re-checked;
# any new commit changes HEAD and forces a fresh docs pass.
if head and os.path.exists(marker):
    try:
        if open(marker).read().strip() == head:
            sys.exit(0)  # no objection
    except OSError:
        pass

reason = (
    "REPO-DOCS UPDATE REQUIRED before this push (runs FIRST, ahead of the review "
    "gate — a docs commit moves HEAD and would invalidate a review marker written "
    "beforehand, so do docs BEFORE approving the review).\n"
    "1. Diff the push range:\n"
    "     git diff @{push}..HEAD   (fallback: git diff origin/main...HEAD)\n"
    "2. Identify repo docs the diff makes stale: README.md, docs/**, any *.md that "
    "describes the changed code (skill SKILL.md files, derive/*.md, prd/*.md that "
    "document behaviour the diff changed).\n"
    "3. Update those docs and COMMIT the changes so they ride in this same push. "
    "If NOTHING is stale, that's a valid outcome — verify, then approve without a "
    "docs commit.\n"
    "4. Approve for the FINAL HEAD (after any docs commit) and retry the push:\n"
    f"     git rev-parse HEAD > {marker}\n"
    "     <re-run the original push command>\n"
    "Do NOT write the marker without actually checking the docs against the diff. "
    "Bypass docs only: SKIP_DOCS=1 git publish. "
    "Full emergency bypass: SKIP_REVIEW=1 git publish"
)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": reason,
}}))
sys.exit(0)
PY

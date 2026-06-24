#!/usr/bin/env bash
# Claude Code PreToolUse(Bash) gate — IN-SESSION code review before a publish.
#
# Fires BEFORE a `git push` / `git publish` Bash command runs, so it is the FIRST
# gate in the publish chain. When it allows the push, the repo's own git pre-push
# hook (.githooks/pre-push) runs next: leak gate -> test-coverage gate -> tests.
#   order:  [this review]  ->  leak gate  ->  tests
#
# Unlike a git hook (which can only spawn a detached `claude -p`), this runs in the
# session that issued the push: it DENIES the command and instructs that session to
# spin up a NEW review subagent over the push diff. The review therefore uses the
# live session's auth and is visible in the conversation. The push is only allowed
# once the session records an approval marker for the EXACT commit being pushed.
#
# Decision protocol (PreToolUse):
#   exit 0, no output  -> no objection (push proceeds to the git pre-push gates)
#   permissionDecision=deny -> blocked; reason tells the session how to review+approve
#
# Approval marker (gitignored, HEAD-scoped): work-context/state/.publish_review_ok
# Bypass for a true emergency:  SKIP_REVIEW=1 git publish   (or --no-verify)
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

# Explicit bypass / non-publishing variants — never gate these.
if any(t in cmd for t in ("--dry-run", "--help", " -h", "SKIP_REVIEW=1", "--no-verify")):
    sys.exit(0)

# Fire ONLY when the command actually runs `git push` / `git publish` as a
# SUBCOMMAND — not when "push"/"publish" merely appears in a path arg or a commit
# message. So `git status .githooks/pre-publish-review.sh` and
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
if not root or not os.path.exists(os.path.join(root, ".githooks", "pre-publish-review.sh")):
    sys.exit(0)

head = git("rev-parse", "HEAD")
marker = os.path.join(root, "work-context", "state", ".publish_review_ok")

# Approved already? Marker must name the exact commit being pushed. Left in place
# (not consumed) so a same-HEAD retry after a flaky test gate is not re-reviewed;
# any new commit changes HEAD and forces a fresh review.
if head and os.path.exists(marker):
    try:
        if open(marker).read().strip() == head:
            sys.exit(0)  # no objection
    except OSError:
        pass

reason = (
    "PRE-PUBLISH REVIEW REQUIRED before this push (runs first, ahead of the leak "
    "gate and tests).\n"
    "1. Spin up a NEW review subagent (Agent tool, subagent_type 'cavecrew-reviewer') "
    "over the diff about to be pushed:\n"
    "     git diff @{push}..HEAD   (fallback: git diff origin/main...HEAD)\n"
    "2. It flags HIGH-SEVERITY issues ONLY: correctness bugs, data loss, leaked "
    "secrets/PII, broken logic, crashes, removed or weakened tests. Skip style nits.\n"
    "3. If it finds blocking issues, FIX them (or check with the user), then re-review.\n"
    "4. Only once the review passes with NO high-severity findings, approve and retry "
    "the push:\n"
    f"     git rev-parse HEAD > {marker}\n"
    "     <re-run the original push command>\n"
    "Do NOT write the marker without actually running the review subagent. "
    "True emergency bypass: SKIP_REVIEW=1 git publish"
)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": reason,
}}))
sys.exit(0)
PY

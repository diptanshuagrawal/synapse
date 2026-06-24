Housekeeping with a classification layer: prune the known-safe stuff, then scan for FURTHER cleanup candidates, judge each, and post Approve/Reject cards to #rollup.

## Usage — `/housekeeping-review [dry|prune|help]`

If invoked with `help`, `-h`, or `--help`: print this Usage block verbatim and STOP.

**What it does:** the deterministic prune (old backups/verdicts/handoffs/logs/.DS_Store + stale-MPIM) PLUS a classification layer that surfaces further cleanup candidates (DB backups, oversized logs, `__pycache__`, stale `derived/`/`state/`, preview bloat, large/untracked files, abandoned worktrees), judges each as delete/truncate/keep/investigate, and posts the actionable ones to #rollup as Approve/Reject cards. Suggest-only — nothing is deleted without an explicit Approve click.

**Modes:**
- (none) — full run: prune → scan → classify → render → post cards to #rollup.
- `dry` — scan → classify → render the local report ONLY; do NOT prune, do NOT post.
- `prune` — run only the deterministic prune (`bin/housekeeping.sh --apply`); no classification.

User input: `$ARGUMENTS`.

Working dir for python: `$HOME/context/work-context`. The scan/prune run the project venv
internally (via housekeeping.sh). The render + relay-post need `slack_sdk` + `yaml` (SYSTEM
python3, not the venv) — resolve:
`PY=$(for p in /opt/homebrew/bin/python3 python3 /usr/local/bin/python3; do "$p" -c 'import yaml, slack_sdk' 2>/dev/null && { echo "$p"; break; }; done)`

## STEP 1 — deterministic prune (skip if mode == `dry`)
```bash
cd "$HOME/context/work-context" && bash bin/housekeeping.sh --apply 2>&1 | tee -a logs/housekeeping.log
```
This is the existing job, unchanged. In `prune` mode, STOP here.

## STEP 2 — scan for further candidates
```bash
cd "$HOME/context/work-context" && bash bin/housekeeping.sh --scan
```
Writes `state/housekeeping_candidates.json` (facts only — no deletion).

## STEP 3 — classify + render + post
Follow `.claude/shared/housekeeping-classify.md` (STEP A–D): read the candidates, write a
`state/housekeeping_verdicts.json`, render with `derive/housekeeping_render.py`, then — unless
mode == `dry` — post the cards with `bin/relay_bot.py --post-housekeeping <run-id>`.

In `dry` mode: render only, then show the owner the top of `derived/housekeeping-suggestions.md`
(proposed + reclaimable total) and STOP — no Slack post.

## Output to the owner (chat)
After a full run, summarise in the chat-reply style (bottom line first):
- one line: `Pruned <files>/<bytes>; <N> further candidates → <K> proposed (<reclaim>) posted to #rollup, <R> to review.`
- then the `## ✅ Proposed for cleanup` section of the report (paths + sizes), so it's reviewable without opening Slack.
- end with: "Approve/Reject in #rollup — Approve deletes (git-safe), Reject skips it for good."

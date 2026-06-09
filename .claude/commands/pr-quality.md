Classify PR review comments into root-cause categories, then apply.

This is the chat-classify half of the PR-quality scorer (PRD:
`prd/pr-quality-scorer.md`). Mechanical friction (review rounds, rework
commits, time-to-merge) is already computed by `derive/github_metrics.py`.
This skill adds the *nature* of each review comment so the friction score
reflects WHAT went wrong, not just how much.

Standalone skill — reuses the rollup dump→classify→apply harness but is never
folded into `/rollup`.

## Usage — `/pr-quality [days|all]`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run anything.

**What it does:** Chat-classify half of the PR-quality scorer — classifies PR review comments into root-cause categories, then applies verdicts.

**Part of:** the PR-quality scorer (PRD `prd/pr-quality-scorer.md`). This is the CLASSIFY half — adds the *nature* of each review comment (root-cause category) on top of the mechanical friction `github_metrics.py` already computes. `/pr-report` renders the consumption half.

**Usage:** `/pr-quality [days|all]`
- optional — number N → last N days (default 45); `all` → full backlog, no window.

Optional arg `$ARGUMENTS`:
- a number N → classify comments from the last N days (default 45)
- `all` → no date window (the full ~7k-comment backlog, chunked)

**Scope: team-only by default.** The dump only emits comments on PRs authored
by a `scope: team` member (people.yaml). Pass `--all-authors` to classify the
full org-wide backlog instead. Friction (`github_metrics.py`) and the report
(`pr-report`) share the same team-only default + `--all-authors` escape hatch.

## Phase 1 — Dump pending (idempotent, chunked)

```bash
cd $HOME/context/work-context && .venv/bin/python derive/pr_quality_dump.py --since-days 45 --limit 50
```

- If `$ARGUMENTS` is a number, pass `--since-days $ARGUMENTS`.
- If `$ARGUMENTS` is `all`, pass `--all` instead of `--since-days`.
- The dump only emits comments not yet classified, so re-running walks the
  backlog in chunks of `--limit`.
- Keep `--limit` at ~50: the pending file must fit in one Read (≈13k tokens at
  50). This makes each `/loop` iteration self-contained in fresh context.
- When `[summary]` reports `nothing to classify`, the backlog is clear — end
  the loop (don't schedule another iteration).

Output ends with `[summary] N comments to classify (M remain after this chunk)`
or `nothing to classify`. If nothing, **stop** — backlog is clear.

## Phase 2 — Classify

First read the rules (canonical taxonomy), then the pending file:
- `$HOME/context/work-context/state/pending_pr_comments.rules.md`
- `$HOME/context/work-context/state/pending_pr_comments.json`

For each `pending[]` entry, emit ONE verdict tagging the single dominant
root-cause category. Schema (copy verbatim, no extra keys):

```json
{
  "event_id": "<echo unchanged from pending>",
  "category": "business-logic|correctness|test-gap|design|security|naming|nit|question|praise",
  "confidence": 0.0
}
```

### Hard rules

- **One dominant category per comment.** When two issues are raised, pick the
  more serious (correctness/business-logic/security > design/test-gap >
  naming/nit).
- **Do NOT label source.** `source` (human/matterai) is already on the pending
  row and carried through automatically. Never put it in the verdict.
- **business-logic vs correctness:** business-logic = "doesn't match the
  spec/intent"; correctness = "buggy regardless of spec".
- **design vs naming:** module/boundary/abstraction = design; one identifier =
  naming.
- **Classify matterai comments with the same taxonomy** as human ones — the
  human-vs-matterai delta is the whole point of the coverage-gap analysis.
- **confidence < 0.6** → comment stays pending (apply drops it). Don't
  fabricate certainty on a terse body.

Write the array to:
`$HOME/context/work-context/state/verdicts.pr_comments.json`

Either `{"verdicts": [ … ]}` or a bare array is accepted. Print the category
distribution you produced (counts per category).

## Phase 3 — Apply + recompute friction

```bash
cd $HOME/context/work-context && .venv/bin/python derive/apply_pr_classes.py
```

Apply validates, writes `pr_comment_class`, and recomputes `pr_friction` for
the affected PRs (category weights now replace the mechanical-only score).
Print the accepted/rejected counts and how many PRs were recomputed.

If `[summary]` reported comments remaining, offer to run another chunk.

# PR review comment classification rules

Canonical taxonomy + weights for the `/pr-quality` classify pass
(PRD: prd/pr-quality-scorer.md). The scorer in `derive/github_metrics.py`
imports these category names — keep the two in sync.

The job: read one review/comment body and tag it with the SINGLE dominant
**root-cause category**. The point is not "how severe" but "what kind of
attention the PR was missing" — the category mix per PR diagnoses where the
author's process broke down.

## Categories

| Category | Tag when the comment is about… | Weight |
|---|---|---|
| `business-logic` | a wrong/missing requirement, flow gap, an edge case implied by the spec/PRD, behaviour that doesn't match intent | 1.0 |
| `correctness` | a concrete bug, wrong result, null/error handling, race, off-by-one — code that is simply wrong | 1.0 |
| `security` | auth, secrets, injection, data exposure, unsafe input | 1.0 |
| `design` | architecture, coupling, wrong abstraction, where code lives, naming of modules/boundaries | 0.6 |
| `test-gap` | missing / insufficient / incorrect tests, no coverage for the change | 0.6 |
| `question` | reviewer is asking for clarification / context they couldn't get from the PR | 0.3 |
| `naming` | a single identifier's clarity (variable/method/field name) | 0.1 |
| `nit` | style, formatting, typos, import order, trivially cosmetic | 0.1 |
| `praise` | positive feedback, "LGTM", approval with no ask | 0.0 |

Weights drive the friction score (human comments count more than matterai —
that's applied by the scorer, not you). Pick the category, not the weight.

## How to choose

1. **One dominant category per comment.** If a comment raises two things,
   pick the one that would matter most (correctness/business-logic/security
   beat design/test-gap beat naming/nit).
2. **business-logic vs correctness:** business-logic = "this doesn't do what
   the spec wants"; correctness = "this code is buggy regardless of spec".
3. **design vs naming:** module/boundary/abstraction = design; a single
   identifier = naming.
4. **Bot vs human:** classify matterai comments with the SAME taxonomy. Source
   (human/matterai) is already recorded — do NOT put it in the verdict.
5. **Pure CI/automation chatter** (coverage bot tables, build logs) is already
   filtered out at dump time; you should not see it. If you do, tag `nit`.
6. **Low confidence:** if the body is too terse to judge, emit confidence < 0.6
   and it stays pending (not written).

## Verdict schema

For each `pending[]` entry emit ONE verdict (copy verbatim, no extra keys):

```json
{
  "event_id": "<echo unchanged from pending>",
  "category": "business-logic|correctness|test-gap|design|security|naming|nit|question|praise",
  "confidence": 0.0
}
```

Write the array to `state/verdicts.pr_comments.json` (either `{"verdicts":[…]}`
or a bare array). Then run apply (see `/pr-quality`).

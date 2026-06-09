Classify and apply team leave mentions from Slack.

This is the chat-classify half of the leave-tracking pipeline. Phase 1
(regex prefilter + render of already-classified rows) runs nightly via
`launchagents/com.example.leaves.plist`. This skill runs the LLM half.

## Usage — `/leaves [days]`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run anything.

**What it does:** Chat-classify half of the team-leave pipeline — classifies regex-prefiltered Slack leave mentions, applies them, and re-renders the leaves table.

**Usage:** `/leaves [days]`
- `days` (optional) — lookback window. Empty → dump default (60).

Optional arg: lookback window in days. User input: `$ARGUMENTS`.
If empty, dump uses its default (60). Otherwise re-run dump with the
provided window.

## Phase 1 — Refresh pending (idempotent)

```bash
cd $HOME/context/work-context && .venv/bin/python derive/leaves_dump.py
```

If `$ARGUMENTS` is non-empty, pass `--days $ARGUMENTS` to override the window.

Output ends with `[summary] N events awaiting /leaves chat classify` (or
`nothing to classify`). If nothing, **stop here** — Phase 1 cron already
re-rendered the markdown.

## Phase 2 — Classify

First, read the rules — do this before reading the pending file:
`$HOME/context/work-context/state/pending_leaves.rules.md`

Then read all pending events:
`$HOME/context/work-context/state/pending_leaves.json`

For each `pending[]` entry, emit one verdict. Schema (copy verbatim,
do NOT add extra keys):

```json
{
  "event_id": "<echo unchanged from pending>",
  "is_leave": true,
  "confidence": 0.0,
  "leaves": [
    {
      "actor": "<canonical handle from team_canonical>",
      "date_start": "YYYY-MM-DD",
      "date_end": "YYYY-MM-DD",
      "reason": "wfh|vacation|sick|holiday|ooo|travel|other"
    }
  ]
}
```

### Hard rules

- **`is_leave: false`** when the regex match was a false positive
  (wrong sense, past tense reference, irrelevant). `leaves: []` in that
  case. Marking processed prevents re-emission.
- **Resolve relative dates** against `mentioned_at`. "Tomorrow" = +1d.
  "Next Monday" = next calendar Monday after `mentioned_at`. "Till the
  5th" = nearest future 5th of a month.
- **Multi-person mentions** ("@bob and @eve out tomorrow") =
  one verdict with N entries in `leaves[]`.
- **`actor`** MUST be one of `pending.team_canonical`. Mentions of
  non-team folks → drop those entries.
- **Ambiguous dates** ("might be off next week, will confirm") =
  `date_start: null, date_end: null`, `reason: "other"`, confidence ≤ 0.7.
  Will re-emerge next dump if re-mentioned with clearer dates.
- **`confidence < 0.7`** = row rejected by apply, stays pending.
  Don't fabricate certainty.

Write the verdict array to:
`$HOME/context/work-context/state/verdicts.leaves.json`

Either form is accepted:
```json
{"verdicts": [ {...}, {...} ]}
```
or a bare array:
```json
[ {...}, {...} ]
```

Print: total verdicts / how many `is_leave=true` vs false.

## Phase 3 — Apply + render

```bash
cd $HOME/context/work-context && \
  .venv/bin/python derive/apply_leaves.py && \
  .venv/bin/python derive/render_leaves.py
```

Then print the path to `derived/team-leaves.md` and the counts the
renderer emitted (active / upcoming / recent / ambiguous).

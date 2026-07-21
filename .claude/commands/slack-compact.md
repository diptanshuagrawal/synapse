Chat-driven Slack thread compaction. Reads `state/slack_compact_pending.json`, produces 1-line digests per thread, writes `state/slack_compact_verdicts.json`. Then invokes the apply step which writes to `subject_summary` and deletes raw events.

Mirrors `/rollup`'s chat-only classification pattern (per memory `project_chat_only_classification.md` + `project_rollup_429.md`). Scripts NEVER call LLM directly; this skill is where the LLM work happens.

## Usage — `/slack-compact [dump-first]`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run anything.

**What it does:** Chat-driven Slack thread compaction — produces 1-line digests per thread, writes verdicts, then applies to `subject_summary` and deletes raw events.

**Modes:**
- empty → reads pending, classifies, applies (assumes `dump` already run).
- `dump-first` → also runs the dump step before classifying.

Apply step writes 1-line digests to `subject_summary` and deletes the raw events.

**Usage:** `/slack-compact [dump-first]`
- optional — default runs the full compaction cycle; `dump-first` regenerates pending before classifying.

User input: `$ARGUMENTS` (optional). Default: full compaction cycle. Accepted forms:
- _(empty)_ — `dump` step assumed already run; reads pending, classifies, applies
- `dump-first` — also runs the dump step before classifying

## Phase 1 — Pre-flight

```bash
cd $HOME/context/work-context
```

If `$ARGUMENTS == "dump-first"`:

```bash
bin/slack-compact.sh dump --days 365
```

Output JSON contains `{"dumped": N, "path": "..."}`. If `dumped == 0`, print done + stop.

## Phase 2 — Read pending threads

Check the size FIRST — never `cat` the whole file blind (a 365-day dump floods
context before the batching rule below can help):

```bash
python3 -c "import json;d=json.load(open('state/slack_compact_pending.json'));print(len(d),'threads')"
```

≤20 threads → read the whole file. More → Read it in ~20-thread slices from the
start (offset/limit or a python slice dump per batch), classifying + writing
verdicts per slice before reading the next.

File is a JSON list of thread objects:

```json
[
  {
    "subject": "slack:C0EXAMPLE:1778667150.756969",
    "channel_id": "C0EXAMPLE",
    "first_ts": "2025-01-15T10:23:45Z",
    "last_ts":  "2025-01-15T18:42:11Z",
    "msg_count": 14,
    "messages": [
      {"actor": "U0EXAMPLE", "ts": "...", "body": "...", "edited_ts": null, "thread_ts": null},
      {"actor": "U0EXAMPLE", "ts": "...", "body": "...", "edited_ts": null, "thread_ts": "1778667150.756969"},
      …
    ],
    "refs": {
      "person": ["alice-example", "frank-example"],
      "ticket": ["EX-2660"],
      "pull_request": ["example-org/service-a#629"]
    }
  },
  ...
]
```

(Batching mechanics per Phase 2 above: slice reads of ~20 threads → verdicts → write → next slice.)

## Phase 3 — Classify each thread

For each thread, produce **one** verdict object:

```json
{
  "subject": "<echo unchanged>",
  "digest": "Action-first 1-line summary ≤200 chars",
  "confidence": 0.90,
  "ops_pattern": "incident" | "drill" | "rca" | "year_end" | "rollback" | null,
  "narrative_tags": ["<canonical>", "<canonical>"]    // optional — who was central
}
```

### Digest content rules

- ≤ 200 characters
- Action-first, present tense
- Names the central decision / outcome / blocker
- Cites canonical people resolved via `refs.person` when relevant
- Cites tickets / PRs only if central to the thread (don't dump all refs into prose)

### Good digest examples

> "Year-end balance fix: Alice flagged double-credit in EA account; Bob rolled patch service-a#612; EX-2642 filed as follow-up."

> "DR drill readiness: Carol owns checklist; Dan flagged scaling-event misfire; rollback dry-run passed."

> "Withholding slab mismatch service-c vs service-a: discussion converged on aligning rule order; EX-2541 fix authored by Alice."

### Bad digest examples (avoid)

- "Thread had many messages about various things." — vague
- "Alice said this, then Bob said that, then they fixed it." — narration not signal
- 350+ characters — too long
- Just enumerating refs without saying what happened

### ops_pattern classification

Match against the `ops_pattern_match` enum (PRD `slack-ingest.md` §7.2):
- `incident` — thread starts with or pivots to a P0/P1/outage discussion
- `drill` — DR drill / fire drill / gameday
- `rca` — explicit root cause / post-mortem in the thread (separate from the actual incident thread)
- `year_end` — financial year-end / FY-end / EOY work
- `rollback` — explicit rollback / revert / hotfix decision
- `null` — no ops pattern; generic team discussion / decision

### Confidence rubric

- 0.95 — thread had clear scope + outcome; digest captures it tightly
- 0.85 — clear topic but outcome partly inferred
- 0.70 — sparse thread; digest captures starting question but resolution unclear
- < 0.7 — would be rejected by apply step. Don't bother writing verdict; leave for next compaction window.

## Phase 4 — Write verdicts

Write the complete JSON array to:

```
$HOME/context/work-context/state/slack_compact_verdicts.json
```

Print: count produced, count deferred (low-conf), batches if any.

## Phase 5 — Apply

```bash
bin/slack-compact.sh apply
```

Reads verdicts → writes subject_summary → deletes events + event_refs for compacted threads → archives verdicts file. Raw JSONL preserved untouched.

Output JSON contains `{"applied": N, "skipped_low_conf": N, "errors": [...], "archived_to": "..."}`.

## Phase 6 — Print summary

```
✓ slack-compact complete @ <ts>

threads compacted:     <N>
deferred (low conf):   <N>
errors:                <list>
events deleted:        <sum from apply>
subject_summary added: <N digests>

Archived verdicts: state/slack_compact_verdicts.<ts>.json
```

## Hard constraints

- **NEVER call any LLM from scripts.** This skill is the ONLY place LLM digestion runs. Per memory `project_chat_only_classification.md`.
- Digest ≤ 200 chars, action-first.
- Confidence < 0.7 → don't write the verdict; thread stays in events for the next compaction window.
- Skips channels with `compaction_policy: never` (dump script already excludes these).
- Apply step is destructive on `events` + `event_refs` — owner must run intentionally. The skill chains dump → classify → apply in one flow but each phase logs separately.
- Raw JSONL (`raw/slack/YYYY/MM/DD.jsonl`) is **preserved** — compaction can be replayed if needed.

## When to run

Manual, like `/rollup`. Triggers:
- `events.db` size approaches 200 MB (slack rows dominate growth)
- Year boundary — compact prior calendar year's old threads to keep narrative window clean
- After a major projects.yaml or people.yaml change — re-classify older threads with fresh slug coverage

NOT automated. NO scheduled-task routine. Owner-invoked only.

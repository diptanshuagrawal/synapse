# Shared harness — dump → classify → apply (maker-checker)

Loaded by the chat-classify skills (`/classify`, `/rollup` Phase 2-3, `/pr-quality`,
`/leaves`). This owns the COMMON process + invariants. Each skill keeps its own verdict
**schema**, **triage rules**, **script names**, and **state-file paths** — those are
skill-specific and stay local.

## The three phases

1. **Dump (idempotent).** Run the skill's dump script. It emits only not-yet-classified
   subjects, so re-running walks the backlog. If the summary says **`nothing to classify`**
   (or equivalent), **STOP** — the backlog is clear; do not write an empty verdicts file, and
   exit the skill (in a loop/automated context, do not schedule another iteration).
2. **Classify (chat).** Read the RULES file FIRST, then the pending file (see "Rules first"
   below). Emit exactly **one verdict per subject** in the skill's schema. Write the verdict
   array to the skill's `state/verdicts.*.json`.
3. **Apply (deterministic script).** Run the skill's apply script — the ONLY writer. It
   validates, writes, and recomputes/renders. Print the accepted / rejected (and any
   "remaining") counts.

## Maker-checker invariant (why this is safe)

- **Maker** = the chat classify step: proposes verdicts only, **zero** writes to any live
  source (events.db, Jira, Slack, Confluence).
- **Checker / Apply** = the deterministic apply script: the single writer, idempotent,
  records provenance. It re-runs cleanly on partial state.
- Never collapse the two — classify must not mutate; apply must not classify.

## Chat-only classification (no LLM API in scripts)

Scripts ONLY dump + apply. ALL LLM work (classification, synthesis, judgement) happens in
chat. No `derive/*.py` script calls the Anthropic/Claude API for classification. New
patterns go in the rules/config (`projects.yaml` keywords, `rules.md`), never hardcoded in
code. (See `[[feedback_openai_embeddings_only]]` — embeddings are the only sanctioned API
call in scripts.)

## Rules first

Read the skill's `*.rules.md` **before** reading the pending file. The rules file is the
authoritative source for the slug/category enum, the verdict schema, and the confidence
threshold. Apply them exactly — do not let judgement override the rules.

## Verdict-schema strictness

Copy the skill's verdict schema **verbatim — no extra keys.** Apply scripts expect a fixed
shape; unknown keys are silently dropped (or cause rejects). Echo pass-through fields
(`subject` / `event_id` / `content_hash`) unchanged. Don't resurrect dead flags.

## Confidence gate (canonical threshold = 0.7)

`confidence` is calibrated 0–1. **Below 0.7 → the row is rejected by apply and stays pending**
for the next run (it re-emerges if re-seen with clearer signal). Don't fabricate certainty on
thin signal. For skills that fetch extra context (e.g. a PR diff), you should reach ≥ 0.70 for
almost all cases after the fetch — only emit < 0.7 if the source itself is empty/unreadable.

## Verdict file format

The apply scripts accept EITHER a bare JSON array `[ {...}, {...} ]` OR the wrapped form
`{"verdicts": [ {...} ]}`. Either is fine.

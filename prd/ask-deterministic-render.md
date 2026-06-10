# Deterministic render for `/ask person_range` (and siblings)

**TL;DR:** Move the determinism boundary so scripts decide *what* to surface/cite/flag/rank; the model only phrases a fixed manifest, then a gate verifies every item landed. Same window → same facts. Parallel `ask-v2` only — wire live after owner approval.

**Status:** DESIGN — not wired. Parallel version only.
**Author:** generated 2026-06-09

**Problem.** Two runs of the same `/ask` over the same window gave different narratives (missed a "getting overwhelmed" workload quote, different cited tickets, different framing). Scripts are deterministic; the *synthesis* (what to surface/cite/read/phrase) happens in the chat model and is stochastic.

---

## 1. Goal + non-goal

**Goal.** Make the *content* of every `/ask` deterministic: same window → same facts, flags, citations, section structure, ordering. Runs differ only in surface wording.

**Non-goal.** Byte-identical prose. We do NOT template prose (kills the manager-quality narrative the skill exists for). Make every *decision* deterministic; leave only phrasing to the model.

**Boundary moves from:**
> scripts compute signals → model decides what to surface + cite + read + phrase

**to:**
> scripts compute signals AND decide what to surface + cite + flag + rank → model only phrases a fixed manifest, then a gate verifies every item landed.

---

## 2. The three deterministic layers

### Layer A — Render manifest (selection is computed, not chosen)

A new deterministic stage emits the EXACT render plan: which artefacts to cite, in which section, what order, capped. The model renders the list; it does not curate it. The script already picked the "top 5".

Proposed schema (one JSON object from the new stage):

```jsonc
{
  "schema_version": "render-1",
  "person": "eve-example",
  "window": { "since": "...", "until": "..." },

  "headline": {                      // drives TL;DR framing — deterministic
    "work_mix": "platform",
    "baseline_role": "platform",
    "feature_yardstick_applicable": false,
    "verdict_line": "platform-weighted window; feature-SP is not the yardstick",
    "tldr_facts": [                  // ranked, capped at 6, each = one bullet
      { "text_key": "owned_list_txn_api", "cite": ["EX-2596","EX-2395"] },
      { "text_key": "cash_execute_changes", "cite": ["EX-2616","EX-2658"] }
      // ...
    ]
  },

  "sections": {                      // every section is a fixed list of render items
    "shipped":      [ { "cite": "EX-2596", "title": "...", "role": "author", "rank": 1 }, ... ],
    "designed":     [ { "cite": "page:EXAMPLE_PAGE_ID", "title": "service-a CASH API Contract", "rank": 1 }, ... ],
    "db_platform":  [ ... ],
    "ops":          [ ... ],
    "workstreams":  [ { "name": "cash-on-service-a", "role": "led", "cites": [...], "rank": 1 }, ... ]
  },

  "flags": [                         // Layer B output. Deterministic.
    { "kind": "workload_sentiment", "evidence": "slack:C0EXAMPLE:177...",
      "quote": "getting a little overwhelmed", "severity": "review-in-1:1" },
    { "kind": "commit_without_pr", "metric": { "commits_in_pr": 123, "own_prs": 0 } }
  ],

  "behavioral": { ... },             // already deterministic in person_v3
  "completion": { ... },
  "caveats": [                       // computed: attribution fallback, no-PR quality gap, etc.
    { "kind": "sp_attribution_fallback", "changelog": 4, "fallback": 6 },
    { "kind": "no_own_prs", "implication": "no code-quality/merge-speed signal" }
  ],

  "verify_manifest": [               // Layer C input — every must-appear token
    "EX-2596","EX-2616","EX-2558","service-b#106",
    "flag:workload_sentiment","flag:commit_without_pr",
    "caveat:sp_attribution_fallback","caveat:no_own_prs"
  ]
}
```

**Selection rules** (deterministic, in code — examples):
- `shipped` = tickets with terminal status in `shipped` class, assigned-at-close to person; ranked by (epic-grouped, then story-points desc, then ticket id).
- `designed` = Confluence pages authored/edited by person; ranked by body bytes, capped at N.
- `workstreams` = led-first (AUTHOR/RESOLVER/DECIDER window role), then contributed; ranked by person-subject count. Cap each section.
- `tldr_facts` = top 6 by fixed priority: own delivery > led design > cross-team coordination > db/platform > ops. No model choice.

### Layer B — Deterministic body extraction (the "overwhelmed" fix, generalized)

The missed quote happened because *reading bodies is a model judgment call*. Fix: extract body-level facts in code, emit as structured fields — the model never decides whether to open a thread.

`person_deepread` (parallel copy) gains a `body_facts` pass over every thread/ticket/PR/page body it already pulls:

| Extractor | What it pulls | How (deterministic) |
|---|---|---|
| `decisions` | decision statements | already in topic_brief; lift verbatim + cite |
| `impact_numbers` | RPS, latency, %, accounts, ₹ amounts | regex over bodies |
| `dates` | go-live / rollout / disable dates | date regex + keyword proximity |
| `approvers` | named approver / decider | "approved by", "@x please approve" patterns |
| `rollback_flags` | rollback / emergency / revert | keyword set |
| `sentiment_flags` | overwhelmed, stretched, blocked, too much, can't keep up | phrase list, per person, own-authored only |
| `risk_phrases` | race condition, data loss, panic, critical | keyword set |

Each extractor emits `{ kind, evidence_subject, snippet }`, becoming `flags[]` + `caveats[]` in the manifest. Phrase lists live in `config/body_extractors.yaml` (auditable + tunable, not buried in code).

Key property: extraction is over the SAME bodies deepread already fetches — no new fetch, no new cost.

### Layer C — Verify gate (guarantees the manifest landed)

After the model writes the narrative, a deterministic post-check asserts every `verify_manifest` token appears in the output text:
- every cited ticket id / PR / page
- every `flag:*`
- every `caveat:*`

Missing token → run is incomplete → regenerate (or flag exactly which items dropped). Makes "did all required facts land" deterministic even though prose isn't. Also catches the silent-section-drop bug the render contract warns about.

Implementation: a tiny `derive/verify_render.py` takes the manifest + written `.md`, exits non-zero with missing tokens listed.

---

## 3. What stays variable (honest boundary)

- Sentence wording, paragraph flow, connective phrasing.
- Which synonym; how a fact is framed in prose.

These don't change the read. A manager gets the same facts, flags, citations, and structure every time.

If true byte-determinism is ever required, Layer A's manifest is already template-ready — a Jinja pass would produce identical bytes at the cost of prose quality. Option kept open; NOT taken now.

---

## 4. Separate-version layout (nothing live is touched)

All new files. Live `/ask` keeps working unchanged until wiring is approved.

```
derive/person_v4_manifest.py      # NEW — builds the render manifest from
                                  #       person_v3 + deepread output (wraps them,
                                  #       does not modify them)
derive/verify_render.py           # NEW — Layer C gate
config/body_extractors.yaml       # NEW — phrase/regex lists for Layer B
.claude/commands/ask-v2.md        # NEW — parallel skill that consumes the
                                  #       manifest + runs the verify gate.
                                  #       Same format contract, zero selection
                                  #       freedom for the model.
prd/ask-deterministic-render.md   # this doc
```

`person_v3.py` / `person_deepread.py` / `.claude/commands/ask.md` are READ by the new code, NEVER edited in this phase.

### Validation plan (before wiring)

1. Run `ask-v2` for one person over a month 3× → assert 3 outputs carry identical manifests + identical verify-pass (prose may differ).
2. Diff against the live `-2` / `-3` files → confirm the overwhelm flag + all cited tickets now appear in EVERY run.
3. Run for 2-3 other people (a feature dev, an ops-heavy IC) → confirm selection rules generalize, not overfit to one person.

---

## 5. Wiring plan (DEFERRED — only after owner approves the parallel version)

1. Owner reviews `ask-v2` outputs side-by-side with live `/ask`.
2. On approval: fold the manifest stage into `person_v3` (or keep as wrapper); replace `.claude/commands/ask.md` synthesis section with the manifest-render + verify-gate flow; retire `ask-v2`.
3. Apply the same manifest pattern to `/retro` and `team_range` (they share the selection-is-stochastic weakness).

---

## 6. Open questions for owner

1. **Scope of v1:** person_range only, or also `summarize` / `rootcauses` in the first parallel cut? (Recommend: person_range only first — highest value, the one that showed the diff.)
2. **Regenerate vs flag on verify-fail:** auto-regenerate silently, or surface "these N items were dropped, rewriting"? (Recommend: surface — honest + debuggable.)
3. **Sentiment extractor sensitivity:** own-authored messages only, or also "X seems stretched" said *about* the person by others? (Recommend: own-authored only for v1 — lower false-positive risk.)

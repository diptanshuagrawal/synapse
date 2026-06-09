---
description: PARALLEL deterministic-render variant of /ask person_range. Consumes a precomputed render manifest + runs a verify gate so two runs over the same window carry identical facts, flags, citations. Not wired into live /ask — validation only.
---

# /ask-v2 — deterministic person_range (PARALLEL VERSION)

**Status:** PARALLEL / VALIDATION. Does NOT replace `/ask`. Same output format
as `/ask person_range`, but the *selection* of what to surface is computed by
`derive/person_v4_manifest.py`, not chosen by the model. Wire into `/ask` only
after owner sign-off (see `prd/ask-deterministic-render.md`).

**Why this exists:** two `/ask` runs over the same window produced different
narratives (missed a real "getting overwhelmed" workload quote, different cited
tickets, different framing). The scripts are deterministic; the synthesis was
not. This variant moves every selection decision into code and verifies the
output, so the only run-to-run variance is wording.

Scope of v1: **person_range only**. Surface-on-fail (not silent regenerate).
Sentiment extraction is **own-authored only**.

---

## Phase 1 — resolve person + window

Same as `/ask`: resolve the name against `config/people.yaml::canonical`
(substring, case-insensitive). Parse the date range to ISO8601 IST per the
`/ask` Phase-2 rules ("april" → since `YYYY-04-01`, until `YYYY-05-01`, etc.).

```bash
cd $HOME/context/work-context
```

## Phase 2 — build the render manifest (deterministic)

```bash
.venv/bin/python derive/person_v4_manifest.py --name "<canonical>" \
    --since "<iso>" --until "<iso>" > /tmp/<canonical>_manifest.json
```

This internally re-runs `person_v3` + `person_deepread` and emits ONE manifest:

- `headline` — `work_mix`, `baseline_role`, `feature_yardstick_applicable`,
  `verdict_note`, and `tldr_facts[]` (the fixed, ranked TL;DR seeds — render
  one bullet per fact, in order, do NOT reorder or add your own).
- `sections.shipped / designed / db_platform / ops / workstreams` — the EXACT,
  ranked, capped lists to cite. Render every item. Do NOT add items not in the
  manifest; do NOT drop items that are in it.
- `flags[]` — `commit_without_pr`, `workload_sentiment`, `risk_callout`. Each
  carries evidence (subject + snippet). Every flag MUST appear in the narrative
  (TL;DR or the matching Signals/Gaps section). The workload flag in particular
  is the regression this whole variant exists to prevent — never omit it.
- `caveats[]` — `sp_attribution_fallback`, `no_own_prs`, etc. Render each in a
  Caveats section.
- `behavioral / completion / ticket_fate / contribution` — render per the
  normal `/ask` translation rules (plain English, no internal jargon).
- `verify_manifest[]` — the tokens the gate will check. You do not render this;
  it's the contract the gate enforces.

**Render rules (unchanged from `/ask`):** all the Phase-4 natural-language
translation + no-internal-jargon + project-level-voice + grep-check rules from
`.claude/commands/ask.md` apply verbatim. The manifest changes WHAT you cite,
not HOW you write. Still: no cluster IDs, no engine/field names, plain-English
behavioral translation, inline citations, real artefact links.

The model's freedom is reduced to **phrasing the manifest**. You may not:
- pick different tickets/docs than the manifest lists,
- skip a flag or caveat,
- reorder or replace the `tldr_facts`.

## Phase 3 — write the narrative file

Write to `management/narratives/per-person/<canonical>-<since>-to-<until>-v2.md`
(the `-v2` suffix keeps it separate from live `/ask` output during validation).
Use the standard `/ask` person_range section order: TL;DR → Signals → Data
silent on → Novel observations → Gaps → Interventions → Detail → Confirmed by
data → Caveats. Header block per `/ask` Phase 5.

## Phase 4 — run the verify gate (MANDATORY)

```bash
.venv/bin/python derive/verify_render.py \
    --manifest /tmp/<canonical>_manifest.json \
    --file management/narratives/per-person/<canonical>-<since>-to-<until>-v2.md
```

- **Exit 0 / VERIFY PASS** → done. Report the path + "verify: PASS".
- **Exit 1 / VERIFY FAIL** → the printed list names every manifest item missing
  from your prose (uncited tickets/pages, an omitted flag, a missing caveat).
  **Surface the list to the owner, then rewrite the narrative to include the
  missing items, and re-run the gate.** Do NOT silently accept a fail. Loop
  until PASS (or, if an item genuinely cannot be placed, say so explicitly with
  the reason — never drop it silently).

## Phase 5 — report

End the chat reply with:

```
**Saved to:** `<absolute path>`
**Verify:** PASS (all N manifest items present)
```

---

## Determinism contract (what this guarantees)

- Same `(name, since, until)` + same `events.db` → **byte-identical manifest**
  (validated: 3 runs, 1 hash).
- Every shipped ticket, design doc, flag, and caveat the data supports is in the
  manifest → in `verify_manifest` → enforced into the prose by the gate.
- The "getting overwhelmed" class of miss cannot recur: body extraction
  (`config/body_extractors.yaml`, stem-based) finds it deterministically and the
  gate fails any narrative that omits it.

What is NOT guaranteed: identical sentences. Wording varies; facts, flags,
citations, and structure do not. That is the achievable definition of a
deterministic output with an LLM writing the prose.

## Files (parallel — nothing live touched)

- `derive/person_v4_manifest.py` — Layer A (manifest) + Layer B (body facts)
- `derive/verify_render.py` — Layer C (gate)
- `config/body_extractors.yaml` — phrase/regex lists (stem-based)
- this skill

Live `/ask`, `person_v3.py`, `person_deepread.py` are unchanged.

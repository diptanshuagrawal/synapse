# Shared-chunk extraction plan

Goal: pull duplicated skill-spec logic into `.claude/shared/*.md` chunks so every
skill renders/behaves consistently and rules live in ONE place. Modeled on the first
extraction, `render-rules.md` (link formats, never-bare-ID, self-summarizing threads,
pre-save check).

## Wiring convention (how each extraction is done)

1. Write the shared chunk as a SUPERSET of every consumer's wording (don't lose a
   skill-specific nuance — keep those local with a pointer).
2. In each consumer, replace the duplicated prose with: `**Read `.claude/shared/<chunk>.md`** — <one-line of what it covers>`, then keep ONLY that skill's specific additions.
3. Never silently change behavior while extracting. If wording diverges between
   skills, pick the canonical version, note the change, and flag any that look like bugs.
4. After each chunk: verify pointers resolve (`grep -rn "shared/<chunk>"`), keep
   diff reviewable, pause if blast radius is large.

## Order of work

Start with ONE (roster-identity) as the proof of pattern, review, then proceed.

### Tier 1 — high reuse, low risk
- [x] **roster-identity.md** — DONE 2026-06-19. Wired standup + ticketize (the only
  full-block restaters; other ~17 are light `canonical` one-liners, left alone).
- [x] **date-range-grammar.md** — DONE 2026-06-19. Wired ask.md (Phase 2) + standup
  (window line). retro/pulse compute their own TS in code — not prose dup, left alone.
- [x] **classify-apply-harness.md** — DONE 2026-06-19. Wired classify / rollup Phase 2 /
  pr-quality / leaves. FIXED: pr-quality 0.6 → 0.7 in BOTH prose AND
  `derive/apply_pr_classes.py::CONFIDENCE_MIN` (prose-only would have mismatched the
  hardcoded gate). Left `apply_verdicts.OWNERSHIP_CONF_THRESHOLD = 0.6` untouched — that's
  a separate ownership-nulling gate, not the classify accept/reject gate.
- [~] **freshness-gate.md** — SKIPPED 2026-06-19. Scanner over-counted (claimed ~11).
  Verified: the ingest-freshness gate (per-source newest-ts → ⚠️ STALE banner →
  don't-render-empty) lives in ONLY standup.md. Other "stale" hits are unrelated concepts
  (stale-thread reconcile, cluster-lifecycle STALE, stale-doc drift, cache rebuild). Single
  consumer = premature abstraction; left in standup.

### Tier 2 — worth it, narrower
- [x] **output-save-conventions.md** — DONE 2026-06-19. Promoted ask.md's version; wired
  ask Phase 5 + pulse Phase 4. EXCEPTIONS (left alone, respecting real divergence):
  standup (writes no md), pr-report (intentionally OVERWRITES same-day + uses
  work-context/management base), retro (stakeholder header style differs).
- [x] **drift-direction-gate.md** — DONE 2026-06-19. Wired doc-sync (canonical owner) +
  doc-sync-sweep Phase 1. doc-sync-digest is resolution-tracking, not a gate consumer.
- [x] **evidence-grounding.md** — DONE 2026-06-19. Wired retro §3a + ask narrative-style
  (deep-read) + standup §8 enrich clause.
- [~] **leave-aware-interpretation.md** — SKIPPED 2026-06-19. Scanner over-counted.
  Verified: the interpretive "rule out leave before calling a decline" rule lives only in
  pulse.md (standup §5 = operational leave-badging, a different concept; dev-style Phase 4
  caveats are scope/sample-size, no leave rule). Single consumer = premature abstraction.

### Tier 3 — marginal (decide later)
- [ ] **python-invocation.md** — standardize `cd work-context` vs `PYTHONPATH` variant. 22 files.
- [ ] **slack-pipeline.md** — upsert/dedup/event-schema/concurrency. slack-* family.

## Explicitly NOT extracting
- help-text one-liner (trivial; each Usage block is skill-specific).
- jira_metrics usage — already code-coupled + correct; consumers cite the module. (Verify
  no skill reimplements it, but no shared chunk needed.)
- retry-until-success cron gate — infrastructure (plists/install-routines), not in specs.

## Progress log
- 2026-06-19: plan written. render-rules.md already live (links/IDs/threads/pre-save).

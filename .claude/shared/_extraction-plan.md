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
- [~] **python-invocation.md** — chunk SKIPPED 2026-06-19 (no dedup benefit — the command
  must be inline anyway; a no-consumer chunk is clutter). BUT the investigation found a real
  BUG: `cd $HOME/work-context` (absent dir) in retro (×2) + dev-style, and fragile relative
  `cd work-context` in pulse. FIXED all 4 → canonical `cd $HOME/context/work-context`.
- [~] **slack-pipeline.md** — SKIPPED 2026-06-19. Scanner over-counted. Verified: candidates
  are single-consumer (run-to-completion, permalink) or thin/code-coupled (channel-config via
  Python). No genuine prose dup; also an active slack code workstream is in flight — don't churn.

### Follow-up (post-tier, deferred candidate)
- [x] **plain-language.md** — DONE 2026-06-19 (in worktree `refactor/plain-language-translation`,
  off origin/main). The no-internal-jargon / translate-to-plain-English rule + pre-save
  grep-check was restated across narrative-style (canonical), standup §9, pulse, doc-sync.
  Extracted the generic rule; each skill keeps its own pipeline's forbidden-token list.
  Wired: standup, pulse (×2), narrative-style (top pointer), doc-sync (meta-labels).
  feature-logic left alone (engineer-facing, different application).
- [x] **code-graph-access.md** — DONE 2026-06-19 (worktree `refactor/code-graph-access`).
  Mirror location + registry resolution + REMOTE-default-branch freshness contract (not
  `~/git` WIP) + unregistered-repo handling, restated across service-brief, pr-from-trd,
  feature-logic, doc-sync, doc-sync-sweep. Wired those 5. trd-build delegates to
  service-brief (left alone). pr-from-trd: chunk = READ source; WRITE target stays ~/git.

## Outcome
8 shared chunks extracted + wired (render-rules, roster-identity, date-range-grammar,
classify-apply-harness, drift-direction-gate, evidence-grounding, output-save-conventions,
plain-language, code-graph-access). 4 candidates correctly SKIPPED as premature abstraction
(freshness-gate, leave-aware, python-invocation chunk, slack-pipeline). 2 real bugs fixed
along the way (pr-quality 0.6→0.7 gate; broken `cd $HOME/work-context` path).

## Explicitly NOT extracting
- help-text one-liner (trivial; each Usage block is skill-specific).
- jira_metrics usage — already code-coupled + correct; consumers cite the module. (Verify
  no skill reimplements it, but no shared chunk needed.)
- retry-until-success cron gate — infrastructure (plists/install-routines), not in specs.

## Progress log
- 2026-06-19: plan written. render-rules.md already live (links/IDs/threads/pre-save).
- 2026-06-19: Tier 1 + 2 done (6 chunks wired across 12 skills). pr-quality 0.7 aligned
  (code + rules source + artifact + boundary test). Reviewed by agent (1 BLOCKER caught:
  generated rules-file was stale at 0.6 — fixed at source). Committed 891b0bd + published to
  main (suite 816 pass, leak scan clean). Slack/dashboard workstream left unstaged (not mine).
- Starting Tier 3.

# PRD — Code-grounded TRD Builder

**What this is:** `/trd-build <prd> <svc>` fuses a PRD with a service's real code surface so every "as-is" / data-model / dependency fact in the TRD is traceable to code, not guessed.

## Problem
Writing a backend TRD at Acme means re-deriving the "as-is" by hand: which endpoints, tables, kafka topics, and call-edges a feature touches.

The `backend-trd-writer` skill nails the 15-section template but (per its own docs) "cannot auto-discover upstream services from code." So §7 (current arch), §8.5 (data model), §8.6 (failure modeling), §10 (dependencies) get written from memory and drift.

We now have deterministic code understanding of service-a-class Go services (`/service-brief` skeleton + code-review-graph) — this closes the gap.

## Approach
Grounding layer + reuse, NOT a new generator. `/trd-build <prd> <svc>`:

1. **Materialize PRD** — local path or Confluence URL (Atlassian MCP).
2. **Deterministic linkage** — `derive/trd/build_context_pack.py`: IDF-weighted token overlap between PRD vocab and skeleton identifiers → ranked, provenance-tagged candidate endpoints/tables/kafka. Zero LLM, zero network.
3. **Blast-radius expansion** — code-review-graph MCP (`semantic_search_nodes`, `query_graph` callers/callees, `get_impact_radius`, `get_affected_flows`) on top candidates → real upstream/downstream + affected flows.
4. **Code-context pack** — `derived/trds/<slug>.context.md`, HARD RULES from `/service-brief` (names verbatim, `(unknown)` never guessed, every fact tagged).
5. **TRD** — hand PRD + pack to `backend-trd-writer`; it runs the 15 sections, the pack fills the code-grounded ones. Output `derived/trds/<slug>.md`.

## Key decisions
- **Reuse** `backend-trd-writer` — do not duplicate the template.
- **Automatic** feature→code linkage (no confirmation gate). Offset wrong-surface risk via: IDF ranking sharpens signal, every code fact carries provenance + linker score, TRD opens with a mandatory mis-link banner.
- **Honesty over completeness** — Go interface dispatch isn't fully resolved by the structural graph; deep callees marked `(unknown)`. "Open gaps" lists PRD requirements with no strong code match (= net-new / greenfield work).

## Scope
- **v1:** Go services with a `/service-brief` skeleton (`service-a`, `service-b`).
- **Out:** Java/Spring; auto-publish to Confluence (manual for now); mobile TRDs (use `mobile-trd-writer`).

## Validated against
`Card Transaction Migration to service-a` (Confluence EXAMPLE_PAGE_ID) × `service-a`.
- Linker surfaced the `ExampleTransactionService.Execute*Transaction` family + `example_master` / `example_transactions` / `example_request` + posting consumers.
- Graph confirmed the handler family + 4 affected flows.
- Draft TRD: `derived/trds/card-txn-migration.md`.

## Files
- `.claude/commands/trd-build.md` — the skill
- `derive/trd/build_context_pack.py` — deterministic IDF linker
- `derived/trds/<slug>.{context.json,context.md,md}` — outputs

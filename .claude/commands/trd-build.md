Generate a code-grounded backend TRD by fusing a PRD with the real code surface
of a Go service. Deterministic linker + code-graph blast radius feed the proven
`backend-trd-writer` 15-section template. Owner-invoked.


## Usage — `/trd-build <prd> <svc>`

If invoked with `help`, `-h`, or `--help`: print this Usage block verbatim and STOP.

**What it does:** Links a PRD to the endpoints / tables / kafka topics it most
likely touches in a service, expands the blast radius via the code-review-graph,
then writes an Acme TRD whose "as-is", data-model, dependency, and
failure-modeling sections are grounded in actual code — not guessed.

**Args:**
- `<prd>` — a local path (`prd/foo.md`, `/tmp/x.md`) OR a Confluence page URL.
- `<svc>` — a registered Go service alias (`service-a`, `service-b`). Required.

**Scope:** Go services only (skeleton-backed). Linkage is FULLY AUTOMATIC — no
confirmation gate — so every code fact in the TRD MUST carry provenance and the
draft MUST open with a mis-link warning (see step 6).

## Steps

**1. Resolve the service + assets**

Confirm the skeleton and brief exist:
- `work-context/derived/services/<svc>.skeleton.json`
- `work-context/derived/services/<svc>.md`

If the skeleton is missing or you want it current, refresh deterministically:

```bash
cd $HOME/context/work-context && \
  bash derive/service_derive/refresh-skeletons.sh
```

If the brief `.md` is missing, run `/service-brief <svc>` first, then resume.

**2. Materialize the PRD as text**

- If `<prd>` is a local path: use it directly.
- If `<prd>` is a Confluence URL: extract the numeric pageId, fetch via the
  Atlassian MCP (`getConfluencePage` with the your-org cloudId), and write the
  page `body` to `/tmp/<slug>.prd.md`. `<slug>` = kebab-case of the page title.

**3. Deterministic linkage (the seed) — no LLM**

```bash
cd $HOME/context/work-context && \
  python3 derive/trd/build_context_pack.py \
    --prd <prd-text-path> --svc <svc> \
    --out derived/trds/<slug>.context.json
```

This writes an IDF-ranked, provenance-tagged candidate surface (endpoints /
tables / kafka consumers+producers), each item carrying its `score` and the
PRD tokens (`matched`) that pulled it in. Read it.

**4. Expand the blast radius — code-review-graph MCP (automatic)**

For the TOP candidate endpoints + tables from step 3, use the graph to turn the
seed into real upstream/downstream context. Prefer graph tools over file reads:
- `semantic_search_nodes` — resolve each candidate rpc/struct to a graph node.
- `query_graph` callers_of / callees_of — direct dependency edges.
- `get_impact_radius` — blast radius of touching that node (feeds §8.6 / §10).
- `get_affected_flows` — execution paths the change rides on.

Keep it bounded: only the top ~5 endpoints and ~5 tables. Record what you find;
do NOT bulk-read the repo.

**5. Assemble the code-context pack**

Write `work-context/derived/trds/<slug>.context.md` — the structured input the
TRD writer consumes. HARD RULES (same as `/service-brief`):
- Copy every name (rpc, http path, table, column, topic, struct, payload)
  VERBATIM from the skeleton / graph. NEVER invent or alter one.
- If a semantic ("why") field can't be inferred with confidence, write
  `(unknown)` — do NOT guess.
- Every code fact carries provenance: `[skeleton]` or `[graph: <tool>]`, plus the
  linker `score` + `matched` tokens for candidate items.

Sections:
```markdown
# Code-context pack — <svc> @ <skeleton_commit>
_PRD: <prd source> · linked <YYYY-MM-DD> · AUTOMATIC linkage, review provenance._

## Surface the feature most likely touches
### Endpoints to extend (from linker, IDF-ranked)
- **<HTTP_METHOD> <http_path>** `<rpc>` (score <n>, matched <tokens>) — <what it does today>. `<req>`→`<resp>`. [skeleton]
### Tables to extend / alter
- **<table>** [<dialect>] (score <n>) — <what it holds today>. [skeleton]
### Kafka
- consumes **<listener>** payload `<payload>` (score <n>) — <when it fires>. [skeleton]
- produces **<struct>** payload `<payload>` — <when emitted>. [skeleton]

## Blast radius (from code-graph)
### Upstream callers — <node>: <callers> [graph: query_graph]
### Downstream callees / dependencies — <node>: <callees> [graph: query_graph]
### Affected flows — <flow names> [graph: get_affected_flows]
### Impact radius — <summary + risk> [graph: get_impact_radius]

## Open gaps (linker found no strong match)
- <PRD requirement with weak/no code hit → likely NET-NEW work>. Mark these so the TRD flags greenfield vs. extension.
```

**6. Write the TRD via backend-trd-writer**

Read the global skill body at
`$HOME/.claude/skills/backend-trd-writer/SKILL.md` and produce
its full 15-section Acme template. Source split:
- **PRD** drives §2 Problem, §3 Scope, §4 Out-of-scope, §6 Goals/Non-goals.
- **Code-context pack** drives §7 Current Architecture, §8 API contracts,
  §8.5 Data Model / Schema Changes, §8.6 Failure modeling, §10 Dependencies —
  using the real endpoints/tables/flows/callers, each with its provenance tag.
- Net-new requirements (from "Open gaps") are written as greenfield additions,
  explicitly labelled as such — never pretend a matching endpoint exists.

**MANDATORY artifacts — never prose-only:**
- **§7 Current Architecture** — a Mermaid `sequenceDiagram` of the closest
  existing flow as-is (e.g. the instant-pay/cash execute path), participants named from
  the real handler family + tables/consumers in the context pack.
- **§8.5 Data Model** — real **DDL** (`CREATE TABLE` / `ALTER TABLE`) for every
  new/changed table. Base column names + types on the closest analog table's
  ACTUAL columns from the skeleton (read `derived/services/<svc>.skeleton.json`
  for the analog's `columns`: name, type, constraints) — do NOT invent types.
  Card/feature-specific columns the analog lacks are added explicitly and
  labelled NET-NEW. State the dialect (mssql / postgresql) per table.
- **§8.6 Business Logic** — a Mermaid `sequenceDiagram` for the proposed flow
  (happy path) AND state the failure branches (the §8.6 failure table stays).

The TRD MUST begin with this banner under the header:

> ⚠️ Code linkage is automatic and unverified. The "as-is" / data-model /
> dependency sections were derived from `<svc>@<commit>` via token-match +
> code-graph. Confirm the cited endpoints/tables/flows are the right surface
> before relying on this draft.

Write to `work-context/derived/trds/<slug>.md`.

**7. Print summary**

- prd source + svc + skeleton commit
- linker hits per category (endpoints / tables / consumers / producers)
- graph expansions run (nodes resolved, flows found)
- count of `(unknown)` fields and count of "Open gaps" (net-new) requirements
- the TRD path written
- one-line mis-link caution

## Permission posture (if run unattended)
File reads/writes under `$HOME/context/**` and `/tmp/**`,
Bash for `python3 *` / `bash derive/* *` / `git *`, and the Atlassian +
code-review-graph MCP tools are pre-approved (owner has bypassPermissions).
Do NOT pause for prompts; on an unexpected gate, abort and report.

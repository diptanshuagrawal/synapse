Generate a service-context MD for a Go service: refresh the deterministic skeleton, then fill the semantic ("why") fields in chat and write `derived/services/<svc>.md`.

## Usage — `/service-brief <svc>`

If invoked with `help`, `-h`, or `--help`: print this Usage block verbatim and STOP.

**What it does:** Runs the deterministic Go extractor to (re)build the service skeleton JSON, then YOU (the chat LLM) read it and write a human + embedding-ready service brief. Scripts never call an LLM — all synthesis happens here, owner-invoked.

**Arg:** `<svc>` — a registered Go service alias (e.g. `service-a`, `service-b`). Required.

**Scope:** Go services only (v1). Java/Spring is out of scope.

## Steps

**1. Resolve the mirror path** (per `.claude/shared/code-graph-access.md` — registry
resolution, mirror-not-`~/git`, freshness contract, unregistered-repo handling)

```bash
python3 - "$ARGUMENTS" <<'PY'
import json,sys,pathlib
reg=json.load(open(pathlib.Path.home()/".code-review-graph/registry.json"))
svc=sys.argv[1].strip()
hit=next((r for r in reg.get("repos",reg) if r.get("alias")==svc),None) if isinstance(reg,dict) else None
print(hit["path"] if hit else "NOT_FOUND")
PY
```

If `NOT_FOUND`: tell the user the alias isn't registered; list available aliases from `~/.code-review-graph/registry.json`. Stop.

**2. Refresh the deterministic skeleton**

```bash
cd $HOME/context/work-context && \
python3 derive/service_derive/go_extractor.py --repo <mirror-path> --svc <svc>
```

This writes `derived/services/<svc>.skeleton.json` (endpoints, tables, kafka). Zero LLM, zero network.

**3. Read the skeleton**

Read `$HOME/context/work-context/derived/services/<svc>.skeleton.json`.

**4. Fill semantics and write the brief — HARD RULES**

You add MEANING only. The skeleton owns the FACTS.

- NEVER invent or alter a name, path, type, table, column, topic, or config key. Copy them verbatim from the skeleton.
- If a semantic field can't be inferred with confidence, write `(unknown)` — do NOT guess.
- Each semantic line ≤ 1 sentence, present tense, action-first. No filler.
- You MAY read a handler/listener source file from the mirror to disambiguate a name, but prefer inference from names to bound cost. Do not bulk-read the repo.
- Endpoints/tables/kafka counts in the brief MUST equal the skeleton counts.

**5. Output format** — write to `$HOME/context/work-context/derived/services/<svc>.md`:

```markdown
# <svc> — service context

_Skeleton @ <commit> · semantic pass <YYYY-MM-DD>. Facts from code; semantics inferred._

## Responsibility
<1–2 lines: what this service owns, inferred from endpoints + tables + topics.>

## API endpoints (<N>)
### <ServiceName>
- **<HTTP_METHOD> <http_path>** `<rpc>` — <purpose>. `<request>` → `<response>`.
<repeat per endpoint, grouped by gRPC service; if http_path is null, show the rpc only.>

## Data model (<N> tables)
- **<table>** [<dialect>] — <what it holds / access pattern>. key cols: <col(type), …up to ~6>.
<repeat per table.>

## Kafka
### Consumes (<N>)
- **<listener_name or struct>** — payload `<payload_type or (unknown)>` — <when/why it fires>.
### Produces (<N>)
- **<struct>** — payload `<payload_type or (unknown)>` — <when emitted>.

## Downstream
_(deferred — populated by code-graph call-edge enrichment.)_

## Glossary
<5–15 domain terms that appear in THIS service's endpoints/tables/topics, each
one line: **Term** — plain-English definition. Infer from names; mark
`(unknown)` if a term's meaning isn't clear. Improves cross-source retrieval.>

## Provenance notes
<carry the skeleton `notes` array verbatim as bullets.>
```

Note on fields/validation: do NOT hand-type request/response fields or proto
validation rules into the endpoint lines. `ingest_briefs.py` injects them into
the embedded chunks automatically from the skeleton (`request_fields` /
`response_fields`, including `buf.validate` rules). Keep the `.md` endpoint
lines at the purpose level; the vectors get the field detail for free.

**5b. Persist the brief as DB rows (canonical output)**

The `.md` is the human-readable artifact; the canonical, embeddable output is
`source='service'` rows in `events.db`. Ingest them:

```bash
cd $HOME/context/work-context && \
python3 derive/service_derive/ingest_briefs.py --svc <svc>
```

This chunks the brief per section (responsibility / per gRPC service / data
model / kafka) and upserts idempotent `service:<svc>#<section>` rows. No LLM.

**6. Print summary**

Show:
- service + commit
- counts written (endpoints / tables / consumers / producers) and confirm they match the skeleton
- count of `(unknown)` semantic fields left
- file path written

**7. Remind user**

```
✓ service brief written → derived/services/<svc>.md
✓ ingested as source='service' rows in events.db

Next: /refresh-embeddings to embed the new service:<svc> subjects (vectors only; clustering skips source='service').
```

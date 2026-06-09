# Service Derivative Schema (proposal)

Goal: give the context repo broad domain understanding by adding one new
source — per-service MD derivatives generated from code — then feeding them
through the existing embed + topic_brief pipeline.

Borrowed from the Context Layer TRD's "code → MD derivative" idea, but built
on the repo's existing `code-review-graph` mirrors instead of `graphify`.

One MD file per service. Stored alongside other ingested sources, embedded,
briefed. No LightRAG, no second graph store, no router.

---

## Generation split

Two passes per service:

1. **Deterministic pass** — pull structure straight from the code-graph /
   AST + repo config (Argo/Bifrost). Cheap, reproducible, no tokens. This is
   the spine of the file and the part you can regenerate for free.

2. **LLM pass** — fill only the *meaning* fields the AST can't know. One
   bounded LLM call per service, over the deterministic skeleton (not raw
   code), so token cost stays small and predictable.

Rule of thumb: structure = deterministic, semantics = LLM.

---

## Output schema (language-agnostic)

The MD file has the same fields regardless of service language. Only the
*extractor* differs. Fields:

### Header
- service name
- repo URL + commit SHA
- one-line responsibility (LLM)
- owning team

### API endpoints (per endpoint)
- transport + method + path/RPC name
- handler function (call-graph node)
- request type (name + fields)
- response type (name + fields)
- what the endpoint does (LLM)
- auth / preconditions

### DB interaction
- tables (name + columns + types)
- relationships (FK)
- which endpoints touch which tables (call-graph: handler → repo → table)
- access-pattern summary (LLM)

### Kafka
- consumed topics
- produced topics
- payload type + fields
- payload meaning / when emitted (LLM)

### Downstream calls
- outbound call sites
- target service
- resilience config (circuit-breaker / retry / timeout)
- latency thresholds
- why the call is made (LLM)

Same rule everywhere: structure = deterministic, semantics = LLM.

---

## Extractor mappings

There is **no single universal extractor**. Acme has Go services (service-a,
service-c, service-b) and Java/Spring services. Each language fills the same
output fields from different sources. Validated against service-a (Go) on
2026-06-08.

**Scope: Go + Java/Play.** Registry has service-a + service-b (Go, gRPC)
and service-c (Java, Play Framework). service-c is NOT Spring — it has no REST/JPA/Kafka
annotations; routes live in `conf/routes`. See the Java/Play section below.

### Go / gRPC (validated on service-a)

**Phase 1 BUILT** — `derive/service_derive/go_extractor.py`. Run on service-a:
82 endpoints (all with HTTP path + req/resp), 44 tables (cols/types/comments),
14/14 Kafka listeners mapped to listener-name + config-key + payload, 4
producers. Output: `derived/services/<svc>.skeleton.json`. No LLM, no network.
Downstream gRPC clients + call-edges + topic strings still TODO (see below).


| Output field | Source | Pass | Confidence |
| --- | --- | --- | --- |
| endpoints | gRPC service defs in `.proto` (118 files) + handler structs; flows surface `GetSubscription`, `ExecuteDebitLeg` etc | deterministic | ✓ confirmed |
| request/response types | proto message defs / Go structs via AST | deterministic | ✓ confirmed |
| tables + columns | `.sql` migrations (57 files) + `internal/infrastructure/postgres/repositories/*` | deterministic | ✓ confirmed |
| endpoint → table | call-graph handler → repository → query | deterministic | ✓ layering clean (DDD) |
| consumed topics | `pkg/kafka/consumer.go`, `cmd/consumer/consumer.go` | deterministic | ✓ confirmed |
| produced topics | `*_publisher.go` (e.g. `kafka_publisher.go` → `Publish`) | deterministic | ✓ confirmed |
| downstream gRPC calls | proto-generated client stubs | deterministic | ⚠ needs proto-aware locator; generic "Client" search hit Redis, not service stubs |
| target service | Argo/Bifrost config → service map | deterministic (config) | not yet checked |
| resilience config | Go middleware / config (no annotations) | deterministic | not yet checked |
| all "why"/semantic fields | LLM over skeleton | LLM | n/a |

### Java / Play — `play_extractor.py` (validated on service-c)

service-c (service-c) is a Java **Play Framework** monolith — NOT Spring.
No `@RestController`/`@Entity`/`@KafkaListener`. Endpoints live in
`conf/routes` text files. The original "Java/Spring annotations" assumption
was wrong; a real Spring service would need its own extractor.

| Output field | Source | Pass | Confidence |
| --- | --- | --- | --- |
| endpoints | `conf/*routes` lines (`METHOD /path controllers.Class.method(params)`) | deterministic | ✓ confirmed (4891) |
| service grouping | controller class (`AccessListController`) | deterministic | ✓ 289 controllers |
| request | route's typed params (e.g. `id:Long`) | deterministic | ✓ |
| response | none in routes → `(unknown)` | n/a | by design |
| tables + columns | `.sql` migrations via shared SQL parser (T-SQL `[dbo].[t]`, dedup, skip `#temp`) | deterministic | ✓ 1321 tables |
| kafka | best-effort graph discovery; service-c has no annotation-based consumers | deterministic | ⚠ ~0 (gaps accepted) |
| downstream / resilience | deferred (code-graph enrichment) | — | — |
| all "why"/semantic fields | LLM | LLM | n/a |

Run on service-c @ 86c26ca6: 4891 endpoints, 1321 tables (deduped), 0 kafka.

### Generalization finding (service-a + service-b)

Ran the extractor on a second Go service (service-b). Result:

- **Endpoints (.proto) and tables (.sql) port for free.** Both are
  language/framework standards. service-b: 23 endpoints + 9 tables clean,
  zero parser changes.
- **Kafka wiring is per-service bespoke — regex does not generalize.**
  service-a uses `enum.ListenerNameX.String(): { kafka.NewListener(NewXListener) }`.
  service-b uses `addEntry(ConsumerNameX, listeners.XListener)` in a
  separate `consumer_registry.go`, payloads via a generic wrapper
  (`ProcessBatch` + `KafkaListenerInterface`), and names don't even align
  (`ReconciliationEventListener` vs struct `ReconciliationListener`).

Decision: do NOT keep adding per-convention Kafka regex. Discover Kafka structs
from the **code-graph** instead. The service-a regex path stays as a fallback.

**DONE — graph-backed Kafka discovery (`discover_kafka_via_graph`).**

Caveat learned on inspection: the Go code-graph does NOT model interface
satisfaction (`INHERITS == 0`) and stores only the *receiver* in a method's
`params` (no arg types). So "find implementers of `ListenerService`" is
impossible. Instead discovery combines two signals that survive in the graph:

1. name — `Class` name contains `Listener` / `Producer` / `Publisher`
2. behaviour — the struct has a consume (`Process`/`ProcessBatch`/`Handle`/
   `Consume`) or produce (`Produce`/`Publish`/`Send`/`Emit`) method

Plus a suffix filter dropping interfaces/config/DTOs/collections
(`*Interface`, `*Service`, `*Config`, `*Request`, `*Wrapper`, `*Name`, plural).

Results vs the old file-glob:

- service-a: 14 listeners (14/14 wired via registry), 4 producers — unchanged.
- service-b: 3 listeners (glob found 2 — gained `TransactionEventListener`)
  and 3 producers (glob found 0). Location/wiring independent.

Remaining best-effort (per-convention, filled by Phase 2 LLM pass):
- listener-name + consumer-config-key: only the service-a registry pattern is
  parsed; service-b's `addEntry(...)` registry → 0/3 (acceptable).
- payload type: extracted when the listener body does
  `json.Unmarshal(msg.Value, &x)`; missed under generic-wrapper patterns.

If no `.code-review-graph/graph.db` exists, falls back to file-glob discovery.

## Status & open threads (2026-06-08)

**Done — pipeline live end to end.**
- Phase 1 extraction: Go/gRPC (`go_extractor.py`) + Java/Play (`play_extractor.py`),
  driven by `build_skeletons.py` over `config/services.yaml` (drift-gated).
- Endpoint enrichment: request/response fields + `buf.validate` rules pulled
  from proto into the skeleton (`request_fields`/`response_fields`).
- Phase 2 briefs: service-a + service-b, with per-service endpoint chunks,
  data model, kafka, and a hand-authored Glossary.
- Phase 3: briefs ingested as `source='service'` rows (`ingest_briefs.py`,
  fields/rules auto-injected at ingest), embedded (26 vectors live).
  Clustering excludes `source='service'`. `/service-brief` skill is durable.

**Validated retrieval (2026-06-08).** Service vectors are queryable and land in
the same semantic space as Jira/Confluence/PR knowledge — e.g. the Payments service
chunk's nearest neighbours are payments PRs, and "create + fund a savings pot"
surfaces the Payments PRD. This cross-source link is the TRD-generation multiplier.

**Open threads (not blocking the core use case):**
1. **Chunk granularity.** Endpoint chunks are per-gRPC-service (all of a
   service's endpoints in one chunk). Good for broad context-gathering (TRD
   generation); dilutes narrow per-endpoint lookups (the service chunk ranked
   below focused PRs/PRD for a narrow query). Lever: split to per-endpoint
   chunks if precise endpoint search becomes important.
2. **Bucket B metadata** (NOT embedded — fetched on demand for code-writing):
   handler `file:line`, repo file per table, call-edges (endpoint→repo→table,
   service→service gRPC), test locations, CODEOWNERS. Needed to *raise PRs*.
3. **service-c (Java/Play)** — extraction only (`brief: false`). Per-endpoint briefing
   of 4891 endpoints is impractical; needs a per-module strategy before briefing.
4. **Daily routine** — not built. Graph cron (`run-codegraph.sh`, 18:00) exists;
   a routine gated on `last_codegraph_success.date` would run build_skeletons +
   /service-brief for drifted Go services. refresh-embeddings stays owner-invoked.
5. **Clustering tail** — the 2026-06-08 refresh was killed during HDBSCAN (after
   embeds persisted); `topic_brief` was NOT mutated (apply never ran). Re-run
   `/refresh-embeddings apply` any time to settle the activity clustering;
   `embed_required` will be ~0.
6. **project ↔ service link** — wire each service to its `projects.yaml` slug to
   pull the service's Jira/Confluence/PR history into TRD/PR generation.

### Adding a new language

Implement one extractor that emits the output schema above. The embed +
brief + `/ask` path downstream is language-agnostic.

---

## What's deterministic vs LLM — summary

Deterministic (≈70% of the file, free to regenerate):
- every name, path, type, topic, table, column, config value
- every edge: endpoint → table, endpoint → downstream, producer → topic

LLM (≈30%, one bounded call per service):
- service responsibility line
- per-endpoint purpose
- payload / access-pattern semantics
- "why" on downstream calls

---

## Why this fits the repo

- Reuses `code-review-graph` mirrors already at `~/.code-review-graph/repos`.
  No graphify dependency.
- Output is just MD → goes through the existing embed + topic_brief +
  `/ask` path. No new query layer.
- LLM cost is bounded: one call per service over a skeleton, not per chunk
  over raw code. The new token cost is the only line crossed vs the repo's
  current "scripts never call the LLM API" rule — flagged, not hidden.
- Deterministic spine means re-runs after code changes are cheap and the
  diff is auditable.

---

## Open questions (for grilling, not yet decided)

1. ~~Spring annotation extraction~~ — superseded. Services are multi-language.
   Go validated on service-a (endpoints/Kafka/DB clean). Still TODO: validate a
   real Java/Spring service the same way before trusting that extractor.
2. Downstream gRPC clients: a generic node search did not isolate service
   stubs (hit Redis client). Need a proto-aware locator — map proto `service`
   defs to their generated Go client stubs.
3. Bifrost/Argo → service-name resolution: is there a single config that maps
   client stub → target service, or is this per-repo?
4. Where do these MD files live — new `events.db` source rows, or a
   `derived/services/*.md` tree that the embed step picks up?
5. Refresh trigger: on mirror update (daily reset --hard) or on demand?
6. Does the LLM "why" pass run in chat (owner-invoked, like classify) or as a
   script step — and does that break the chat-only-LLM policy?
7. Per-language extractors share one output schema — where do they live
   (new `derive/service_derive/<lang>.py` modules)?

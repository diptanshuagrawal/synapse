# /ask · feature_logic — dispatch chunk

Loaded by the `/ask` router (Phase 3). Self-contained — this intent has its own
output voice (WIKI / CODE below); the person-narrative rules do NOT apply.
URL conventions + save rules are in the router.

**This intent answers "how does X work / what is Y / where is Z computed" from ALL
SOURCES — service briefs, jira, confluence, slack, AND code — never code alone.**
The service brief is the *map* (which service owns the concept, what tables, what
the terms mean); code tells you *how it currently runs*; the TRD/PRD on Confluence
tells you *what it's meant to do and why*; jira/slack tell you *what changed and
what broke*. A code-only answer
is partial and can be actively wrong — it can describe the wrong subsystem, or miss
that a doc-specified behaviour differs from what shipped. (Validated in practice: a
code-only "how does withholding work" answer described deposit-interest withholding and claimed it was
*async* to the transaction; the cash-withholding TRD revealed a *second* withholding
system — Section NNN cash-withdrawal withholding — that is deliberately *synchronous*. The
docs corrected both the scope and the core claim.)

**Step 1 — GATHER ALL SOURCES (mandatory, in parallel).** Fire these together:

- **Service briefs (the ROUTING layer — read FIRST).** Per-service plain-English
  maps built by `/service-brief` (deterministic Go extractor + chat semantics),
  stored as `source='service'` rows in `events.db` — embedded for retrieval but
  NOT clustered, so they never show up as a topic cluster. They answer "*which
  service owns this concept, what tables hold it, what do the domain words mean*"
  BEFORE you touch the code graph. A raw concept word ("posting", "charges") often
  buries the real subsystem under recurring-alert noise in the events search — the
  brief cuts straight to the owning service. Use them as the map, then drill.
  - List what exists: `SELECT DISTINCT subject FROM events WHERE source='service'`
    (via `from ingest.common import get_db`). Subjects are
    `service:<svc>#responsibility | #glossary | #data-model | #endpoints/<svc> | #kafka`.
  - Pull the relevant sections for the concept:
    `SELECT title, body FROM events WHERE source='service' AND subject LIKE 'service:<svc>#%'`.
    Always read `#responsibility` + `#glossary` + `#data-model` for the candidate
    service(s); add the matching `#endpoints/...` section.
  - **The `#glossary` section seeds WIKI §Key terms directly** — it is already
    plain-English domain definitions, per service.
  - Coverage is partial (Go services only; v1 = `service-b`, `service-a`). If no
    brief exists for the owning service (notably **service-c** has none yet), say so and
    fall back to the code graph + Confluence — don't pretend the map is complete.
  - A brief is a MAP, not the territory: it's fresher than a TRD (derived from
    current code) but COARSER than the graph. It tells you *where* and *what owns
    what*; it does NOT prove behaviour. Verify the actual computation in code
    (Step 2). Briefs also stop at the service edge — a flow that hands off to a
    downstream service's Kafka consumer won't be traced by the source service's
    brief; follow it into the consumer's brief/code (validated in practice: the
    posting brief mapped `staging → posted` but the final ledger commit lives in a
    downstream consumer the brief didn't trace).
- **Events pipeline** — `ask_engine.py search --query "<concept>" --k 25` from
  `work-context`. Returns clusters spanning jira/CMR/confluence/slack/PR. Note the
  cluster labels (they reveal adjacent subsystems — e.g. a "withholding" search surfaces
  both interest-withholding and cash-withdrawal-withholding clusters). Pull member subjects to cite
  and to read bodies. **Service-brief hits land in the `<unaffiliated>` bucket**
  (`cluster_id=None`, `topic_brief=None`) since briefs aren't clustered — treat
  those rows as authoritative service-structure context, not as a weak cluster;
  better to pull the brief directly via the SQL above than rely on this bucket.
  For a pure domain word, expect this search to surface mostly operational/alert
  noise (e.g. "posting" → recurring `CLEARING_OUTWARD_POSTING` oncall alerts) —
  that's the *operational* view, useful for §Why/ops notes, not the logic.
- **Confluence docs (direct)** — Atlassian MCP. `searchConfluenceUsingCql` with
  `text ~ "<concept>" AND type = page ORDER BY lastmodified DESC` (cloudId
  `YOUR_CONFLUENCE_CLOUD_ID`, site `your-org.atlassian.net`). Hunt for
  TRD / PRD / "API Contract" / design pages by title. `getConfluencePage` (markdown)
  for the full body of the 1-2 most relevant. **The TRD/PRD is the primary source
  for "what it is" + "why".**
- **Code graph** — `mcp__code-review-graph__semantic_search_nodes_tool` (keyword/
  hybrid; concept terms — single words land better than phrases) over `service-c` then
  `service-a`. `query_graph_tool` for callers/callees once a symbol is known.

If a source returns nothing, say so — don't silently answer from the others alone.
If code spans an unregistered repo (`deposits-orch` on disk, not registered), flag it.

**Step 2 — READ the primaries.** Read the TRD/PRD body (Confluence) AND the 2-4
functions that carry the logic (`get_minimal_context` / direct Read + grep).
**Never infer behaviour from a name or a doc title — read the actual body / code.**

**Step 3 — RECONCILE across sources, with a PRECEDENCE RULE.** Sources answer
different questions and have different trust for different claims. When they
conflict, resolve by *what kind of claim* it is:

- **"What does it do NOW / current behaviour / does X happen" → CODE WINS.** The
  TRD/PRD states *intent*, and intent goes stale: docs are written before code, get
  partially implemented, or drift as the code changes. Never assert current
  behaviour from a doc alone — confirm it in code. If the doc says one thing and the
  code does another, **the code is the true state**; say so plainly and note the doc
  is out of date (or describes an unshipped/changed design).
  - **Freshness scope:** the code graph is built from dedicated mirror clones pinned
    to each repo's REMOTE default branch (`main` for service-a, `master` for service-c),
    refreshed daily. So "code wins" means *merged* code wins — the graph does NOT see
    un-pushed or uncommitted local work or unmerged feature branches. If an answer
    seems to contradict in-progress local work, say it reflects merged `main`/`master`,
    not local WIP.
- **"Why / what was intended / what's the tradeoff" → TRD/PRD + jira/slack WIN.**
  Code can't tell you *why*; the design doc and the discussion can. Rationale claims
  lean on docs. If NO design doc surfaces (common for plumbing like posting), the
  "why" can only be *inferred* from code structure + inline comments — label it as
  inferred, don't present it as documented intent.
- **"Where does it live / which service / what tables / what's this term" →
  SERVICE BRIEF wins as the map**, then confirm in code. The brief is derived from
  current code so its structure claims are reliable, but it is COARSER than the
  graph and never proves behaviour — use it to locate, use code to verify. When the
  brief and code disagree on structure, the brief is stale; re-run `/service-brief`.
- **Distinguish stale-doc from not-yet-built.** If the code lacks something the TRD
  describes, it's either (a) shipped-differently → code is truth, doc is stale; or
  (b) not-built-yet → the feature is aspirational. Decide which using jira/slack
  status ("TRD in progress", "rollout branch-by-branch", open tickets) — and SAY
  which case it is. Absence in code ≠ "doc is wrong" without checking the status.
- **Verify behaviour, don't just confirm names exist.** Finding a function/table the
  doc names proves the *structure* exists, NOT that it *behaves* as the doc claims.
  For any load-bearing behavioural claim (sync vs async, idempotent, free-tier
  logic, the actual formula), read the function body — don't infer behaviour from a
  matching name or trust the doc's sequence diagram.
- **Multiple subsystems under one term:** if the search surfaced two features sharing
  a name (interest-withholding vs cash-withholding), disambiguate up front and answer the one asked.

In the wiki answer's §Sources, state the verdict for any conflict: "code is the
current truth; the TRD still says X (out of date)" or "doc describes Y, not yet in
code — planned, see <ticket>".

**Output voice — WIKI by default.** `feature_logic` answers in plain-language,
encyclopedia style — the reader is someone new to this feature (a new joiner, a PM,
an engineer from another team), NOT the author. They should understand the feature
WITHOUT reading code. Code is the *footnote that proves the explanation*, never the
headline. This is the inverse of every other intent's "show the artefact" rule:
here, lead with the concept, bury the `file:line`.

The owner may signal the engineer-facing variant with `/ask code <question>`,
"show me the code", "where exactly", "trace the flow". Then flip to the CODE shape
below. Default (any "how does X work / what is Y / explain Z") = WIKI shape.

**WIKI shape (default — Wikipedia-style):**
1. **What it is** — 1 short paragraph, pure plain English. No code, no file names,
   no jargon. A smart non-expert should get it in one read. Lead with the human
   purpose ("withholding is the tax the bank withholds on the interest it pays you").
2. **How it works** — the flow told as a STORY, in order, plain English. "When the
   bank runs its interest cycle, it credits interest, then withholds a slice as tax,
   then records both." Number the steps. NO code, NO SQL, NO `file:line` here.
3. **Key terms** — a 3-6 item glossary of any domain acronym/term used (withholding, ABB,
   EOD/BOD, GL, CMR). One plain sentence each. This is what makes it newcomer-safe.
   **Seed this from the service brief's `#glossary` section** when one exists — it's
   already per-service plain-English definitions. Skip a term if it's already obvious
   from context; never pad.
4. **Why it's built this way** — the design rationale / tradeoff, if discoverable.
   Pull from the jira/CMR/confluence/slack discussion (one `ask_engine search`).
   Often the interesting part ("they moved withholding off the live transaction because it
   was slowing the app"). Skip if no rationale surfaces — don't invent one. If the
   rationale is only *inferable from code* (no design doc surfaced), say so — "read
   off the code's structure, not a design doc" — don't dress inference as intent.
5. **Sources & under the hood** (LAST, the footnote) — the evidence trail. Lead with
   the authoritative docs (TRD/PRD Confluence links + key jira/CMR tickets); if none
   exist, SAY so ("no posting TRD surfaced — code is the only authority"). Next give
   the **service map** (the brief's responsibility + table chain, e.g.
   `order_request → transaction_staging → transaction`). THEN the code: `file:line`
   citations, the key SQL/formula as a short code block, the entry-point symbol.
   Note any doc-vs-code drift found in Step 3, and any service-edge boundary the
   brief didn't cross (e.g. "the final ledger commit is in a downstream consumer").
   This section is for the reader who wants to verify or go deeper; everything above
   stands on its own without it.

Hard rules for WIKI voice:
- No tool/code jargon above §5. No "stored proc", "repository", "JDBC", "ResultSet",
  "cluster" in the explanation — translate ("a database routine", "the code that
  reads the table"). Acronyms get defined in §Key terms before first use.
- Short sentences. Concrete nouns. Analogies where they help a newcomer.
- Honest about boundaries: if the rate/logic lives in a DB proc or an unregistered
  repo, SAY so in plain English ("the exact tax rate is set in a database routine we
  don't have indexed here"), don't hand-wave.
- If the question is broad ("how does the charge system work"), it's fine to answer
  at the feature/concept level and offer to drill into a sub-part — don't dump every
  file.

**CODE shape (only when owner asks for code / "where exactly" / "trace"):**
1. **TL;DR** — 3-5 bullets: what the logic does, where it lives (file + symbol),
   key inputs/outputs.
2. **How it works** — numbered prose walk of the flow: entry point → each transform
   → output. Cite `file_path:line` (clickable) at each step. Show the 1-2 lines that
   carry the actual computation as a short code block.
3. **Where it's wired** — callers + callees / dependencies, from `query_graph`.
4. **Decision context** (if discussion found) — thread/ticket/doc as inline links.

Cite code with `repo/path/to/file.ext:line` (link to the file when a URL convention
exists; otherwise plain path is fine — these are local repos). NEVER fabricate a
line number — if the read didn't return it, cite the symbol name only.

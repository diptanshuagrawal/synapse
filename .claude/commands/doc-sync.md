On-demand doc-drift detector. Diffs a single TRD/PRD on Confluence against the
current truth — code (graph + migrations + source) and recent jira/slack decisions —
and proposes the edits needed to bring the doc back in sync. PROPOSE-ONLY: never
edits the live Confluence page. Owner-invoked.

## Usage — `/doc-sync <doc>`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run anything.

**What it does:** On-demand doc-drift detector — diffs a single TRD/PRD on Confluence against current code + recent jira/slack decisions and proposes sync edits. Propose-only; never edits the live page.

**Usage:** `/doc-sync <doc>`
- `doc` (required) — Confluence page id, tiny-link id, title substring, full wiki URL, or a free-text feature name from memory.

`$ARGUMENTS` is the doc to check — OR a feature name from memory. Accepts a
Confluence page id, tiny-link id, title substring, full wiki URL, or a free-text
**feature/concept** ("instant-pay atm", "lien unlien", "withholding", "service-b"). Examples:

```
/doc-sync EXAMPLE_PAGE_ID
/doc-sync CASH withholding Charges TRD
/doc-sync https://your-org.atlassian.net/wiki/pages/EXAMPLE_PAGE_ID
/doc-sync instant-pay atm   # feature name — resolver finds the TRD/PRD
/doc-sync withholding       # ambiguous — resolver lists options + asks
```

This is the operational sibling of `/ask`'s `feature_logic` intent. `feature_logic`
*answers* "how does X work" by reconciling doc vs code; `/doc-sync` *turns that
reconciliation into a punch-list of doc edits*. It reuses the same all-source gather
and the same code-wins precedence rule. Read `.claude/commands/ask.md` §feature_logic
before running — the gathering + reconcile mechanics are defined there; this skill
adds the direction gate and the propose-only output.

## Phase 1 — Resolve the input to a doc (feature-name aware)

First classify the input:

- **Page id / tiny-link / URL** → fetch directly via `getConfluencePage`
  (`contentFormat: markdown`, cloudId `YOUR_CONFLUENCE_CLOUD_ID`). Done.
- **Anything else** (title substring OR a free-text feature/concept name) → run the
  RESOLVER below. Do NOT assume the text is an exact title.

**Resolver (concept → candidate docs):**

1. Search Confluence two ways and pool the hits:
   - `searchConfluenceUsingCql` `title ~ "<x>" AND type = page`
   - `searchConfluenceUsingCql` `text ~ "<x>" AND type = page ORDER BY lastmodified DESC`
2. Optionally corroborate with the events pipeline — `ask_engine.py search --query "<x>"`
   from `work-context` — to catch the confluence pages the team actually references.
3. Rank candidates: prefer titles containing `TRD` / `PRD` / `Tech Spec`; then most
   recently updated; then design-doc parents over stray pages. Drop `[Deprecated]`
   to the bottom (but surface it — a live deprecated doc is itself a signal).
4. Decide:
   - **Exactly one strong match** → proceed. State which doc you picked and why
     ("resolved 'instant-pay atm' → TRD — instant-pay ATM Charges, page EXAMPLE_PAGE_ID").
   - **Several plausible** (e.g. "withholding" → CASH withholding Charges TRD / service-a withholding Revamp /
     withholding touch-points) → print a short numbered pick-list (title · status ·
     last-updated · url) and ASK which. **Stop — never guess.**
   - **Input is a repo/service, not a feature** (e.g. "service-b") → there is no
     single doc. List the TRD/PRDs that touch that service (title search + events
     pipeline scoped to it) and ask which one to check.
   - **Zero matches** → say so plainly, show the closest titles, suggest a narrower
     phrase. Don't fabricate a target.

Resolver output is always either (a) one confirmed page to check, or (b) a pick-list
+ a question. Only proceed to Phase 2 once a single page is locked.

Extract from the page:
- **Status** — the header/field value (`Draft` / `In Progress` / `In Review` /
  `Approved` / `Done` / `Deprecated`). This drives the DIRECTION GATE (Phase 4).
  If absent, treat as unknown and infer direction from code presence + linked jira.
- **last-updated timestamp** (`version.createdAt`) — the cutoff for "what changed since".
- **Repo** — TRDs often state `Repo: service-a`. Use it to target the right code graph.
- **linked PRD / tickets** — for decision-drift + direction disambiguation.

## Phase 2 — Identify the feature + target repo

Name the feature/domain the doc covers. Map to a registered code graph:
- `service-a` → `$HOME/.code-review-graph/repos/service-a`
- `service-c` (service-c) → `$HOME/.code-review-graph/repos/service-c`

Note (per the code-graph mirror memory): the graph reflects REMOTE default branch
(merged code), refreshed daily — not local WIP. So a "missing in code" finding means
"not merged," which is exactly what the direction gate needs.

If the doc's feature spans an unregistered repo (`deposits-orch`), say so — flag those
sections as "not checkable here," don't guess.

## Phase 3 — Gather current truth (all four sources, in parallel)

Same gather as `feature_logic` Step 1, plus migrations:

- **Code graph** — `semantic_search_nodes` / `query_graph` over the target repo for
  the doc's named symbols, classes, flows.
- **Migrations / DDL** — `grep` the repo's migration dir (e.g. service-a
  `migration/postgresql_sql/*.sql`) for each table the doc's schema section names.
  This is the GROUND TRUTH for schema checks.
- **DTOs / structs** — the `bo/` / domain structs for those tables (how code reads them).
- **jira/slack/confluence** — `ask_engine.py search --query "<feature>"` from
  `work-context`, to catch decisions made AFTER the doc's last-updated date.

## Phase 4 — Run the five drift checks + the DIRECTION GATE

For each doc surface, diff against code/decisions. Five checks, roughly by precision:

1. **Schema drift** (highest precision). Doc's column tables vs the migration DDL
   (primary) + DTO struct fields (secondary). Detect: added column, removed column,
   renamed, type change, constraint/index mismatch, renamed/replaced table.
   - Read the migration body — never infer a column set from a struct name.
2. **LLD drift**. Doc-named enums / class paths / method signatures vs the graph's
   nodes + signatures. (e.g. "add OrderTypeLien to order_type.go" → is it there?)
3. **Sequence drift**. Diagram steps (participant → message) vs the graph's `flows` +
   `CALLS` edges. Detect: documented step absent in code, code call the diagram omits,
   reordered control flow. **First you must GET THE DIAGRAM SOURCE** — the team's docs
   rarely use inline mermaid; most are ZenUML macros (source hidden) or PNGs. Resolution
   order:
   - **Inline mermaid** (```mermaid fence / ADF `codeBlock` lang=mermaid) → use directly.
   - **ZenUML "Diagram as Code Lite" macro** → source is in Forge custom-content, NOT the
     page API (a plain `getConfluencePage`/`fetch` on the id 404s). Extract it via the
     **logged-in work browser** (Claude-in-Chrome):
     1. Fetch the page ADF (`getConfluencePage` contentFormat=adf); for each
        `zenuml-sequence-macro-lite` extension read `parameters.guestParams.customContentId`.
     2. `list_connected_browsers` → if >1, ask which (use `switch_browser`/`select_browser`);
        pick the **work browser** signed into Confluence. The page domain needs extension
        site-access granted, else JS/read are "denied on this domain".
     3. `navigate` that tab to the page, then `javascript_tool` (wrap in
        `(async()=>{…})()`, NOT top-level await):
        `fetch('/wiki/rest/api/content/<customContentId>?expand=body.raw')` →
        `JSON.parse(body.raw.value).code` = a ZenUML/mermaid source — but **`body.raw` is
        UNRELIABLE for these macros: it often returns the default starter template
        ("Order Service / OrderController / PurchaseService.createPO") even when a real
        diagram is rendered** (the live source lives in the ZenUML app backend, not
        Confluence content). So NEVER conclude "placeholder/never drawn" from body.raw.
     4. ALWAYS verify against the RENDERED diagram: expand the diagram's section, then
        `screenshot` + `zoom` the SVG and read participants + messages visually — that is
        the source of truth. Only call it a "placeholder diagram" if the RENDERED diagram
        is the default template.
   - **RIGOR IS MANDATORY — no structural glance.** A "looks aligned" pass is NOT
     acceptable. To emit a sequence verdict you MUST:
     a. Capture the diagram TOP-TO-BOTTOM (multiple zoomed screenshots) and transcribe
        EVERY participant + message + opt/alt block, in order. Partial reads → keep
        scrolling; do not conclude from the visible portion.
     b. READ the actual code path end-to-end (the real entry fn, e.g. `Execute()`, plus
        the helpers it calls — lock acquire, validations, build, persist, status) and list
        the real call order with `file:line`.
     c. Diff STEP-BY-STEP IN ORDER. Report per-step: matches / missing-in-code /
        missing-in-diagram / **reordered** (same steps, different order is a real drift).
        Naming-label mismatches (table/field names in the diagram vs code) are findings too.
     d. State the verdict precisely ("N steps match, M reordered, K naming") — never the
        vague "in sync". If you couldn't read the whole diagram or trace the whole path,
        say so and mark the check incomplete rather than claiming sync.
   - **Image/PNG blob** → not text-extractable; if the work browser is available, a
     `screenshot` of the rendered diagram can be read visually; else flag "verify manually".
   - **No browser available (unattended/cron)** → emit ONE low-severity note
     ("diagram source not machine-readable here — verify manually"); never fabricate steps.
4. **Behavior drift**. Narrative claims ("computed synchronously", "counter upserted
   here") vs the actual function body. READ THE BODY — a matching function name proves
   structure exists, not that it behaves as claimed. (This is the instant-pay-ATM `OnTransactionFailed`
   class of finding.)
5. **Decision drift**. Doc statements vs newer jira/slack decisions after last-updated.

**DIRECTION GATE (mandatory — classify every finding before emitting it):**

Per `.claude/shared/drift-direction-gate.md` — FORWARD (doc ahead of code) = not built yet,
suppress by default; BACKWARD (code ahead of / diverged from doc) = real drift, emit an edit;
AMBIGUOUS = propose verification; clean matches recorded as clean, never padded.

## Phase 5 — Output: a propose-only change-list (NEVER edit the page)

**Hard rule: this skill is READ-ONLY on Confluence.** It must NOT call
`updateConfluencePage` / `createConfluenceFooterComment` / any write. It outputs a
markdown punch-list the owner reviews and applies by hand (or approves separately).
Editing a shared TRD is high blast-radius — always human-gated.

Write the output to `management/doc-sync/<doc-slug>-<YYYY-MM-DD>.md`
(`mkdir -p` first; never overwrite — append `-2`, `-3`).

File header:
```markdown
# Doc-sync: <doc title>

**Page:** <url>  ·  **Status:** <status>  ·  **Last updated:** <date>
**Repo:** <repo>  ·  **Generated:** <YYYY-MM-DD HH:MM IST>
**Verdict:** <N backward-drift edits · M forward/planned · K clean>
```

Then, ordered backward-drift FIRST (the actionable part):

**Per backward-drift finding — lead PLAIN, keep proof underneath.** The reader may
not remember the feature's internals. Open every finding with a plain-English
headline + a one-line "In short" that a non-expert understands WITHOUT the code.
Then put the technical proof below for anyone who wants to verify. Same spirit as
`/ask` wiki voice: concept first, `file:line` as the footnote.

- **Plain headline** — one sentence, no jargon, names the gap in human terms.
  (e.g. "The doc claims the failure step also updates a usage counter — it doesn't.")
- **In short** — 1-2 plain sentences: what the doc says will happen, what actually
  happens, and why the gap matters (or "harmless, just inaccurate" if so).
- **Suggested edit** — the concrete rewrite, phrased so it can be pasted into the doc.
- *Detail (verify):* — the technical proof, indented/last: **Doc says** "<quote>" ·
  **Code does** <what> (`file:line`) · **Check** <schema/LLD/sequence/behavior/decision>
  · **Confidence** high/medium.

Keep the technicality (it pinpoints the gap) — but it lives under the plain lead,
never as the opening line. No reader should have to parse `file:line` to understand
WHAT is wrong; only to verify it.

**Two hard output rules:**
- **Always hyperlink doc-section references.** Every `§N` / section-name mention is a
  clickable markdown link to the Confluence page. Never write a bare `§4` the reader
  can't click.
  - **Use the REAL page URL from the API — never construct `/wiki/pages/<id>` (that
    404s).** Take `_links.base + _links.webui` from the `searchConfluenceUsingCql`
    result (form: `https://your-org.atlassian.net/wiki/spaces/<KEY>/pages/<id>/<slug>`),
    or the short `_links.base + _links.tinyui` (`…/wiki/x/<tiny>`). If you only have a
    page id, do a CQL/get call to fetch its webui link before writing any link.
  - **Deep-link to the section** with a heading anchor (confirmed working format):
    take the heading's visible text and replace every space with a hyphen, keeping
    numbers/periods/case as-is. E.g. heading "4. Hook Fire Order" →
    `#4.-Hook-Fire-Order`; "3.1 charge_attempts" → `#3.1-charge_attempts`. Append to
    the page URL: `<page-url>#4.-Hook-Fire-Order`. (The API does NOT expose heading
    ids, so this scheme is the only way — build it from the heading text.) If a
    heading has unusual punctuation and you're unsure, fall back to the page URL.
- **Never write meta-labels like "(plain English)", "(analyzed)", "(gist)" in the
  output.** Lead with plain language because that's the default voice — don't announce
  it. Section headings are plain nouns ("The gist", "What to fix"), no parenthetical
  tags. (Same rule as `/ask`'s forbidden-suffix grep-check.)

**Then a "Doc ahead of code (planned)" section** — forward findings, one line each,
with the tracking ticket if known. No edits.

**Then a "Clean" section** — surfaces checked that matched, one line each (e.g.
"§3 schema: all 4 tables match migrations"). Brief; this is the trust signal.

**Chat reply** ends with the verdict line + `**Saved to:** <abs path>`.

## Hard constraints

- READ-ONLY on Confluence. No page edits, no comments. Propose-only, always.
- Code wins on "what it does now"; doc/jira/slack win on "why/intended" (per ask.md
  precedence rule). The graph = merged remote default branch, not local WIP.
- Read migration DDL + function bodies directly — never assert drift from a name match.
- Honour the direction gate: forward findings (Draft/unbuilt) are NOT drift; do not
  emit edits for them.
- If a section maps to an unregistered repo or a DB stored-proc not in the graph,
  mark it "not checkable here" — don't fabricate a verdict.
- One doc per run (on-demand). No repo-wide scans.

## Anti-patterns (refuse to emit)

- A "fix the doc" edit for a Draft TRD whose code simply isn't built yet.
- A drift flag from a matching symbol/table name without reading the body/DDL.
- Editing or commenting on the Confluence page directly.
- Padding the change-list — a clean check is a valid, valuable result; report it as clean.
- Flagging the doc's own stale inline line-numbers as high-severity (note as minor).

## Smoke tests (development)

- Forward case: `/doc-sync Lien / Un-lien — Simplified TRD (v2)` → expect mostly
  "planned, not built" (Draft); zero backward-drift edits.
- Backward case: `/doc-sync TRD — instant-pay ATM Charges` → expect schema "clean" + a
  behavior-drift edit on the OnTransactionFailed hook row.

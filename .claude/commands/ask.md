Cross-source query router over the embedding + topic_brief pipeline. Routes natural-language questions to the right retrieval primitive in `derive/ask_engine.py`, then synthesizes a grounded answer with citations. Owner-invoked.

**ROUTER FILE — intent details live in `.claude/ask/*.md` chunks.** Classify the
intent FIRST (Phase 1), then Read ONLY the chunk file(s) for that intent (Phase 3
table). Do not read chunks for intents you are not running — that is the point of
the split (token + latency).

## Usage — `/ask <question>`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run any query.

**What it does:** Cross-source query router over the embedding + topic_brief pipeline (slack + jira + confluence + CMR + PRs + code). Classifies the question into ONE intent, dispatches to the matching retrieval primitive, then synthesizes a grounded, cited answer and saves it under `management/`.

**Intents it routes to:**
- `summarize` — what's happening with a topic / workstream.
- `person_range` — what a person shipped + how they worked over a date range (full or `brief`).
- `team_range` — whole-team narrative across all core ICs for a range.
- `attention` — what from the last N days needs the owner's attention (blockers, root causes).
- `ticket_gaps` — decisions/work that should have a Jira ticket but don't.
- `rootcauses` — categorise incidents/issues by root cause over a range.
- `dev_style` — a person's working style (delegates to `/dev-style`).
- `highs_lows` — stakeholder retro highs + lows (delegates to `/retro`).
- `feature_logic` — how a feature/computation works in code, reconciled with TRD/PRD + jira/slack (code graph).

**Usage:** `/ask <question>`
- `question` (required) — natural-language question across slack / jira / confluence / code.
- Modifiers: prefix `brief` (condensed person narrative) or `code` (engineer-facing feature_logic).

`$ARGUMENTS` is the question, e.g.:

```
/ask summarize everything done for instant-pay migration to service-a
/ask what did frank work on in march
/ask anything from yesterday that needs my attention
/ask any tasks for which jira ticket needs to be created
/ask go through issues in past month and categorise root causes
/ask working style of bob
/ask highs and lows of april
```

## Phase 1 — Classify intent

Parse the question. Pick exactly ONE intent. Resolve required parameters from the question; if missing, ask owner ONE clarifying question and stop.

| Intent          | Trigger phrases                                                   | Required params              | Route                         |
|---|---|---|---|
| **summarize**   | "summarize X", "what's happening with X", "what did we do for X"  | topic string                 | `ask_engine.py search`        |
| **person_range**| "what did <person> work on in <range>", "<person>'s work in <range>" | person, since, until         | `person_v4_manifest.py --bundle-dir /tmp` (one call: manifest+v3+deep) |
| **team_range**  | "what did the team do in <range>", "team narrative for <range>", "engineering retro narrative <range>" | since, until                 | loop `person_range` over `config/people.yaml` |
| **attention**   | "anything from yesterday / last N days I should care about", "what needs my attention" | since, until, optional me=owner | `ask_engine.py window`        |
| **ticket_gaps** | "tasks needing jira ticket", "untracked decisions", "unlinked work" | optional since/until         | `ask_engine.py gaps`          |
| **rootcauses**  | "root causes for X in past N days", "categorise issues in past month", "incident themes" | since, until                 | `ask_engine.py rootcauses`    |
| **dev_style**   | "working style of X", "how does X work", "X's response pattern"   | person                       | invoke `/dev-style <person>`  |
| **highs_lows**  | "highs and lows of <month>", "retro of <range>", "what went well/badly" | since, until                 | invoke `/retro <range>`       |
| **feature_logic** | "how is X aggregated / computed", "logic for Y in code", "how does the <feature> flow work", "where is X implemented" | feature/concept string, optional repo | service briefs → code-graph MCP + Confluence (chunk file) |
| **event_metrics** | "how many times did X occur / fire", "count of X alerts in <range>", "how often did Y happen", "frequency of <alert/event> in <month>" | terms (keywords), since, until, optional channel/source | `ask_engine.py events` (chunk file) |

If ambiguous between two intents (e.g. "what did bob work on AND his response style"), pick the structural one first (person_range) and offer to follow up with the other.

**`feature_logic` is the CODE intent — distinct from every other intent above.**
Every other intent answers from the *events* pipeline (slack/jira/CMR/confluence/PR
embeddings + clusters). `feature_logic` answers from the **code graph** (the actual
source of `service-a` + `service-c`). Route here when the question is about *how the code
works* — computation logic, data flow, where a thing is implemented — NOT about who
did what or what shipped. Disambiguation:
- "how is withholding aggregated" / "CGST/SGST charge logic" / "where is the ledger-balance
  computed" → **feature_logic** (asking about code behaviour).
- "what did the team do on withholding in May" / "ledger-balance issues this month" →
  **summarize** or **rootcauses** (asking about activity/incidents).
- Mixed ("why did ledger-balance break in May AND how is it computed") → answer the
  incident leg via `rootcauses`, then OPTIONALLY add a code-logic addendum from
  `feature_logic`. Keep the two legs visibly separate; do not blend tool jargon.

**`event_metrics` is the COUNT intent — distinct from `summarize` and `feature_logic`.**
Route here when the question wants a NUMBER or FREQUENCY over raw events — "how many
times did the ledger-balance mismatch alert fire in May", "count of instant-pay settlement
failures last week", "how often did X occur". This queries `events` directly, NOT
`topic_brief` clusters. Critical: automation channels (alert/recon/digest) are
EXCLUDED from clustering (see `derive/cluster_noise_filter.py`), so they are absent
from `summarize`/cluster routes — `event_metrics` is the ONLY route that sees them.
- "how many ledger-balance mismatches in May" → **event_metrics** (a count).
- "what's the ledger-balance reconciliation workstream" → **summarize** (a narrative).
- "where is the ledger-balance computed" → **feature_logic** (code).

**Isolation rule (design decision):** `feature_logic` is self-contained. It MUST NOT
alter `/retro` (stakeholder voice bans code/tech jargon) or `person_range` (real-artefact
framing; the code graph is author-agnostic and can't improve attribution). The only
sanctioned cross-over is `rootcauses`/incident queries reaching the code graph on-demand
for impact-radius / affected-flows — never automatically.

## Phase 2 — Resolve params

### Date range parsing

Per `.claude/shared/date-range-grammar.md` (IST relative dates → ISO8601 bounds,
working-hours window, weekend guard). Today = the cron-status `currentDate`.

### Person resolution

Per `.claude/shared/roster-identity.md` §5 — case-insensitive substring against `canonical`
in `config/people.yaml`; if multiple matches, list them and ask which.

## Phase 3 — Dispatch (Read the chunk file(s), then follow them)

```bash
cd $HOME/context/work-context
```

Chunk files live in `$HOME/context/.claude/ask/`. Read EVERY file listed for the
chosen intent, in order, then execute. Do not read other intents' chunks.

| Intent          | Read these files (in order)                                                  |
|-----------------|-------------------------------------------------------------------------------|
| summarize       | `summarize.md`, `narrative-style.md`                                          |
| person_range    | `person-range.md`, `narrative-style.md`, `person-template.md`                 |
| team_range      | `team-range.md`, `person-range.md`, `narrative-style.md`, `person-template.md` |
| attention       | `attention.md`                                                                 |
| ticket_gaps     | `ticket-gaps.md`                                                               |
| rootcauses      | `rootcauses.md`, `narrative-style.md`                                          |
| feature_logic   | `feature-logic.md`                                                             |
| event_metrics   | `event-metrics.md`                                                             |
| dev_style       | none — delegate (below)                                                        |
| highs_lows      | none — delegate (below)                                                        |

### dev_style (delegation — no chunk file)

Invoke the existing skill (do NOT reimplement):

```
/dev-style <person>
```

If the question also asks for a comparison ("Alice vs Bob"), run `/dev-style` twice and surface the deltas afterward.

### highs_lows (delegation — no chunk file)

Invoke the existing skill:

```
/retro since=<iso> until=<iso>
```

`/retro` produces a STAKEHOLDER-FACING document — team-level voice, no dev names, ONLY actual production deliveries in Highs, real impact numbers from slack threads. Format matches the owner's prior monthly stakeholder updates (Feb + March precedent in `#example-monthly-update` channel).

Key rules `/retro` enforces:
- Team-level voice only. "The team delivered X" — never "Alice shipped Y" or "Bob owned Z".
- Highs = production deliveries only. Code merged / ticket-Done WITHOUT user-facing rollout = NOT a high. It belongs in Lows as "X dev complete, rollout slipping to <date>".
- Every high needs measurable impact (RPS, latency, success rate, accounts onboarded, downtime saved, cost reduction). Pull from team's own slack rollout-update posts.
- No internal jargon. No PR/ticket/cluster references. No IC-level metrics.

## Phase 4 — Synthesise + cite

Rules live in the chunk files: `narrative-style.md` (output style, deep-read,
translation hard rules, pre-save grep-check, length guidance, anti-patterns) +
`person-template.md` (locked person output template, signal groups, reliability
gates). The Phase 3 table says which apply. Self-contained intents (attention /
ticket_gaps / feature_logic / event_metrics) carry their own render template.

## Phase 5 — Save output to markdown file (MANDATORY)

**Every `/ask` run writes its rendered output to a markdown file under
`management/`.** Format + safety rules (header, never-overwrite, `Saved to:` footer,
Write-tool/mkdir) are shared — `.claude/shared/output-save-conventions.md`. The per-intent
PATHS below are `/ask`-specific.

Filename convention by intent (use lowercase kebab-case for variable
parts; pick a 4-6-word `<topic-slug>` for free-text intents):

| Intent          | Path                                                                                        |
|-----------------|---------------------------------------------------------------------------------------------|
| person_range    | `management/narratives/per-person/<canonical>-<since>-to-<until>.md`                        |
| team_range      | `management/narratives/team/<since>-to-<until>.md`                                          |
| summarize       | `management/queries/summarize-<topic-slug>-<YYYY-MM-DD>.md`                                 |
| attention       | `management/queries/attention-<YYYY-MM-DD>.md`                                              |
| ticket_gaps     | `management/queries/ticket-gaps-<YYYY-MM-DD>.md`                                            |
| rootcauses      | `management/queries/rootcauses-<since>-to-<until>.md`                                       |
| feature_logic   | `management/queries/feature-logic-<topic-slug>-<YYYY-MM-DD>.md`                             |
| event_metrics   | `management/queries/event-metrics-<topic-slug>-<since>-to-<until>.md`                       |
| dev_style       | (handled by `/dev-style` skill)                                                             |
| highs_lows      | (handled by `/retro` skill — writes to `management/retros/<since>-to-<until>.md`)           |

**File header, never-overwrite, `Saved to:` footer, Write-tool/mkdir:** per
`.claude/shared/output-save-conventions.md`. For `/ask`, the header's `**Intent:**` line
names the intent and `**Question:**` carries the verbatim user question. Body = the full
rendered output exactly as in chat; inline citations + project-level voice rules apply.

**Why mandatory:** ad-hoc /ask queries previously produced chat-only output that
disappeared once the session ended. The file preserves the analysis + citations for future
reference (1:1 prep, retro source, audit trail). Cost is trivial.

**Verify gate:** person_range additionally runs the deterministic verify gate
AFTER saving — spec + loop rules in `person-range.md`. Other intents skip it.

### URL conventions (use as inline markdown links)

Per the shared render rules — **Read `.claude/shared/render-rules.md` §1** for the
slack / Jira / Confluence / GitHub-PR link formats + section-anchor deep-linking.
The shared file also carries the never-a-bare-ID rule (§2), self-summarizing thread
refs (§3), and the pre-save link check (§4) — they apply to `/ask` output too.

## Hard constraints

- Read-only. NEVER write to `topic_brief`, `events`, or `embedding`.
- NO LLM API calls — synthesis happens in chat from JSON output + deep-read of events.db.
- Cite every claim. If you make a statement, it must trace to a subject
  in the JSON output AND you must have read that subject's actual body
  content to support the claim.
- If a primitive returns empty, say so plainly. Suggest scoping fixes.
- Don't conflate authorship with work. When citing impact, lead with
  substantive contribution signals, not "X authored ticket Y".

## When `/ask` doesn't fit

If the question is:
- a code review → suggest `/review` or `/security-review`
- a single-file lookup → just use grep/Read directly
- a Slack ingest question → suggest `/slack-ingest` or `/slack-backfill`

Don't force-route everything through `/ask`.

## Smoke tests (for development)

```bash
.venv/bin/python derive/ask_engine.py search --query "instant-pay migration to service-a" --k 10
.venv/bin/python derive/ask_engine.py person --name frank --since 2026-03-01 --until 2026-04-01
.venv/bin/python derive/ask_engine.py window --since 2026-05-18 --until 2026-05-20
.venv/bin/python derive/ask_engine.py gaps
.venv/bin/python derive/ask_engine.py rootcauses --since 2026-04-19 --until 2026-05-19

# Deterministic per-person Signals — text format for quick eyeballing,
# JSON (default) for chat consumption.
.venv/bin/python derive/person_profile.py --name grace --since 2026-03-21 --until 2026-05-21 --format text
```

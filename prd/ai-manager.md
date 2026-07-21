# PRD — AI Manager ("Synapse Manager")

**What this is:** the layer that turns the context engine (events.db, embeddings, topic
briefs, skills, routines) from a *query tool* into a *manager* — a persistent, proactive,
judgment-bearing agent that remembers state between sessions, composes its own retrieval,
and raises risks before being asked.

**One-liner:** the skills are verbs; this adds the noun. A world model the AI owns
(the ledger), an open conversational surface over every source (/manager), and an
initiative loop that diffs reality against expectations and calls out what's drifting.

## Problem

- Every skill today recomputes from events.db and forgets. There is no memory of
  "what I expected to happen" — so nothing can notice when it doesn't.
- /ask routes questions into pre-shaped intents. Cross-cutting managerial questions
  ("is CBS sunset actually on track and who's the bottleneck?") don't fit any single
  intent; the owner feels restricted because the *interface* is the router, not the model.
- All intelligence is pull-based. Risks the owner doesn't know to ask about
  (a commitment made in a meeting that nobody followed up, a dev gone quiet on their
  epic, review-stuck PRs, sprint burn drifting from plan) surface late or never.
- Skill outputs land in `management/` as disconnected artifacts (pulse, standup,
  narratives, retros). Nothing synthesizes them into a continuous picture of the team.

## Product principles

0. **Additive-only — the existing system is load-bearing and untouched.** Everything
   ships as NEW files (`management/ledger/**`, new skill, new docs, new gather scripts).
   Zero edits to existing skills, ingest, derive scripts, routines, planners, or
   dashboard. Reads of existing data (events.db, derived/, config yamls) are read-only.
   Where an integration point wants to touch an existing file (routines.yaml entry,
   dashboard tile, planner wiring, root .gitignore), it is deferred and each such edit
   is a separately-approved step — never bundled into a phase.

1. **Chat-only LLM.** No direct LLM API calls from scripts — Claude in-session (or a
   scheduled Claude routine) IS the analysis layer. OpenAI key stays embeddings-only.
   Same policy as rollup/sprint-plan/monthly-plan.
2. **Skills demote to library, not UI — and are FROZEN.** Existing gather scripts,
   `jira_metrics.py`, vector search, person manifests, pulse — the manager *invokes*
   them exactly as the owner does today and reads the artifacts they already produce.
   No skill file, prompt, script, or output contract is ever modified for the manager's
   benefit. If the manager needs a capability an existing skill almost-but-not-quite
   provides, it gets its OWN new script under the manager's namespace — it does not
   bend the existing one. Every skill behaves identically whether the manager exists
   or not.
3. **Deterministic where outward, agentic where owner-facing.** Anything other people
   read (standup posts, retro docs, Jira comments) keeps its deterministic renderer and
   relay Approve gate. Free-form reasoning is owner-eyes-only.
4. **Ledger is private by construction.** `management/ledger/` is gitignored (repo is
   public). People-state notes never leave the machine; any derived artifact that could
   be shared passes the leak-scan like everything else.
5. **Honest judgment.** Every callout carries receipts (event ids, ticket keys, thread
   links) and a confidence. "I think X is slipping because Y, Z" — never vibes.
   Unattributable signals are marked, not guessed (ticketize attribution rule).
6. **Reuse the routine infra.** Proactive sweeps are ordinary scheduled Claude routines
   (`work-context/scheduled-tasks/routines.yaml` + retry-until-success gates), run after
   the ingest windows they depend on.

## Architecture — three layers

### L1. The Manager Ledger (world model)

Persistent, structured state the AI owns and updates. Lives in `management/ledger/`:

```
management/ledger/
  goals.yaml         # active initiatives: expected trajectory, target dates, health
  risks.yaml         # open risks: severity, trend (better/worse/flat), receipts, owner
  commitments.yaml   # promises made (meetings, threads, standups): who, what, by-when, status
  people/<slug>.md   # per-person manager notes: load, growth arc, flags, 1:1 threads
  decisions.md       # decision log: what was decided, where, receipts
  watchlist.yaml     # things the owner said "keep an eye on"
```

Rules:
- **Schema'd YAML, append-friendly.** Every entry: `id`, `created`, `last_reviewed`,
  `status`, `receipts: [event refs]`. Free prose only in people notes and decisions.
- **The AI writes it, the owner can edit it.** Manual edits are authoritative; the
  manager never reverts an owner edit (same as people.yaml autofix append-only rule).
- **Staleness is a first-class signal.** Any entry not reviewed in N days surfaces in
  the next brief ("still true?"). The ledger must not silently rot.
- **Bootstrap from existing derived data:** goals from `derived/initiatives.json` +
  projects.yaml slugs; people files seeded from the last /pulse + /dev-style outputs;
  risks seeded empty (populated by the first sweeps + owner conversation).

### L2. The open agent surface (/manager)

A skill that is deliberately NOT a router: it primes a session with the world and lets
Claude compose.

Priming pack (all pointers, not inlined content):
- `management/ledger/**` — current world model
- `work-context/derive/SCHEMA.md` — events.db schema + canonical query patterns (new doc)
- `management/ledger/TOOLS.md` — the tool inventory: every gather script, metric module,
  vector-search helper, and skill, one line each: what it answers, how to call it (new doc)
- `config/projects.yaml`, `config/people.yaml` — identity + domain maps
- the current sprint plan (`work-context/derived/sprint-plan.json`) and monthly plan

Behavior contract:
- Answer anything; plan own retrieval across Jira/Slack/GitHub/Confluence/meetings.
- **Write back learnings.** When the conversation establishes a goal, risk, commitment,
  or decision — update the ledger in the same turn (with the owner's line as receipt).
  This is how the manager gets smarter instead of starting from zero.
- Guide and push back: when asked to plan, check capacity/leaves/on-call first; when the
  owner proposes something that contradicts the ledger (an existing decision, a known
  risk), say so with the receipt.
- Never post outward from a /manager session; drafts hand off to the relevant skill's
  Approve gate.

### L3. The initiative loop (proactive)

A scheduled sweep (`manager-sweep`) that runs after the ingest windows and diffs new
reality against ledger expectations. Output: `management/ledger/briefs/<date>.md` —
the **daily manager brief** — plus a dashboard card and (P2+) an optional Slack DM to
the owner via relay.

Detector catalog (each emits callouts with receipts + severity):

| Detector | Signal |
|---|---|
| `sprint-drift` | burn vs sprint-plan.json expected trajectory; spillover risk by dev |
| `stuck-review` | PR open/in-review > N days, especially cross-team reviewers |
| `quiet-epic` | epic with no events from its owner in N days while In Progress |
| `commitment-due` | commitments.yaml items due/overdue with no closing event |
| `risk-trend` | risks.yaml items whose receipt stream is growing (getting worse) |
| `unticketed-risk` | Slack thread matching risk language on a team surface with no Jira ref (reuses ticketize wide-recall) |
| `meeting-followup` | action items from meeting notes (source=meeting) with no follow-up event |
| `load-imbalance` | per-dev WIP + after-hours share outliers vs team baseline |
| `ledger-staleness` | goals/risks not reviewed in N days |

Rules:
- Sweep is a Claude routine (chat-only policy holds); deterministic gather scripts
  produce the candidate deltas, the session judges them (same DETECT→classify shape as
  ticketize/leaves).
- Every callout is **suggest-only**. The brief proposes; the owner (or a /manager
  conversation) disposes. Dispositions write back to the ledger (ack, snooze-until,
  dismiss-with-reason) so the same callout never nags twice.
- Quiet by default: no callouts → one-line brief. Never manufacture urgency.

## What "help me plan" means here

The planners (sprint, monthly) already exist. The manager's additions:
- **Pre-planning brief:** before /sprint-plan, the manager summarizes ledger state that
  should shape the plan (open risks touching this sprint's epics, overdue commitments,
  who's overloaded, leaves) — so the plan starts informed, not blank.
- **Plan-vs-reality memory:** after a sprint closes, the sweep writes the delta
  (planned vs landed, by dev and epic) into the ledger. Over sprints this becomes the
  velocity/estimation-bias memory a human manager carries in their head.
- **Mid-sprint steering:** sprint-drift detector + /manager conversation to re-plan
  ("if X slips, what moves?") on the planner's scratch surface — never touching Jira
  without /sprint-apply.

## Phases

### P1 — Ledger + /manager (the unlock)
- `management/ledger/` structure + schemas; privacy via a `.gitignore` INSIDE
  `management/ledger/` (self-contained — no edit to any existing gitignore).
- Bootstrap script (deterministic): seed goals from initiatives/projects.yaml, people
  files from latest pulse/dev-style artifacts. One-time, owner-reviewed.
- `SCHEMA.md` (events.db) + `TOOLS.md` (tool inventory) — written once, maintained by
  doc-sync-style drift checks later.
- `/manager` skill: priming pack + behavior contract + ledger write-back rules.
- Exit criteria: a /manager session answers a cross-cutting question end-to-end with
  receipts, AND a stated goal/risk from the conversation lands in the ledger correctly.

### P2 — The sweep + daily brief
- `manager-sweep` routine: gather scripts for the first 5 detectors (sprint-drift,
  stuck-review, quiet-epic, commitment-due, ledger-staleness), session judgment pass,
  brief renderer, disposition write-back.
- Sweep runs owner-invoked first (a `/manager-sweep` skill call), so P2 needs NO edit
  to routines.yaml or the dashboard. Scheduling it (routines.yaml + install manifest,
  per the sync rule) and the dashboard tile are each separately-approved follow-ups
  once the brief has proven itself.
- Exit criteria: two weeks of briefs with <20% dismissed-as-noise callouts.

### P3 — Full judgment
- Remaining detectors (unticketed-risk, meeting-followup, load-imbalance, risk-trend).
- Plan-vs-reality memory + pre-planning briefs. Briefs land as standalone files the
  owner brings into a planning session — the planner skills themselves stay unedited;
  any direct wiring into /sprint-plan or /monthly-plan is a separately-approved edit.
- Optional owner DM of the brief via relay (auto-send per scheduled-post policy).
- Commitment extraction from meeting notes pipeline (P1 of meeting-intelligence already
  emits action items — wire them into commitments.yaml).

## Tensions & risks

- **Variance vs judgment.** The system fought LLM variance with manifests and verify
  gates. The reconciliation: ledger *writes* are schema-validated (a verify step rejects
  malformed entries); brief *prose* is free. Outward artifacts unchanged.
- **Ledger rot.** A stale world model is worse than none — hence staleness as a
  detector and `last_reviewed` on every entry.
- **Nag fatigue.** Dispositions (ack/snooze/dismiss) are mandatory plumbing in P2, not
  a later nicety; a manager that repeats itself gets ignored.
- **Privacy.** people/*.md is the most sensitive artifact in the repo. Gitignored,
  leak-scan denylist entries for its path, never quoted into shareable outputs.
- **Scope creep into HR.** People notes capture work signals (load, growth topics,
  flags) — not performance ratings. /dev-review stays a separate, deliberate act.

## Out of scope

- Autonomous outward actions (posting, ticket creation, Jira edits) — everything
  outward keeps its existing maker-checker gate.
- A new UI. The brief is markdown + a dashboard tile; conversation is /manager in a
  session. (A Synapse page for the ledger can come later if reading YAML gets old.)
- Replacing the deterministic skills. Standup/retro/ask keep their contracts.

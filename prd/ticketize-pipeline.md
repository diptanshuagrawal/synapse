# PRD — Ticketize: work-gap → Jira ticket (→ gated PR)

_Status: draft · 2026-06-09 · owner: owner_

## Problem
`/standup` already surfaces real work that has **no Jira ticket**: someone doing
adhoc debugging, a cross-team Slack ask to "pick this up next sprint", a long
prod-support thread with no bug/CMR. Today that signal dies in the digest — the
manager hand-creates tickets later, or they never get tracked. Backlog drifts
from reality and pickup-asks get lost.

Separately, a slice of those gaps are small, well-scoped code fixes that today
wait on a human to even start. We already have `/trd-build` + `/pr-from-trd` that
can implement a feature in a real Go service and raise a PR. Nothing connects the
detected gap to that machinery.

## Goal
Turn standup-detected work-gaps into tracked Jira tickets through an explicit
**maker-checker** gate, and let the narrow, safe subset of those tickets flow
into the existing code→PR skills — without ever collapsing the read-only,
ticket-write, and code-write risk tiers into one unsupervised flow.

## Non-goals
- NOT an autonomous bug-fixer fed straight from a standup line.
- NOT auto-merge. Ever.
- NOT touching `/standup` — it stays read-only and cron-safe.
- NOT code changes to money-math / ledger / financial-correctness paths (see tiers).

## Risk tiers (the spine of the design)
The pipeline is a risk gradient; the gate gets **stricter** going down, never looser.

| Stage | Blast radius | Gate |
|---|---|---|
| `/standup` detect | read-only | none |
| `/ticketize` create | Jira write (reversible) | 1 human gate |
| code change + PR | real service code | human gate + CI + existing PR review |

Code-eligibility sub-tiers:
- 🟢 **Auto-draftable** — typo, config bump, single-function mechanical fix, ticket
  with clear repro + named file/symbol, single repo. → draft PR, human review.
- 🟡 **TRD-first** — real feature. → `/trd-build` → human → `/pr-from-trd`.
- 🔴 **Never auto** — ledger / ledger-balance-diff / money math / cross-service. Human only.
  (e.g. a ledger-balance-diff defect and a withholding label defect surfaced in a
  standup are both 🔴 — ambiguous, money-critical, cross-team.)

## Approach — two layers, three skills

### Layer 1: `/ticketize` (the maker-checker for Jira)
Sibling of `/standup`, separate skill, own file. Two modes:

**`/ticketize [window]` — DETECT (maker, Jira read-only)**
1. Reuse `bin/standup_gather.py` output (window PRs, commits, Jira, Slack + board).
2. Detect ticketable gaps (judgement, model-driven, conservative — under-propose):
   - adhoc work: PR/commit/long Slack thread by a member citing no `EX-` and not
     matching any of their in-progress tickets;
   - future ask: imperative/future Slack ask directed at a member, citing no ticket.
3. Pre-create dedupe: search Jira by PR link / summary keywords; skip if a ticket
   already exists.
4. Emit a **proposal file** → `management/standup/<date>/ticket-candidates.md` —
   each candidate: proposed summary, type, assignee, epic guess, placement,
   evidence link, code-tier (🟢/🟡/🔴), `decision: pending`.
5. Record fingerprints in `state/ticket_candidates.json` (person + normalized
   summary + source link) so the same gap isn't re-proposed daily.

**Checker — the owner**
Edits the file: sets each `decision:` to `approve` / `reject`, or tweaks fields.

**`/ticketize apply [date]` — APPLY (the ONLY Jira-write step)**
- Reads `approve`-marked candidates, calls `createJiraIssue`.
- Idempotent: skips any fingerprint already created; writes the new `EX-NNNN`
  back into the file + state.
- Owner-invoked only, never on cron — mirrors `/rollup` + `/leaves`
  (chat does the mutation, scripts/cron never do).

### Layer 2: gated code hand-off (reuse, no new engine)
- A created candidate tagged 🟢/🟡 carries a `code-actionable` flag + its `EX-NNNN`.
- `/ticketize` does NOT write code. It only hands the ticket ID to the existing path:
  - 🟢 → a thin `/ticket-fix <EX>` (optional v2): mechanical, single-repo edit →
    `make ready` + test → **draft** PR → human review. Push gated on explicit "go"
    (inherits `/pr-from-trd` push gate).
  - 🟡 → `/trd-build <EX>` → human → `/pr-from-trd`.
- 🔴 never enters Layer 2.

## Key decisions
- **Separate skill**, not an extension of `/standup` — quarantines the only
  Jira-write power and keeps the read-only daily digest untouched.
- **File-as-approval** — the owner's edit to `decision:` IS the gate; `apply` reads
  it. No second chat re-confirm (one clean gate). (open Q below)
- **Backlog-only placement** by default — no board/sprint-ID resolution, nothing to
  mis-assign; owner promotes to a sprint manually.
- **Full-draft pre-fill** — candidates come pre-filled (summary, type, assignee,
  epic, description+links); guesses clearly marked so approve/reject is fast.
- **Reuse `/pr-from-trd` + `/trd-build`** for all code work — no new code-gen engine.
- **Conservative detection** — false-positive cost is high (spam); under-propose and
  lean on the human gate.

## Idempotency & safety
- Fingerprint dedupe across runs (state json) — no daily re-proposal, no double-create.
- `apply` is re-runnable: created candidates are skipped on fingerprint.
- Assignee resolution via `lookupJiraAccountId`; unresolved → leave blank, flag.
- Every created ticket records provenance (the Slack/PR evidence link) in its body.

## Scope
- v1: Layer 1 only (`/ticketize` detect + apply), backlog-only, EX project,
  roster = `/standup` `scope: team`.
- v2: Layer 2 🟢 `/ticket-fix` (draft PR for mechanical fixes), and 🟡 `/trd-build`
  hand-off.
- Out: 🔴 anything; auto-merge; non-EX projects; sprint auto-placement.

## Open questions
1. Approval mechanism — file-only (recommended) vs also chat-confirm vs both?
2. Detect trigger — purely on-demand `/ticketize`, or auto-emit candidates at the
   end of every `/standup` run (still read-only)?
3. 🟢 auto-draft-PR in v1, or hold all code work to v2 after the ticket flow is trusted?
4. Who is the checker when the manager is the maker's subject (owner's own gap)?

## Files (planned)
- `.claude/commands/ticketize.md` — the skill (detect + apply)
- `state/ticket_candidates.json` — fingerprint + created-ID state
- `management/standup/<date>/ticket-candidates.md` — proposal/checker file
- (v2) `.claude/commands/ticket-fix.md` — 🟢 mechanical fix → draft PR
- reuses: `bin/standup_gather.py`, `/trd-build`, `/pr-from-trd`

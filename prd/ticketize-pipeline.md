# PRD — Ticketize: work-gap → Jira ticket (→ gated PR)

_Status: draft · 2026-06-09 · owner: owner_

**TL;DR:** Turn `/standup`-detected untracked work into Jira tickets via a maker-checker gate, and let the safe subset flow into existing code→PR skills — never collapsing read-only / ticket-write / code-write risk tiers into one unsupervised flow.

## Problem
`/standup` surfaces real work with **no Jira ticket**:
- adhoc debugging
- a cross-team Slack ask to "pick this up next sprint"
- a long prod-support thread with no bug/CMR

Today that signal dies in the digest — the manager hand-creates tickets later, or they never get tracked. Backlog drifts from reality; pickup-asks get lost.

Separately, a slice of those gaps are small, well-scoped code fixes that wait on a human to even start. We have `/trd-build` + `/pr-from-trd` to implement a feature in a real Go service and raise a PR — but nothing connects the detected gap to that machinery.

## Goal
Track detected gaps as Jira tickets through an explicit **maker-checker** gate, and route the narrow safe subset into the existing code→PR skills.

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
- 🟢 **Auto-draftable** — typo, config bump, single-function mechanical fix; clear repro + named file/symbol; single repo. → draft PR, human review.
- 🟡 **TRD-first** — real feature. → `/trd-build` → human → `/pr-from-trd`.
- 🔴 **Never auto** — ledger / ledger-balance-diff / money math / cross-service. Human only.
  (e.g. a ledger-balance-diff defect and a withholding-label defect are both 🔴 — ambiguous, money-critical, cross-team.)

## Approach — two layers, three skills

### Layer 1: `/ticketize` (maker-checker for Jira)
Sibling of `/standup`, separate skill, own file. Two modes:

**`/ticketize [window]` — DETECT (maker, Jira read-only)**
1. Reuse `bin/standup_gather.py` output (window PRs, commits, Jira, Slack + board).
2. Detect ticketable gaps (model-driven, conservative — under-propose):
   - adhoc work: PR/commit/long Slack thread by a member citing no `EX-` and not matching any in-progress ticket;
   - future ask: imperative/future Slack ask directed at a member, citing no ticket.
3. Pre-create dedupe: search Jira by PR link / summary keywords; skip if a ticket exists.
4. Emit a **proposal file** → `management/standup/<date>/ticket-candidates.md`. Each candidate: proposed summary, type, assignee, epic guess, placement, evidence link, code-tier (🟢/🟡/🔴), `decision: pending`.
5. Record fingerprints in `state/ticket_candidates.json` (person + normalized summary + source link) so the same gap isn't re-proposed daily.

**Checker — the owner:** edits the file, setting each `decision:` to `approve`/`reject`, or tweaks fields.

**`/ticketize apply [date]` — APPLY (the ONLY Jira-write step)**
- Reads `approve`-marked candidates, calls `createJiraIssue`.
- Idempotent: skips any fingerprint already created; writes new `EX-NNNN` back into file + state.
- Owner-invoked only, never on cron — mirrors `/rollup` + `/leaves` (chat does the mutation, scripts/cron never do).

### Layer 2: gated code hand-off (reuse, no new engine)
- A 🟢/🟡 candidate carries a `code-actionable` flag + its `EX-NNNN`.
- `/ticketize` does NOT write code. It only hands the ticket ID off:
  - 🟢 → thin `/ticket-fix <EX>` (optional v2): mechanical single-repo edit → `make ready` + test → **draft** PR → human review. Push gated on explicit "go" (inherits `/pr-from-trd` push gate).
  - 🟡 → `/trd-build <EX>` → human → `/pr-from-trd`.
- 🔴 never enters Layer 2.

## Key decisions
- **Separate skill**, not an extension of `/standup` — quarantines the only Jira-write power; keeps the read-only daily digest untouched.
- **File-as-approval** — the owner's edit to `decision:` IS the gate; `apply` reads it. No second chat re-confirm (one clean gate). (open Q below)
- **Latest-active-sprint placement** — apply resolves the current active sprint (via `openSprints()`) and attaches each ticket; no story points (devs add at planning). Parent to the candidate's epic, or the **Tech-Misc fallback epic `EX-2882`** when none is confident. `Environment` defaults to `PROD`.
- **Full-draft pre-fill** — candidates pre-filled (summary, type, assignee, epic, description+links); guesses clearly marked so approve/reject is fast.
- **Reuse `/pr-from-trd` + `/trd-build`** for all code work — no new code-gen engine.
- **Conservative detection** — false-positive cost is high (spam); under-propose, lean on the human gate.

## Idempotency & safety
- Fingerprint dedupe across runs (state json) — no daily re-proposal, no double-create.
- `apply` is re-runnable: created candidates skipped on fingerprint.
- Assignee resolution via `lookupJiraAccountId`; unresolved → leave blank, flag.
- Every created ticket records provenance (the Slack/PR evidence link) in its body.

## Scope
- **v1:** Layer 1 only (`/ticketize` detect + apply), latest-active-sprint placement + Tech-Misc fallback epic, EX project, roster = `/standup` `scope: team`.
- **v2:** Layer 2 🟢 `/ticket-fix` (draft PR for mechanical fixes), 🟡 `/trd-build` hand-off.
- **Out:** 🔴 anything; auto-merge; non-EX projects; cross-board placement.

## Open questions
1. Approval mechanism — file-only (today) vs chat-confirm vs **daily Slack reply** (see v1.5)?
2. Detect trigger — on-demand `/ticketize` only, or auto-emit candidates at the end of every `/standup` run (still read-only)?
3. 🟢 auto-draft-PR in v1, or hold all code work to v2 after the ticket flow is trusted?
4. Who is the checker when the manager is the maker's subject (owner's own gap)?

## Daily Slack interaction (planned — v1.5)
Goal: run the maker-checker **daily from Slack** with no new infra (Slack MCP is
send + read only; clickable Block Kit buttons need a hosted interactivity endpoint and
break the "cron/chat does the Jira write" pattern — explicitly out).

**Reply-driven flow (recommended):**
1. **Morning cron** runs `/ticketize` DETECT (read-only) → posts the numbered candidate
   list to the owner's Slack DM (or a private channel) as one threaded message
   (`slack_send_message`): per line = summary, assignee, tier, links.
2. **Owner replies in-thread** during the day: `apply G2, C3` / `reject C1` / `approve all`.
3. **Later run** (evening cron, or owner-triggered) reads the thread (`slack_read_thread`),
   parses the reply, runs `apply` for approved items, posts a confirmation with the created
   `EX-NNNN` keys.

Why it holds the invariants:
- The Jira write stays gated — the Slack reply IS the approval (same role as the file edit).
- Uses only `slack_send_message` + `slack_read_thread`; no Slack app / endpoint.
- Idempotency unchanged (fingerprint state); re-runs never double-create.
- DM-to-self = only the owner sees/approves (authz).

**Rollout:**
- **v1.5a (WIRED 2026-06-11):** scheduled task `track-work-ticketize` (weekday 12:24 IST,
  mirrors `daily-standup`) runs DETECT read-only and posts candidates to **#track-work**
  (`<channel id>`); approval is a reply / Claude chat. Apply stays manual + gated.
- **v1.5b (BUILT 2026-06-11):** `/ticketize reply [date]` reads the owner's Slack reply on
  the DETECT post and applies it. Deterministic parser `bin/ticketize_reply.py` (fail-closed:
  ambiguous/unknown → apply nothing, post help). Skill §2.5. Owner-replies-only, idempotent.
  Trigger = manual `/ticketize reply`, OR opt-in second scheduled task (cron writes Jira after
  your reply — enable deliberately). Reply IS the gate (≡ the file `decision:` edit).
- **Out:** emoji-reaction gate (needs verified reaction-read; reply-parsing is safer);
  Block Kit interactive buttons (needs a hosted Slack app).

Cadence: a single daily run can both apply yesterday's approved replies and post today's
new candidates. Channel + schedule come from config (mirror `oncall.yaml` / cron pattern),
never hardcoded.

**Delivery target:** a dedicated **private Slack channel, owner-only** (feed-style, like a
standup channel) — e.g. `#ticketize-<owner>`. NOTE: the Slack MCP has **no create-channel
tool** → the owner creates the channel + adds the Slack app; the skill resolves its ID via
`slack_search_channels` (store in config). Zero-setup fallback: post to the owner's **self-DM**
(own `user_id` as `channel_id`) — truly single-recipient, nothing to create.

**Scheduling — local task, NOT a cloud routine.** Ticketize needs the **local `events.db`**
(via `standup_gather`) and the **interactively-authenticated Slack MCP**. A cloud *routine*
likely has neither (same reason ingest/standup run on **local LaunchAgents**, not routines).
So schedule via a **local scheduled task / LaunchAgent**, mirroring the standup cron — unless
the routine runtime is confirmed to have repo + Slack-MCP access. The DETECT half is read-only
and cron-safe; APPLY stays gated on the owner's Slack reply (never auto-applies).

## v1.5c — Socket Mode buttons (CHOSEN — native, real-time)
Supersedes the MCP-post + cron-poll surface with a **local Slack Bolt app in Socket Mode**:
real Approve/Reject buttons, real-time, **no public endpoint** (Socket Mode dials out to
Slack), runs locally as a LaunchAgent like the ingest jobs.

**Architecture (single gated writer preserved):**
- A small `slack_bolt` (Socket Mode) app — `bin/ticketize_slack_app.py` — runs persistently
  (LaunchAgent `com.example.ticketize-bot`).
- **Posting:** the daily DETECT routine still runs read-only and writes
  `management/standup/<date>/ticket-candidates.md`, then signals the bot (drop a file in a
  watch dir / local trigger). The **bot posts the interactive message** to `#track-work`
  (`<channel id>`) — one Block Kit section per open candidate with `Approve` / `Reject`
  buttons, plus a bulk `Approve all`. `action_id` carries `{date, fingerprint}`.
  (The MCP send tool can't post Block Kit; the Bolt app posts.)
- **Click handler = thin trigger, NOT a Jira writer.** On click the bot: (1) verifies the
  clicker's Slack id == owner (else ignore), (2) records the decision into the candidate md
  / a decision queue, (3) **invokes the existing gated apply** — headless
  `claude -p "/ticketize apply <date>"` (or a local `apply` runner that runs the same skill
  steps). The bot never calls Jira directly — the skill stays the one writer.
- **Feedback:** on completion the bot edits the message / replies in-thread with the created
  `EX-NNNN` (or the failure), and disables the acted buttons.

**Safety:** owner-only interactivity (verify `user.id`); idempotent via fingerprints; click
= the human gate (≡ file edit / Slack reply); fail-loud if apply errors; still never creates
a CMR; reporter≠assignee preserved.

**Owner must provision (I can't — needs workspace admin / token issuance):**
1. Create a Slack app (manifest provided), **enable Socket Mode**.
2. Scopes: `chat:write`, `commands` (optional), `channels:history`/`groups:history` for the
   channel; event subscriptions not needed (Socket Mode).
3. Install to workspace → **bot token `xoxb-…`** + **app-level token `xapp-…`**
   (`connections:write`).
4. Add the app to `#track-work`.
5. Put tokens in `~/.secrets/ticketize_slack.env` (gitignored); the LaunchAgent loads them.

**Build (after tokens exist):** `bin/ticketize_slack_app.py` (Bolt Socket Mode: post-with-
buttons + click→trigger-apply), the LaunchAgent plist, a `--post <date>` entrypoint the DETECT
routine calls, and a manifest doc. v1.5b parser stays as the typed-reply fallback.

**Fallback if the bot is down:** the DETECT md + `/ticketize apply` (manual) and `/ticketize
reply` (typed) paths still work — the bot is an additive surface, not a single point of failure.

## Files (planned)
- `.claude/commands/ticketize.md` — the skill (detect + apply)
- `state/ticket_candidates.json` — fingerprint + created-ID state
- `management/standup/<date>/ticket-candidates.md` — proposal/checker file
- (v2) `.claude/commands/ticket-fix.md` — 🟢 mechanical fix → draft PR
- reuses: `bin/standup_gather.py`, `/trd-build`, `/pr-from-trd`

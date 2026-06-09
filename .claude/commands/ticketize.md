Turn standup-detected work-gaps into tracked Jira tickets through an explicit
maker-checker gate. DETECT proposes (Jira read-only); the owner approves in a
file; APPLY creates the tickets (the only Jira-write step). Sibling of `/standup`,
deliberately SEPARATE — `/standup` stays read-only and is never modified by this.
Owner-invoked. PRD: `prd/ticketize-pipeline.md`.

## Usage — `/ticketize [window] | apply [date]`

If invoked with `help` / `-h` / `--help`: print this Usage block and STOP.

- `/ticketize [window]` — **DETECT** (maker). Read-only on every source incl. Jira.
  Proposes candidate tickets for work that has no ticket. Writes a proposal file.
  - `window` — DEFAULT = yesterday (today-1d 00:00 IST → today 00:00 IST). Accepts a
    day (`2026-06-08`), `last N days`, `this week`. Same window grammar as `/standup`.
- `/ticketize apply [date]` — **APPLY** (checker→create). The ONLY step that writes
  to Jira. Reads the proposal file for `date` (DEFAULT = today's detect run), creates
  every candidate marked `decision: approve`, writes the new key back. Idempotent.

Examples: `/ticketize` · `/ticketize 2026-06-08` · `/ticketize apply 2026-06-08`

This is **Layer 1** of the PRD pipeline (Jira only). The code/PR layer (`/trd-build`,
`/pr-from-trd`, a future `/ticket-fix`) is OUT OF SCOPE here — do not write code or
open PRs from this skill.

---

## 0. Hard constraints (read first)

- **Two-phase maker-checker.** DETECT never writes to Jira. APPLY is the only writer,
  and only for candidates the owner explicitly marked `approve`. No auto-create.
- **Roster = `scope: team` reports** (same as `/standup`): the `scope: team` entries in
  `work-context/config/people.yaml`, EXCLUDING the owner/manager (`standup_gather.py`
  drops `owner_handle()`). A non-roster assignee in a candidate is a bug.
- **Backlog only.** Every created ticket lands in the backlog (no sprint field). The
  owner promotes to a sprint manually. Never set sprint/board.
- **Conservative detection.** False positives are costly (noise). When unsure whether
  a signal is real net-new work, DROP it. Under-propose. The human gate is mandatory,
  not a safety net for over-eager proposals.
- **Never invent work.** A candidate must trace to a concrete artifact (a PR/commit
  with no ticket, or a dated Slack ask). No "they should probably also…".
- **Idempotent.** The same gap is never re-proposed or double-created across runs —
  enforced by `bin/ticketize_state.py` fingerprints. Trust `prior_status`.
- **Read-only on events.db / Slack / GitHub / Confluence.** Jira: read in DETECT,
  write ONLY in APPLY.

---

## 1. DETECT — propose candidates (maker, read-only)

### 1a. Gather (reuse the standup gather — do NOT hand-query)
```bash
python3 bin/standup_gather.py <YYYY-MM-DD> team
```
It already emits, per roster report: window jira (assignee-resolved), window
github (PRs/commits), confluence, current board state, Slack authored in window,
and open @-asks over 7d. That is the full raw surface — read it, don't re-query.

### 1b. Detect ticketable gaps (judgement — this is the model's job)
Scan the gather output for work that has **no Jira ticket**. Two signal classes:

**A. Adhoc work, no ticket.**
- A PR/commit (window github) whose title/branch cites no `EX-NNNN`, AND which does
  not obviously map to one of that member's in-progress board tickets (§1c match).
- A substantive Slack thread the member drove (debugging, prod support, a root-cause
  post, a design decision) that produced real work but references no ticket/CMR.
- Heuristic: a one-off "checking", "looking", or a question is NOT a ticket. A
  multi-message effort, a posted fix/PR, or a concluded root-cause IS.

**B. Future ask / commitment, no ticket.**
- A Slack message directed at the member (`<@their_id>` or subteam ping) asking them to
  **take on new work** — "can you pick this up", "please build/add/create X", "next
  sprint we should…", "raise it to <team>" — that cites no existing ticket.
- A first-person commitment by the member — "I'll pick up X", "we'll need to add Y" —
  with no ticket.
- A pure status ask ("any update?", "is this resolved?") is NOT a candidate — that's a
  `/standup` Up-next item, not net-new tracked work.

### 1c. Suppress what's already tracked (before proposing)
For each surviving gap, rule it out if ANY holds:
- It cites or clearly maps to an existing ticket the member already has (match the gap
  text against their in-progress / to-do board titles from the gather — same feature
  noun = already tracked).
- A Jira search shows a ticket already exists. Search the EX project by the PR link
  and by the 3–4 strongest keywords from the gap:
  - `mcp__plugin_atlassian_atlassian__searchJiraIssuesUsingJql` with a `summary ~`
    / `text ~` JQL, scoped `project = EX`.
  - If a plausible match exists, DROP the candidate (note it as "already: EX-NNNN").
- It is ops noise already covered by a CMR (TB-diff/rectification threads → CMRs exist).

### 1d. Fingerprint + dedupe across runs (deterministic)
Build a JSON array of the surviving gaps and pipe through the state helper:
```bash
echo '<seeds-json>' | python3 bin/ticketize_state.py annotate --date <YYYY-MM-DD>
```
Each seed: `{"person","summary","link"}`. The helper adds `fingerprint`,
`prior_status` (new|proposed|created|rejected) and `prior_jira_key`.
- `prior_status == created` → DROP (already a ticket; show its key for reference).
- `prior_status == rejected` → DROP (owner already said no).
- `prior_status == proposed` → keep, but carry the existing fingerprint (re-surfacing
  an un-actioned proposal is fine; it's the same row).

### 1e. Draft each candidate (full pre-fill, guesses marked)
For each kept gap, pre-fill all fields so approval is one edit:
- **summary** — imperative, specific, ≤ ~12 words (reworded from the evidence, not a
  raw Slack quote).
- **type** — `Task` (default) | `Bug` (a defect/failure) | `Sub-task` (clearly under a
  parent epic). Never `Epic`, never `CMR` (CMRs are ops, raised by their own flow).
- **assignee** — the roster member, by `canonical`. Resolve the Jira accountId from
  their `jira_id` in `people.yaml` at APPLY time.
- **epic** — best-guess parent epic key from the member's current board (the epic their
  related in-progress tickets sit under), tagged `(guess)`. Blank if unclear.
- **placement** — always `backlog`.
- **code_tier** — `🟢 mechanical` | `🟡 feature` | `🔴 never-auto` (money/ledger/
  cross-service). Informational only in Layer 1 — it does NOT trigger any code action
  here; it's the hand-off hint for a future code layer. Default 🔴 when unsure.
- **evidence** — the PR URL and/or Slack thread permalink the gap came from.
- **decision** — `pending`.

### 1f. Write the proposal file
Write `management/standup/<date>/ticket-candidates.md` (mkdir -p; same-day re-run
overwrites in place — but PRESERVE any `decision:` the owner already set: re-read the
existing file first and carry forward decisions/keys by fingerprint).

Format — one block per candidate, with a YAML-ish front so the owner edits one line:
```markdown
# Ticket candidates — <date>  (DETECT run)
_Maker: Claude (read-only). Checker: edit `decision:` to approve|reject, then run
`/ticketize apply <date>`. Backlog-only. Nothing is created until you apply._

## C1 · Bob Example
- decision: pending        # approve | reject | pending
- summary: Decouple IFT freeze-check into its own validator
- type: Task
- assignee: bob-example
- epic: EX-2590 (guess)
- placement: backlog
- code_tier: 🟡 feature
- fingerprint: a1b2c3d4e5f6
- evidence: https://github.com/example-org/service-a/pull/735
- why: 6 commits on PR #735 incl. "decouple freeze check" / "remove unused code",
  no EX ref on the PR and no matching board ticket.
---
```
End the file with a one-line tally: `N candidates · pending N · (already-tracked M dropped)`.

### 1g. Chat reply (DETECT)
Bottom-line first, owner's preferred style. List each candidate in one line
(`C1 Bob — decouple IFT freeze-check → Task, epic EX-2590 (guess) [🟡]`), say how
many were dropped as already-tracked, and end with the next step:
`Edit decision: in management/standup/<date>/ticket-candidates.md, then run /ticketize apply <date>.`

---

## 2. APPLY — create approved tickets (the only Jira-write step)

Owner-invoked: `/ticketize apply <date>`.

1. Read `management/standup/<date>/ticket-candidates.md`.
2. Take ONLY candidates with `decision: approve`. If none, say so and STOP.
3. Re-run the dedupe guard (annotate) to be safe: skip any whose `prior_status`
   is already `created` (idempotent — never double-create).
4. For each approved candidate, resolve the assignee accountId from `people.yaml`
   (`jira_id` of the matching `canonical`; if missing, create unassigned + flag).
5. Create the ticket:
   - `mcp__plugin_atlassian_atlassian__createJiraIssue`
   - project = `EX`, issuetype = the candidate `type`, summary = candidate summary,
     assignee = resolved accountId, parent/epic = candidate epic if present and valid.
   - description = the `why` clause + an **Evidence** line with the PR/Slack link +
     `Auto-proposed by /ticketize on <date>` provenance footer.
   - Backlog: do NOT set sprint/board.
6. Capture each new `EX-NNNN`.
7. Persist: build the decided JSON (with `fingerprint`, `decision`, `jira_key`) and
   commit:
   ```bash
   echo '<decided-json>' | python3 bin/ticketize_state.py commit --date <date>
   ```
8. Write the new keys back into `ticket-candidates.md` (set `decision: created` and add
   `jira_key: EX-NNNN` on each created block) so the file is the audit record.

### 2a. Chat reply (APPLY)
Bottom-line first. List `created: EX-NNNN — <summary> → <assignee>` per ticket, note
any skipped (already created / unresolved assignee / rejected), and end with the next
step: `Promote to a sprint in Jira if needed — all created in backlog.`

---

## 3. Maker-checker — why it's safe (do not weaken)

- **Maker** = Claude in DETECT — proposes only, zero Jira writes. Separation of duties:
  the proposer cannot create.
- **Checker** = the owner — the `decision:` edit in the file IS the approval. Durable,
  auditable, survives across sessions.
- **Apply** = a distinct, explicitly-invoked step — the only writer, idempotent, and it
  records provenance into every ticket.
- Detection is conservative and deduped, so the daily signal cannot spam the backlog.
- `/standup` is never touched — the read-only daily digest and its cron are unaffected.

## Hard constraints (recap)
- DETECT read-only; APPLY writes Jira only for `approve` rows. No auto-create.
- Roster = `scope: team` reports only; assignee must be a roster member.
- Backlog only; never set sprint. Never create Epics or CMRs.
- Idempotent via fingerprints; never double-create.
- No code, no PRs — Layer 1 is Jira-only.

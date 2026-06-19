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
- **Roster = `scope: team` reports** — per `.claude/shared/roster-identity.md` (roster
  definition, identity set, manager exclusion). Ticketize specifics: the owner/manager is
  excluded (`standup_gather.py` drops `owner_handle()`); a non-roster assignee in a
  candidate is a bug; resolve the assignee accountId from the matched `canonical`'s
  `jira_id` at APPLY time (shared §6).
- **Attach to the latest active sprint.** Every created ticket goes into the current
  active sprint (resolve dynamically — never hardcode a sprint id). Devs reprioritise at
  planning. (The Tech-Misc fallback **epic** itself carries NO sprint.)
- **Fallback epic = Tech-Misc catch-all** (`EX-2882` "Tech Misc — Engineering BAU &
  untracked work"). When a candidate has no real/confident epic, parent it here; devs
  reattach to the correct epic + add story points at planning. Never block a create for a
  missing epic — use the fallback.
- **Environment defaults to `PROD`** on any issue type that requires it (override per candidate).
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
**Resolve a yaml-capable interpreter first** (the interactive shell may resolve a bare
`/usr/bin/python3` without `pyyaml`; cron is fine because its PATH puts homebrew first).
Use `$PY` for every python call in this skill:
```bash
PY=$(for p in /opt/homebrew/bin/python3 python3 /usr/local/bin/python3; do "$p" -c 'import yaml' 2>/dev/null && { echo "$p"; break; }; done)
$PY bin/standup_gather.py <YYYY-MM-DD> team
```
It already emits, per roster report: window jira (assignee-resolved), window
github (PRs/commits), confluence, current board state, Slack authored in window,
and open @-asks over 7d. That is the full raw surface — read it, don't re-query.

**Resolve the on-call (config-driven, same source as `/standup` §6).** Read
`work-context/config/oncall.yaml`, query Opsgenie for the current on-call, map the
email → roster `canonical`. The on-call already carries a **standing 5-SP placeholder
ticket** that absorbs their ops/triage/CMR load — so their work is pre-tracked. Hold
the on-call canonical for the §1c suppression.

### 1b. Detect ticketable gaps (judgement — this is the model's job)
Scan the gather output for work that has **no Jira ticket**. Four signal classes
(A–B = untracked work; C–D = CMR ↔ board coherence):

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

**C. CMR with no tracking board ticket.**
Pull the window/open CMRs (`issuetype = CMR`) from the gather board state (and a Jira
read for their `issuelinks`). A CMR is a prod rectification — many are intentional
**one-off data fixes** that correctly need no board ticket. But a CMR that fixes a
**code/config defect** or points at a **recurring root cause** should have a Bug/Task
tracking the underlying fix, and often doesn't.
- Flag a CMR as needing a board ticket only when ALL hold:
  1. it implies a code/config change or a recurring/repeatable root cause (NOT a pure
     one-off data correction — e.g. TB-diff insert, missing-txn backfill, GL balance
     poke with no code path);
  2. it has no linked Jira issue (`issuelinks` empty / no "relates to / caused by"
     Bug/Task), and
  3. no existing board ticket matches its summary (§1c search).
- Propose a `Bug` (the defect) or `Task` (follow-up hardening) — assignee = the CMR's
  assignee — and at APPLY time **link it to the CMR** (`relates to`). `source: cmr-no-ticket`.
- Do NOT propose for pure one-off data rectifications — those are correctly CMR-only.

**D. Release with no CMR (missing release record).**
A CMR **is** the release/rollout record — a prod change with no CMR is a
traceability/compliance gap. Detect release signals in the window:
- a PR merged to a prod/release branch, or a ticket moved to `Released` / `Pending
  Release` / `Released with Emergency`;
- a Slack "released to prod" / "deployed" / "rolled out" post by a roster member.
For each, check whether a CMR covers it (a CMR in/near the window referencing the same
change/PR, status `Change Approved` / `Implementation Reviewed` / `Released`). If NONE:
- Flag a **Missing CMR for release**. `source: release-no-cmr`.
- **NEVER auto-create the CMR** — raising a CMR is a controlled ops process owned by the
  releaser/on-call. The candidate is at most a `Task: "Raise CMR for <release>"`
  assigned to the releaser (or current on-call), or a pure flag if you'd rather not
  create a Task. The human raises the actual CMR.

### 1c. Suppress what's already tracked (before proposing)
For each surviving gap, rule it out if ANY holds:
- **It is attributed to the on-call person** (assignee resolves to the on-call canonical
  for the window). Their ops/triage/CMR work is pre-tracked by the standing 5-SP oncall
  placeholder ticket — DROP it (note as "dropped: on-call"). This applies to all four
  signal classes, incl. class-D release-missing-CMR landing on the on-call.
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
echo '<seeds-json>' | $PY bin/ticketize_state.py annotate --date <YYYY-MM-DD>
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
- **reporter** — the roster member whose stream surfaced the gap (always set this).
- **assignee** — the OWNER of the work, which is NOT automatically the reporter:
  - class-A adhoc *work* (they authored the PR/commit/effort) → assignee = that member.
  - class-C CMR-no-ticket → assignee = the CMR's assignee.
  - a **reported defect / support case** (member flagged a problem, didn't do the work)
    → that member is the **reporter only**. Set assignee to the owning team/person if
    known; if the owner is **another team or unknown, leave assignee BLANK** and add
    `route_to:` with the suspected owning team + a one-line routing note. NEVER assign a
    reported bug to its reporter just because they raised it.
  Resolve the Jira accountId from `jira_id` in `people.yaml` at APPLY time.
- **epic** — best-guess parent epic key from the member's current board (the epic their
  related in-progress tickets sit under), tagged `(guess)`. If none is confident, use the
  Tech-Misc fallback epic `EX-2882`.
- **placement** — `active-sprint` (the apply step resolves the current active sprint).
- **code_tier** — `🟢 mechanical` | `🟡 feature` | `🔴 never-auto` (money/ledger/
  cross-service). Informational only in Layer 1 — it does NOT trigger any code action
  here; it's the hand-off hint for a future code layer. Default 🔴 when unsure.
- **source** — `adhoc-work` (A) | `future-ask` (B) | `cmr-no-ticket` (C) |
  `release-no-cmr` (D). Drives rendering (§1f) and the APPLY action.
- **links_cmr** — for class C: the CMR key to link (`relates to`) on create. For D:
  the release evidence (PR/ticket) the missing CMR should cover.
- **evidence** — the PR URL and/or Slack thread permalink the gap came from.
- **decision** — `pending`.

Type rule still holds: never `Epic`, never `CMR`. Class C → `Bug`/`Task`; class D →
at most `Task` (raise-CMR reminder), never an auto-created CMR.

### 1f. Write the proposal file
Write `management/standup/<date>/ticket-candidates.md` (mkdir -p; same-day re-run
overwrites in place — but PRESERVE any `decision:` the owner already set: re-read the
existing file first and carry forward decisions/keys by fingerprint).

Format — one block per candidate, with a YAML-ish front so the owner edits one line:
```markdown
# Ticket candidates — <date>  (DETECT run)
_Maker: Claude (read-only). Checker: edit `decision:` to approve|reject, then run
`/ticketize apply <date>`. Created tickets attach to the latest active sprint; epic
defaults to the Tech-Misc fallback `EX-2882` if none. Nothing is created until you apply._

## C1 · Bob Example
- decision: pending        # approve | reject | pending
- summary: Decouple IFT freeze-check into its own validator
- type: Task
- assignee: bob-example
- epic: EX-2590 (guess)
- placement: active-sprint
- code_tier: 🟡 feature
- fingerprint: a1b2c3d4e5f6
- evidence: https://github.com/example-org/service-a/pull/735
- why: 6 commits on PR #735 incl. "decouple freeze check" / "remove unused code",
  no EX ref on the PR and no matching board ticket.
---

## CMR gaps
### Needs board ticket
## G1 · Carol Example
- decision: pending
- summary: Fix root cause behind GL posting rectification
- type: Bug
- assignee: carol-example
- epic: EX-2660 (guess)
- placement: active-sprint
- code_tier: 🟡 feature
- source: cmr-no-ticket
- links_cmr: EX-2869        # link `relates to` on create
- fingerprint: ...
- evidence: https://example.atlassian.net/browse/EX-2869
- why: CMR EX-2869 rectified a recurring GL posting drift; no Bug/Task tracks the code
  root cause and no `issuelinks` on the CMR.
---
### Release missing CMR  (flag — CMR is raised by a human, never auto-created)
## G2 · Dave Example
- decision: pending
- summary: Raise CMR for the EX-2846 prod release
- type: Task
- assignee: dave-example        # releaser / current on-call
- placement: active-sprint
- source: release-no-cmr
- links_cmr: (none — that's the gap)
- fingerprint: ...
- evidence: https://github.com/example-org/service-a/pull/742
- why: PR #742 merged + EX-2846 → Released on <date>, but no CMR references this change.
  Traceability gap. Raising the CMR itself is manual.
---
```
End the file with a one-line tally:
`N work candidates · M CMR-gaps (K needs-ticket, L missing-CMR) · pending P · (already-tracked Q dropped)`.

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
5. **Discover required fields first** (don't hardcode IDs — projects differ and this
   file is org-generic). Call `getJiraIssueTypeMetaWithFields` for the project + the
   candidate's issue type and honor every `required: true` field. Common gotchas seen
   in practice:
   - a **mandatory parent Epic** on Bugs/Tasks → set `parent` to the candidate `epic`;
     if `epic` is blank/guess, use the **Tech-Misc fallback epic `EX-2882`** (never
     block, never invent a feature epic).
   - **required custom fields** (e.g. an `Environment` select) → default `Environment` to
     `PROD` unless the candidate says otherwise. Pass via `additional_fields`.
6. **Resolve the latest active sprint** (don't hardcode an id):
   ```
   searchJiraIssuesUsingJql  jql="project = EX AND sprint in openSprints()"  fields=["customfield_10010"]  maxResults=1
   ```
   From `customfield_10010`, pick the entry with `state == "active"` and the latest
   `startDate`. That is the sprint id to attach. If none is active, fall back to backlog
   (and say so).
7. Create the ticket:
   - `mcp__plugin_atlassian_atlassian__createJiraIssue`
   - project = `EX`, issuetype = the candidate `type`, summary = candidate summary,
     assignee = resolved accountId, parent = candidate epic (or `EX-2882` fallback),
     + required fields from §5 (`Environment: PROD` default).
   - **Sprint**: set `customfield_10010` = the active sprint id from §6 (via `additional_fields`).
   - description = the `why` clause + an **Evidence** line with the PR/Slack link +
     `Auto-proposed by /ticketize on <date>` provenance footer.
   - Do NOT set story points — devs add at planning.
   - **Class C (`cmr-no-ticket`)**: after create, link the new issue to each `links_cmr`
     key. Discover the link type first (`getIssueLinkTypes`) and use the neutral
     relate-style one — on this instance there is **no "Relates" type; use `Associated`**
     (`createIssueLink type=Associated`). Don't hardcode "relates to".
   - **Class D (`release-no-cmr`)**: only ever create the `Task` reminder — NEVER create
     a CMR. If the candidate is flag-only (no Task), skip creation; it stays a surfaced gap.
8. Capture each new `EX-NNNN`.
9. Persist: build the decided JSON (with `fingerprint`, `decision`, `jira_key`) and
   commit:
   ```bash
   echo '<decided-json>' | $PY bin/ticketize_state.py commit --date <date>
   ```
10. Write the new keys back into `ticket-candidates.md` (set `decision: created` and add
   `jira_key: EX-NNNN` on each created block) so the file is the audit record.

### 2a. Chat reply (APPLY)
Bottom-line first. List `created: EX-NNNN — <summary> → <assignee>` per ticket, note
any skipped (already created / unresolved assignee / rejected), and end with the next
step: `Promote to a sprint in Jira if needed.`

---

## 2.5 APPLY FROM SLACK — reply-driven gate (v1.5b)

`/ticketize reply [date]` — read the owner's Slack reply on the daily DETECT post and
apply it. The Slack reply IS the maker-checker gate (same role as the file edit). The
parse is DETERMINISTIC and **FAIL-CLOSED**: ambiguous/unknown → apply NOTHING, ask to rephrase.

1. **Find the post + thread.** Locate the latest DETECT post in the ticketize channel
   (`#track-work`, `<channel id>`) whose header is `Ticket candidates — <date>` (default:
   most recent). `slack_read_channel` to find it, `slack_read_thread` for replies.
2. **Owner replies only.** Keep thread replies authored by the **owner** (slack_id from
   `config/people.yaml` owner entry). Ignore anyone else. Take the **newest** owner reply
   first; if it doesn't parse `ok`, try the next older one; if none parse, go to step 6 (help).
3. **Parse (deterministic).** Build the label set from that date's
   `management/standup/<date>/ticket-candidates.md` (C1, G1, …). Then:
   ```bash
   echo '{"reply":"<owner reply text>","labels":[<labels>]}' | $PY bin/ticketize_reply.py
   ```
   - `ok:false` (ambiguous / unknown labels) → **STOP**, apply nothing, post the help (step 6).
   - `approve_all:true` → approve every candidate whose current `decision` is `pending`,
     MINUS any label explicitly in `decisions` as `reject`.
   - else approve/reject exactly the labels in `decisions`.
   - Never approve a label already `created`/`rejected`/`cancelled` (idempotent; trust state).
4. **Apply.** For approved labels, run the §2 APPLY steps verbatim (discover required
   fields, resolve active sprint, epic = candidate epic or `EX-2882` fallback,
   `Environment: PROD` default, create, Class-C `Associated` link, commit state, write keys
   back). For rejected labels, commit `decision: reject` to state + mark the md.
5. **Confirm in-thread.** Reply on the SAME thread (`thread_ts` = the post ts): per created
   item `created: <KEY> — <summary> → <assignee> (sprint <name>)`, plus rejected/skipped.
   Link every key.
6. **Help (fail-closed).** If nothing parsed, reply in-thread: "Couldn't parse — reply with
   `apply C1, G2` / `reject C3` / `approve all` (optionally `approve all except C1`)."

**Safety (do not weaken):** only the owner's reply counts; deterministic parse, fail-closed;
idempotent via fingerprints; still never creates a CMR (Class D stays a Task/flag); reported
defects still aren't auto-assigned to the reporter. A reply naming items is the human gate —
exactly equivalent to the file `decision:` edit, just via Slack.

**Triggering:** run `/ticketize reply <date>` manually, OR (opt-in) a second scheduled task
that runs it on a delay after the DETECT post. Auto-scheduling means cron writes Jira once you
reply — acceptable because your reply is the gate, but enable it deliberately, not by default.

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
- Attach every created ticket to the latest active sprint (resolved dynamically). Parent
  to its epic, or the Tech-Misc fallback `EX-2882` if none. `Environment: PROD` default.
  No story points (devs add). Never create CMRs; only create an Epic on explicit request.
- CMR coherence (C/D): propose a Bug/Task for a CMR that lacks a tracking ticket; FLAG a
  release that lacks a CMR — but never auto-raise the CMR (human-owned ops process).
- On-call is exempt: drop every candidate assigned to the current on-call — their work
  is pre-tracked by the standing 5-SP oncall placeholder ticket.
- Reporter ≠ assignee. The member who surfaced a reported defect is the reporter; never
  auto-assign it to them. Cross-team / unknown owner → leave assignee blank + `route_to:`.
- Idempotent via fingerprints; never double-create.
- No code, no PRs — Layer 1 is Jira-only.

# Pushing the sprint board into Jira

**Built, and run against the real board.** This was the plan; what follows is kept
because the reasoning still holds, with the decisions now settled and the places the
implementation went further marked inline. `derive/jira_push.py`, `/jira-push`.

What the plan did not anticipate, all found by using it:

- **The window has to be enforced here.** The board plan is one list across every sprint,
  so a preview of one fortnight listed every scheduled tile on the board and would have
  put the lot into whichever sprint the window matched.
- **Sprints are matched on date overlap, in every state.** The sprint being planned has
  usually not started, and the name a team gives it has nothing to do with one derived
  from dates. Matching on name, or on exact dates, reported "none" for a sprint sitting
  right there and offered to create a second one over the top of it.
- **Most tiles are not new work.** They are shorthand for a ticket that already exists
  ("AA FIP" for "AA FIP: PAN + DOB discovery"), so matching is by containment rather than
  shared words, and a linked ticket supplies its own epic. That turned out to matter more
  than guessing an epic, which was the original plan's focus.
- **Oncall is not a tile.** It is `O` in the capacity calendar, already subtracted from
  capacity, so the push derives one ticket per unbroken stint instead.
- **Nothing may be guessed.** The issue type defaulted to one and created fifteen tickets
  as Story where the team wanted Task. A missing setting is now a refusal.

---

The original plan follows. The board is a planning surface; this is about making it
produce a real sprint with real tickets, once, safely, and repeatably.

## What already exists

Almost all the plumbing is there. This is mostly wiring, plus two decisions only you can
make.

- `capacity_engine._jira(method, path, body)` already does authenticated POST and PUT.
  Creating an issue through it is a pattern already in use (`POST /rest/api/3/issue`
  creates delivery epics for initiatives).
- The board id, project key, story point field, sprint field and epic issue type all come
  from `config/sprint_planning.yaml`. Nothing is hardcoded.
- `config/people.yaml` carries each person's `jira_id`, which is the assignee accountId.
- `config/ticketize.yaml` already establishes the house conventions for creating tickets
  from a non-Jira source: a project, a fallback epic for work that belongs nowhere else,
  the sprint field, and an owner-approval loop before anything is written.
- The board plan is one durable server-side store (`state/sprint_board.json`), already
  versioned, so a push can record what it did.

## What the board has, and what Jira needs

A board task is `{id, key, title, cat, who, date, off, days, sp, adhoc, tint}`.

Maps cleanly:

- `title` to summary.
- `who` to assignee, via `people.yaml` `jira_id`.
- `sp` to story points. In effort mode this is the real estimate. In days mode it is
  absent, and `days * efficiency` is the honest conversion, which is what the board
  already shows.
- `key` to an existing issue. Present means update, absent means create.
- The window (`from`/`to`, or the selected sprint) to the sprint's start and end dates.

Does not map, and needs a decision:

- `cat` is a free text heading you typed ("TBM", "CASA", "Tech Debt/Oncall"). Jira wants
  an epic. There is no mapping today.
- `date` and `off` are a quarter-day position. Jira has no equivalent. A sprint is the
  finest grain Jira will hold unless start and due dates are written per issue.

## Decisions needed

**1. How a task finds its epic.** Three options.

- *Category to epic, mapped once.* You pick an epic per backlog heading, stored with the
  plan. "TBM" becomes one epic for every task under it. Cheapest to use, and it matches
  how you already think about the backlog. Recommended.
- *Epic per task.* Most precise, most clicks, and most of them repeat the one above.
- *Fallback epic only.* Everything lands in the BAU epic, the one the duplicates were
  consolidated into. Fine as the default for anything unmapped, wrong as the only
  behaviour.

Recommendation: category to epic, with the fallback epic for anything unmapped, and the
push refuses to run while any category is unmapped unless you explicitly accept the
fallback.

**2. Issue type for created tickets.** Task or Story. `ticketize` already has a house
answer for this; reuse it rather than introduce a second convention.

**3. Whether a push may change tickets that already exist.** A task carrying a `key` is an
existing ticket. Writing assignee, story points and sprint onto it is useful and is also
the only destructive thing in this whole flow. Recommendation: it may set the sprint and
the assignee, and it may set story points only when the ticket has none. Anything it would
overwrite is listed in the preview and needs a separate tick.

**4. Whether per-day positions are written at all.** Recommendation: no, not in v1. The
board's day positions are a planning aid; Jira's sprint is the commitment. Writing start
and due dates per ticket invites a second source of truth that nothing keeps in step.

## The flow

Four steps, and the first two write nothing.

1. **Preview.** A button on the board, next to Save. It reads the current plan and the
   current window and produces a list: the sprint it would create (name, dates, board),
   every ticket it would create (summary, epic, assignee, points), every ticket it would
   change (key, and exactly which fields, with before and after), and everything it is
   skipping and why. Nothing is sent.
2. **Confirm.** You see the list and accept it. Overwrites are a separate tick. A push
   never starts from a page load, a reload, or an autosave.
3. **Apply.** In order: create the sprint, create the missing tickets, set fields, move
   every ticket into the sprint. Each step records what it did before attempting the next.
4. **Receipt.** What was created, what was changed, what failed, with Jira links. Written
   to the plan store and shown on the page, so there is a record that is not a chat
   message.

## Running it twice must be safe

This is the part most likely to hurt, so it is designed first.

- Every ticket created writes its key back into the board task, immediately, one at a
  time. A second run sees a `key` and updates rather than creates.
- If the write back fails after the ticket was created, the next preview would duplicate.
  So before creating, a keyless task is matched against the target epic and sprint by
  exact summary. A match is adopted, not duplicated, and the preview says "adopting <key>" rather than "creating".
- The sprint itself is matched by name on the configured board before being created.
- A failure part way through leaves a partial sprint. That is acceptable and visible; the
  receipt names exactly what landed, and the next preview starts from reality rather than
  from an assumption.

## What it must refuse to do

- Run without an explicit confirmation, every time.
- Create a sprint when one of the same name already exists on the board.
- Create a ticket for a task with no owner. Backlog tiles are not commitments.
- Overwrite a story point estimate someone else set, unless separately accepted.
- Delete or close anything, ever. There is no reverse gear in this flow, and the board
  should not pretend to have one.

## Build order

Each phase is useful on its own.

1. **Mapping and preview.** Category to epic mapping stored with the plan, plus the
   preview, which writes nothing. This is where the thinking is, and it can be reviewed
   against your real board before a single Jira call.
2. **Create and assign.** Sprint creation, ticket creation, assignee, story points, write
   back of keys, receipt.
3. **Update existing.** Moving existing tickets into the sprint, and the guarded field
   updates.
4. **Adoption by summary.** The duplicate guard for the case where a write back failed.

## How it gets tested

Jira writes cannot be exercised against the real instance in a test, so the split matters.

- The decision layer, which is everything that produces the preview, is a pure function of
  the plan plus a snapshot of Jira state. That gets real unit tests: what it creates, what
  it adopts, what it refuses, what it reports as an overwrite.
- The write layer is a thin adapter over `_jira`. It gets a fake that records calls, so
  the ORDER and the exact payloads are asserted without touching Jira.
- One end to end run against a throwaway sprint on the configured board, by hand, with the receipt
  checked against the board, before it is used for a real cycle.

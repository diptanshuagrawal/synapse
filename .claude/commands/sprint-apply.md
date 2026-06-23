# /sprint-apply — execute an accepted sprint plan in Jira

You turn an **accepted** sprint plan into real Jira state through an explicit
maker-checker loop (DETECT → file-approve → apply). No Anthropic API key — you reason
in-session. Sibling of `/ticketize`; reuse its conventions and `derive/jira_metrics.py`.

The planner's **Accept** button snapshots the chosen plan to
`work-context/derived/sprint-plan-accepted.json`. You read that snapshot and make a
sprint "like the plan" happen — creating only what's missing, never clobbering.

## Inputs

1. **`work-context/derived/sprint-plan-accepted.json`** — the accepted snapshot:
   `{ _accepted, source ("claude"|"brain"|"manual"), label, sprint:{label,start,end,…},
   plan:{ people:[{name, cells:[{date,kind,label}]}], backlogPicks:[…], signals, rationale } }`.
   If missing → tell the user to click **✓ Accept plan** on the planner page first, and stop.
2. **`work-context/derived/sprint-dump.json`** — for ticket keys, epics, statuses, the
   per-person `spillover`, `backlogPool`, and each initiative's `epic`/`comments`.
3. **Config** — `config/sources.yaml` (Jira host + project key + owner), and
   `config/sprint_planning.yaml` (`board_id`). Load everything from config — never hardcode
   org identity, project keys, board ids, or names.

## Steps

### 0 · Load + map
Read the accepted snapshot and the dump. Build a label→ticket map: most plan-cell labels
and `backlogPicks` embed a ticket id (e.g. `Deadlock retry (1069)` → ticket #1069, or a label
like `Work item (PROJ-2591)` that names its key directly). Resolve each against the dump's
`spillover` / `backlogPool` /
initiative `epic`. A label with no resolvable key and no matching open ticket = **NEW work**.
Map each person's display name → Jira accountId via `lookupJiraAccountId` + `config/people.yaml`.

### 1 · Derive the action set (NO writes yet)
Walk every `work`/labelled-`oncall` cell and every `backlogPick`, then classify into actions:

- **Sprint** — target = the accepted `sprint`. Query the board's sprints; if none matches the
  label → action **CREATE sprint** (name = sprint.label, start/end from the snapshot, state
  `future`). If it already exists, reuse it.
- **Epics** — each planned initiative maps to an epic via the dump. If an initiative has **no
  epic** (e.g. Narration), this is an action **CREATE epic** — but you need a description, so
  ASK the owner (Step 3). Never invent epic scope text.
- **Tickets — always prefer an existing ticket; CREATE is the last resort.**
  1. *Has a resolvable key* (embedded in the label/pick, or found in the dump's
     `spillover` / `backlogPool` / the mapped epic's children) → it EXISTS → action **UPDATE** only.
  2. *No key* → **SEARCH Jira before assuming it's new.** JQL the project for an open ticket
     whose summary matches the planned work (and/or sits under the mapped epic). If a plausible
     match exists → treat it as EXISTING (UPDATE) — **do NOT create a duplicate.**
  3. *Only when no existing ticket matches* → action **CREATE**: summary from the
     label/initiative, Task default (Bug for defect-category picks), link to its epic. Follow
     `/ticketize` norms: PROD env default, reporter ≠ assignee. **Always set Story Points** on
     created tickets — discover the SP custom field id from an existing SIZED ticket (a project
     may expose two SP-like fields, e.g. "Story Points" vs "Story point estimate"; use the one
     the board actually populates) and carry the plan's SP. New tickets with blank SP are a bug.
- **On-call tickets** — if the plan has on-call days, the team tracks on-call as its OWN ticket.
  Copy the convention from a recent on-call ticket (summary, epic, SP) and create ONE per on-call
  rotation in the sprint (e.g. one per person/week), assigned to that person, added to the sprint.
  Do NOT fold on-call into a feature ticket. (Don't infer the on-call epic/SP — read a real
  prior on-call ticket and mirror it.)
  - **UPDATE** = set `assignee` = planned owner; set `priority`; **backfill Story Points from
    the plan when the ticket's SP is blank** — applies to EXISTING tickets too, not just created
    ones (don't overwrite a non-blank SP; if the plan has no SP for it, leave blank); post a
    footer comment `Reviewer: <name>` when the plan names a reviewer (Jira has no native reviewer
    field). If the ticket already matches, skip it (no-op, note it).
  - If a fuzzy match is ambiguous ("is this the same ticket?"), surface it in the proposal as a
    link-or-create question (Step 3) rather than silently creating.
- **Sprint membership** — add to the target sprint ONLY tickets that are **newly created or
  pulled from backlog**. **NEVER move existing spillover / in-progress tickets** — the owner
  rolls those over by closing the current sprint and starting the next. List them under
  SKIPPED (auto-carries) so it's explicit.

### 2 · Write the proposal + render it
Write the full action set to `work-context/state/sprint_apply_pending.json`, then render a
readable plan in chat, grouped:
`CREATE sprint` · `CREATE epics` (with the exact description you'll use) · `CREATE tickets`
(summary · type · epic · assignee · priority) · `UPDATE` (key → assignee/priority/reviewer-comment)
· `ADD to sprint` · `SKIPPED — spillover (auto-carries)`. Show counts.

### 3 · STOP — checker gate
Do not touch Jira yet. If any epic/new ticket needs a description or an ambiguous owner needs
resolving, ASK 1–3 crisp questions now and wait. Otherwise ask the owner to approve (edit the
pending file or reply **go**). This is a hard gate even under bypass mode (see plan-share rule).

### 4 · Apply (only on explicit go)
Apply in dependency order: create sprint → create epics → create tickets → set
assignee/priority → add new/pulled tickets to the sprint → post reviewer comments. Apply
exactly what's in the (possibly owner-edited) pending file — nothing more.

**Mechanism — two channels:**
- *Ticket create / assignee / priority / comments* → Atlassian MCP **or** authenticated
  browser REST. (Discover project quirks from a real ticket before writing — e.g. some projects
  use lowercase priority names like `p1`/`p2`/`p0`, link the epic via the `parent` field, and
  default the reporter to the caller so reporter ≠ assignee holds.)
- *Sprint create + add-to-sprint* → the MCP has **no Agile API**, so drive Jira's Agile REST
  from the owner's authenticated browser (Claude-in-Chrome): `POST /rest/agile/1.0/sprint`
  `{name, startDate, endDate, originBoardId}` then `POST /rest/agile/1.0/sprint/{id}/issue`
  `{issues:[…]}` with `credentials:'include'` + header `X-Atlassian-Token: no-check`. **Sprint
  name must be < 30 chars** — DON'T use the planner's display label; follow the board's live
  convention (read `…/board/{id}/sprint?state=active,future` and increment, e.g. `… S5`→`… S6`).

### 5 · Report
Summarize every action with its result and a Jira link. State failures plainly; never fake a
success. If a leg couldn't run (permissions, missing field), say so and leave it in the pending
file for retry. Update `sprint_apply_pending.json` to mark applied items.

## Notes
- Maker-checker is mandatory: **no Jira writes before an explicit go.**
- Never move spillover into the new sprint.
- Create-if-missing only: reuse an existing sprint/epic/ticket rather than duplicating.
- Keep this skill generic + config-driven — it is published to the public synapse skeleton.

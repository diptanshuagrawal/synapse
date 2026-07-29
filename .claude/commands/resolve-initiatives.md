---
description: Enrich the sprint planner's initiative skeleton — resolve each initiative's Jira epic, remaining story points, and a factual comment from real ticket states. Reads work-context/derived/initiatives-in.json, writes initiatives-out.json for the planner page to merge. Owner-invoked; uses Jira (no Anthropic API key).
---

# /resolve-initiatives — initiative epic/SP/comment resolver

The sprint planner page lets the owner list initiatives by name. This skill turns
each name into its Jira epic + remaining SP + a grounded comment, so the planner
dump carries correct epics. Pairs with `/sprint-plan` (which does the allocation).

## Steps

1. **Read** `work-context/derived/initiatives-in.json` (the planner wrote it on
   "Resolve via chat"). It has `initiatives: [{name, task, type, assignees, reviewer,
   priority, epic, comments}]`. If missing, tell the user to click **Resolve via
   chat** on the planner page first. If `$ARGUMENTS` names another path, use it.
   A non-empty `task` marks the row as ONE specific work item inside the initiative
   (several rows may share a `name` with different tasks/assignees) — entry identity
   everywhere is **name+task**, and task rows resolve at ticket level (step 2.5).

1.5 **Prefetch — one script call does the diff AND every Jira search.** Run
   `cd work-context && .venv/bin/python derive/resolve_prefetch.py` (append
   `--all` if `$ARGUMENTS` contains `all` / owner says "resolve all"). It writes
   `derived/resolve-prefetch.json`:
   - `unchanged`: previous out-entries whose input fingerprint (`_src` =
     `name|epic|type|assignees`, lowercased) is unchanged — the script already
     handles the **epic-echo** case (the page writes resolved epics back into
     the input; that's not a diff). **Copy these into the output verbatim.**
     Note their Jira SP/comment is from when they were last resolved — the diff
     is on *input*, not Jira state; `--all` is the refresh escape hatch.
   - `resolve`: rows needing judgment, each with the full Jira evidence —
     `epicSearch` (epics whose title matches), `ticketSearch` (matching tickets
     with status/SP/assignee/parent), `candidates` (top parent epics with votes,
     `remainingSP`, `doneRecentSP/N`, and their `open` ticket lists).
   - Out-entries whose name is no longer in the input are dropped.
   - If the script fails, fall back to manual curl per the old flow (JQL POST to
     `/rest/api/3/search/jql`, creds in `~/.secrets/atlassian_*`).

2. **For each `resolve` row, judge the epic from the prefetch evidence** (no
   further Jira calls needed in the common case):
   - If `epic` was already set in the input, keep it (its children are in
     `candidates`).
   - Else pick from `candidates`: the epic the most matching tickets point to
     wins; epic-title matches (`epicSearch`) count extra. **The ticket titles
     bridge aliases** — an acronym in the initiative name often appears in
     ticket titles whose parent epic has a longer, different summary. If
     genuinely ambiguous, pick the strongest and name the runner-up in the
     comment.
   - **Generic-epic guard / ticket-level fallback**: matching tickets sometimes
     sit under a catch-all epic (e.g. a small "Narration fixes" ticket under a
     generic "Prod Misc" epic). If the best parent epic's summary doesn't itself
     relate to the initiative name, do NOT adopt that epic — its remaining SP
     would count a pile of unrelated work. Instead resolve at ticket level:
     leave `epic` empty, set `sp` = sum of the matched tickets' remaining SP,
     list the ticket keys in a `tickets` array on the out-entry, and name the
     tickets + their generic parent in the comment (e.g. "no dedicated epic;
     one live ticket (To Do, 2 SP) under a generic 'Prod Misc' epic").
   - If neither epics nor tickets match, leave `epic` empty (new/unticketed work).

2.5 **Task rows (`task` non-empty) — resolve at ticket level, never whole-epic SP.**
   The prefetch already ran the ticket search on the TASK text (even when the epic
   was preset). Resolve the `epic` from the initiative `name`/preset as in step 2,
   but for `sp`:
   - Match tickets to the task from `ticketSearch` and the chosen candidate's
     `open` list — task text against summaries, the row's `assignees` against
     ticket assignees as a tie-breaker.
   - `sp` = sum of the matched tickets' remaining SP; list keys in `tickets`.
   - Comment from THOSE tickets (status, holder), not the epic totals.
   - Nothing matches → keep the epic, give a rough `sp`, say the task is unticketed.
   - Rows sharing a `name` are separate out-entries — never merge or dedupe them.

3. **Remaining SP**: for a resolved epic, use the candidate's `remainingSP`
   (already summed from `parent = <epic> AND statusCategory != Done`). For a
   ticket-level match (generic-epic fallback), sum the matched tickets' remaining
   SP instead. For an unticketed initiative, give a rough `sp` from the
   name/scope (and say it's an estimate in the comment).

4. **Comment** — short and factual, from the epic's ticket states:
   - open vs near-done split (statuses In Review / QA / Pending Release ≈ near-done),
   - who holds the open tickets, and whether they're on leave (cross-ref the
     planner's people if helpful),
   - recency / staleness, and the runner-up epic if the match was close.
   - Example: "<EPIC-KEY> <epic name> — 12 SP open, 11 in review/pending-release →
     mostly done, just shepherd to release."

5. **Write** `work-context/derived/initiatives-out.json`:
   ```
   { "_generated": "<today>",
     "initiatives": [ { "name", "task", "epic", "epicSummary", "sp", "type", "comment", "_src", "tickets?" } ] }
   ```
   Keep `name` AND `task` **identical** to the input — the page merges by
   name+task (sets `epic`, `comments`, and the manual `sp` for unticketed rows
   AND for task rows, which always size from their matched tickets). Every entry
   (copied or freshly resolved) gets `_src` = its input fingerprint (step 1.5),
   so the next run can diff; ticket-level matches also carry `tickets: [<keys>]`.
   The page ignores `_src` and `tickets`.

6. **Tell the user**: resolved — click **Load resolved** on the planner page.
   Say how many were freshly resolved vs copied unchanged.

## Notes
- Resolution is grounded in Jira (deterministic prefetch + your judgment on ambiguity).
- Don't invent epics; empty is correct when nothing matches.
- The prefetch script batches all searches (~1-2s total); only fire extra JQL
  yourself when the evidence is genuinely insufficient (e.g. need a specific
  ticket's description).
- Leave cross-ref for comments: `work-context/derived/team-leaves.md`.

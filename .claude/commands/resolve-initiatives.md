---
description: Enrich the sprint planner's initiative skeleton — resolve each initiative's Jira epic, remaining story points, and a factual comment from real ticket states. Reads work-context/derived/initiatives-in.json, writes initiatives-out.json for the planner page to merge. Owner-invoked; uses Jira (no Anthropic API key).
---

# /resolve-initiatives — initiative epic/SP/comment resolver

The sprint planner page lets the owner list initiatives by name. This skill turns
each name into its Jira epic + remaining SP + a grounded comment, so the planner
dump carries correct epics. Pairs with `/sprint-plan` (which does the allocation).

## Steps

1. **Read** `work-context/derived/initiatives-in.json` (the planner wrote it on
   "Resolve via chat"). It has `initiatives: [{name, type, assignees, reviewer,
   priority, epic, comments}]`. If missing, tell the user to click **Resolve via
   chat** on the planner page first. If `$ARGUMENTS` names another path, use it.

2. **For each initiative, resolve the epic** (Jira host + project key come from
   `config/sources.yaml`; creds in `~/.secrets/atlassian_email` +
   `~/.secrets/atlassian_token`; use `/rest/api/3/search/jql` POST):
   - If `epic` is already set, keep it.
   - Else search by the initiative `name`: run a ticket text search
     `project = <PROJECT> AND summary ~ "<name>" AND issuetype != Epic` and tally the
     `parent` epics; also `project = <PROJECT> AND issuetype = Epic AND summary ~ "<name>"`.
     **The ticket titles bridge aliases** — an acronym in the initiative name often
     appears in ticket titles whose parent epic has a longer, different summary. Pick
     the epic the most matching tickets point to (epic-title matches count extra). If
     genuinely ambiguous, pick the strongest and name the runner-up in the comment.
   - If nothing matches, leave `epic` empty (new/unticketed work).

3. **Remaining SP**: for a resolved epic, sum story points
   (`customfield_10051`) on `parent = <epic> AND statusCategory != Done`. For an
   unticketed initiative, give a rough `sp` from the name/scope (and say it's an
   estimate in the comment).

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
     "initiatives": [ { "name", "epic", "epicSummary", "sp", "type", "comment" } ] }
   ```
   Keep `name` **identical** to the input — the page merges by name (sets `epic`,
   `comments`, and for unticketed ones the manual `sp`).

6. **Tell the user**: resolved — click **Load resolved** on the planner page.

## Notes
- Resolution is grounded in Jira (deterministic search + your judgment on ambiguity).
- Don't invent epics; empty is correct when nothing matches.
- One Jira round of searches per initiative is fine; batch where you can.

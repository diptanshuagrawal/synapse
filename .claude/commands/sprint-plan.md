---
description: Turn a sprint-planner dump into the final day-by-day plan. Reads work-context/derived/sprint-dump.json (capacity + initiatives + spillover + backlog) and writes work-context/derived/sprint-plan.json for the HTML planner to render. Owner-invoked; no API calls — Claude (this session) IS the analysis layer.
---

# /sprint-plan — sprint plan generator

You are the tactical-analysis layer for the sprint planner. The HTML planner page
(`work-context/derived/sprint-planner.html`, served by `derive/sprint_server.py`)
produces a **dump** of everything; you read it, reason over it, and write **two
plans** the page renders side by side:

- **Plan A — as-specified** → `work-context/derived/sprint-plan.json`. Faithful to
  the owner's inputs: keep their initiative assignees/reviewers, honor their comments
  and selected backlog. This is "did the owner's plan fit?".
- **Plan B — manager rebalance** → `work-context/derived/sprint-plan-brain.json`.
  YOUR opinionated version as an experienced EM: you MAY reassign owners/reviewers,
  rebalance execution vs review load, and re-sequence — whatever makes the strongest
  sprint. Same schema; the page shows it as a separate view.

No Anthropic API key is used — you do this in-session.

**Mindset — plan like an elite engineering-manager tactician.** The dump gives you
five things: **capacity** (`people`), **previous-sprint carry** (`spillover`), the
owner's **forward goals** (`initiatives`), **backlog + the owner's picks**
(`backlogPool` / `selectedBacklog` / `taskRequests`), and the **comments**. Use all
of them. Protect P1 / compliance work; sequence to de-risk the thin part of the
sprint (e.g. when seniors are out early); spend slack deliberately; don't silently
overload one person or pile every review on one reviewer; respect the comments as
direct tactical instructions.

**Ask before guessing.** If a call is genuinely the owner's — unsized fixed work
that has a deadline, over-capacity that needs a descope/reassign decision, an
initiative whose only open tickets sit with someone on leave, an ambiguous or
missing assignee, or a comment you can't interpret — STOP and ask 1–3 crisp
questions in the chat, then wait. Only write the plan once resolved (or the owner
says "use your judgment"). A short clarifying round beats a confidently-wrong plan.

## Steps

1. **Read the dump:** `work-context/derived/sprint-dump.json`.
   It is self-describing — `_instructions`, `_output_schema`, and `_write_to` are
   inside it. If the file is missing, tell the user to click **Save dump for chat**
   on the planner page first. If `$ARGUMENTS` names a different dump path, use that.

2. **Analyze** (the dump's `_instructions` are authoritative; this is the summary):
   - `people` — capacity. `statuses` is one code per day in `days` order:
     `""`=available, `W`=WFH (a WORKING day), `O`=on-call, `L`=leave, `H`=holiday,
     `WE`=weekend. `net`/`sp`/`eff` = net working days / effective story points /
     role efficiency. `spillover` = tickets each person carries from the current sprint.
   - `initiatives` — `fixed` (must finish) vs `continuous` (rolls over, fills spare);
     `assignees`, `reviewer`, `priority` (P1>P2>P3), `effortSP`, `epic`, `comments`.
   - `backlogPool` — every backlog ticket. `selectedBacklog` — owner-picked tickets
     (committed). `taskRequests` — owner's manual asks.
   - **Honor every `comment` tactically.** Examples: "X is ~done, mostly in review"
     ⇒ near-zero residual effort; "solve Y during on-call by <person>" ⇒ place it on
     that person's on-call days; "inherits a leaver's tickets" ⇒ real load on the assignee.
   - **Place work only on available (`""`) or WFH (`W`) days** — never leave/holiday/
     weekend — UNLESS a comment says otherwise (e.g. on-call work).
   - **Spillover reconciliation:** a spillover ticket whose `epic` matches a named
     initiative's `epic` is PART of that initiative (don't double-count). Spillover
     outside any named track is extra committed work — place it on the person's days
     (label it), or flag it as a slip if their days are full.
   - **Allocation order:** fixed initiatives first (priority order), converting
     `effortSP` to days as `round(SP / eff)`; continuous fills remaining days. If a
     person's committed work exceeds available days, place what fits and report the
     rest as a slip in `signals`.
   - **Backlog curation (deterministic backbone):** `backlogPool` is pre-scored by a
     reproducible classifier — every ticket carries `score`, `category`, and `reasons`,
     judged from **title + description + recency + type + status** (priority/SP fields
     are usually blank — do not rely on them). Schedule `selectedBacklog` +
     `taskRequests` first (owner-committed; honor their `assignee`/`sp` overrides and
     never put IC work on the owner). Then take the **top-scoring** pool tickets as
     your default candidates; apply judgment on top — dedupe near-identical items, drop
     stale investigations or poor team-fit, and deviate from score order only with a
     stated reason. Carry each pick's `score` + `category` into `backlogPicks`. Only
     schedule into genuinely free days; if there's no spare capacity, still return the
     top candidates to pull first, each marked `fit:"if room frees"`.

3. **Write Plan A (as-specified)** to `work-context/derived/sprint-plan.json`,
   matching the dump's `_output_schema`:
   ```
   {
     "_generated": "<today> (as-specified)",
     "people":  [{ "name", "cells": [{ "date", "kind", "label" }] }],   // one cell per day in `days` order
     "backlogPicks": [{ "key", "title", "sp", "priority", "score", "category", "suggestedAssignee", "fit", "reason" }],
     "signals": [{ "level": "danger|warn|ok|info", "text" }],           // text may contain light <b> HTML
     "rationale": "2-5 sentences on the key calls + how comments/spillover/backlog were applied"
   }
   ```
   `cells[].kind` ∈ `work|leave|oncall|wfh|holiday|weekend|idle`; `label` is what they
   work on for `work`/shared-`oncall` cells (≤24 chars), else `""`.

4. **Build Plan B (manager rebalance)** and write it to
   `work-context/derived/sprint-plan-brain.json` (same schema). Here you act as the
   EM, not a transcriber:
   - **Rebalance load.** If one person is execution-overloaded (e.g. on 3 initiatives)
     or one reviewer is a bottleneck (reviewing most of the work), move work/reviews to
     even it out — respecting role/tier (SDE1/2/3 from `people`).
   - **Match work to who actually knows it.** Consult the DB before reassigning:
     `config/people.yaml` (roles), `config/domain_team_map.yaml` /
     `config/team_subteams.yaml` (domains), the `actor_behavior` view and `events.db`
     (who ships/reviews what), `cluster_project_map` (slug↔owner), and the
     code-review-graph MCP (`query_graph` callers/owners, `semantic_search_nodes`) for
     code ownership. Prefer the person with real history in that area.
   - **Never assign IC delivery to the owner.** Keep reviewers off their own code.
   - **Explain every change** in `signals` (level `info`): one line per reassignment —
     "Moved <track> from <person A> → <person B>: B owns the adjacent migration and A is
     already on 3 initiatives." Put the philosophy in `rationale`.
   - If the rebalance is materially better, say so; if Plan A is already well-balanced,
     Plan B can be close to it — but still your honest call.

5. **Tell the user:** both plans written — click **Load plan** to render them
   (as-specified + your rebalance, side by side).

## Notes
- Keep `cells` aligned to `days` order, one per day, for every person in `people`.
- Mirror the engine's status codes exactly; the page colors cells by `kind`.
- This is read-dump → write-plan only. Don't touch the engine, server, or page.

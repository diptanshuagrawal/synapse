---
description: Turn a /plan roadmap-sandbox dump into a month-by-month plan. Reads work-context/derived/plan-dump.json (multi-month capacity + pod initiatives + per-initiative Target SP) and writes plan-A.json (as-specified) + plan-B.json (manager rebalance) for the /plan page to load into its scratch allocation + Gantt. Scratch only — never touches Jira. Owner-invoked; no API calls — Claude (this session) IS the analysis layer.
---

# /monthly-plan — roadmap-sandbox plan generator

You are the planning-analysis layer for the **/plan roadmap sandbox** (`/plan`, served by
`derive/sprint_server.py` from `work-context/derived/plan.html`). It's a **what-if** — it
sizes initiatives and packs them across months, and **never touches real Jira budgets**. The
page produces a **dump**; you read it, reason over it, and write **two plans** the page loads
into its scratch allocation (which drives the Gantt):

- **Plan A — as-specified** → `work-context/derived/plan-A.json`. Faithful to the owner's
  inputs: honor the stack-rank (`rank`, 1 = top) and each initiative's DRIs. This is "did the
  owner's ordering fit the months?".
- **Plan B — manager rebalance** → `work-context/derived/plan-B.json`. YOUR opinionated
  version as an experienced EM: you MAY re-sequence across months and rebalance DRI load —
  whatever makes the strongest quarter. Same schema; the page shows it behind an
  **As-specified ⇄ Rebalance** toggle.

No Anthropic API key is used — you do this in-session.

## The one hard rule — SEQUENCING ONLY

Each initiative arrives with a **`targetSP`** (the total the owner wants to spend on it).
**Never change an initiative's `targetSP`.** Your only job is to decide **how that total is
split across the selected months** — which month(s) the work lands in. The sum of an
initiative's per-month allocations must equal its `targetSP` (any remainder that doesn't fit
the window goes to `overflow`, it is NOT dropped and NOT squeezed into a full month).

## Mindset — plan like an EM sequencing a quarter

The dump gives you three things: **capacity** (`capacity[].teamSP` per month + per-person
`net`/`sp`/`eff`/`leave`/`oncall`), the owner's **stack-ranked initiatives**
(`initiatives[]` with `rank`, `orgPriority`, `engDri`/`prodDri`, `targetSP`, `currentAlloc`),
and the **month window** (`monthNames`, in order).

- **Respect the month capacity ceiling** (`teamSP`). Fill a month toward its `teamSP`; only
  spill into the next month once it's reasonably full. Don't blow past a month's `teamSP`
  unless EVERY month in the window is full — then the remainder is `overflow`.
- **Protect priority.** High `orgPriority` (P0/P1/compliance/regulatory) initiatives get the
  earliest months and are the last to overflow. Rank breaks ties.
- **Watch DRI load.** Don't stack a single Eng-DRI's initiatives all into one month — a DRI
  can only really drive so much at once. Spread one person's initiatives across months where
  it doesn't hurt priority.
- **Front-load where a month has more capacity;** ease off months with heavy leave/on-call
  (visible in per-person `leave`/`oncall`). Prefer starting an initiative in the month with
  room rather than splitting it thinly across all months for no reason.
- **Continuous vs one-shot:** if `currentAlloc` already spreads an initiative across months,
  that's a hint it's ongoing — a smooth spread is fine. A fresh `targetSP` with no history
  can usually land in the fewest months that hold it.

## Ask before guessing

If a call is genuinely the owner's — the window is over capacity and needs a
descope/overflow decision, two P1s collide in the only month with room, a DRI is overloaded
no matter how you sequence — STOP and ask 1–3 crisp questions in chat, then wait. A short
clarifying round beats a confidently-wrong plan. Otherwise proceed.

## Steps

1. **Read the dump:** `work-context/derived/plan-dump.json`. It is self-describing —
   `_instructions`, `_output_schema`, `_write_to` are inside it. If missing, tell the user to
   click **⤳ Dump for chat** on `/plan` first. If `$ARGUMENTS` names a different path, use it.

2. **Build Plan A (as-specified).** Walk initiatives in `rank` order. For each, pour its
   `targetSP` into the months left-to-right, filling each month toward its remaining `teamSP`
   before moving on — but apply the mindset above (priority protection, DRI spread, capacity
   easing). This is the faithful pass: keep the owner's ordering and DRIs; you're only
   deciding the month split. Record any remainder that doesn't fit as `overflow`.

3. **Build Plan B (manager rebalance).** Same targets, but now act as the EM: re-sequence to
   de-risk (pull compliance earlier, push nice-to-haves later), even out DRI load across
   months, and use capacity more deliberately. Explain every material change from Plan A in
   `signals` (level `info`), one line each: e.g. "Pulled OINT-42 (compliance) into Jul ahead
   of OINT-51 — deadline risk; OINT-51 has slack." If Plan A is already well-sequenced, Plan B
   can be close to it — but it's still your honest call.

4. **Write both files** matching the dump's `_output_schema`:
   ```json
   {
     "_generated": "<today> (as-specified)",
     "months": ["Jul","Aug","Sep"],
     "allocations": [
       { "key": "OINT-42", "summary": "…", "months": {"Jul": 12, "Aug": 8}, "overflow": 0 }
     ],
     "signals": [ { "level": "danger|warn|ok|info", "text": "…" } ],
     "rationale": "2-5 sentences on the key sequencing calls"
   }
   ```
   - `months` keys inside each allocation MUST match the dump's `monthNames` exactly (e.g.
     `"Jul"`), and a month with zero allocation can be omitted or set to 0.
   - `sum(allocation.months) + overflow == initiative.targetSP` for every initiative (round to
     0.5 SP). Include only initiatives with `targetSP > 0`.
   - Plan A → `work-context/derived/plan-A.json`; Plan B → `…/plan-B.json`
     (change `_generated` to `(rebalance)`).
   - `signals`: capacity verdicts ("Sep is 8 SP over — overflow on OINT-51"), DRI-load flags,
     priority calls. Plan B additionally carries one `info` line per re-sequence vs Plan A.

5. **Tell the user:** both plans written — click **▸ Load plan** on `/plan` to render them
   (as-specified fills the scratch allocation + Gantt; toggle to Rebalance to compare). This is
   a what-if sandbox — nothing is written to Jira.

## Notes
- Never mutate `targetSP`. Sequencing only.
- Keep month keys aligned to `monthNames`; the page maps them straight onto the scratch allocation.
- Read-dump → write-plan only. Don't touch the engine, server, or page.

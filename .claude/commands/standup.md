Daily-standup digest for the owner's team — what each person actually did, what's in
flight, what's blocked, and what they could pick up — for a given day/window.
Person-first, roster-scoped, leave- and oncall-aware. Read-only. Owner-invoked.

## Usage — `/standup [scope] [window]`

If invoked with `help` / `-h` / `--help`: print this Usage block and STOP.

- `scope` — `team` (DEFAULT) | `me` (=the owner) | `<person>`.
  - `team` → the 7 reports, person by person, + a team summary at the end. EXCLUDES
    the manager (the owner) — they're the audience.
  - `me` / `<person>` → just that person's section (this is how the owner sees their own).
- `window` — DEFAULT = yesterday. Accepts a day (`2026-06-05`), `last N days`,
  `this week`. Parsing + the weekend/holiday guard: `.claude/shared/date-range-grammar.md`.

Examples: `/standup` · `/standup team 2026-06-05` · `/standup <person> last 3 days` · `/standup me`

This is a sibling of `/ask` but deliberately SEPARATE (ask.py is large; standup has
its own daily cadence, sources, and ownership rules). It composes the same raw data
(`work-context/index/events.db`) but does NOT route through `/ask`.

---

## 1. Roster — `scope: team` is the source of truth

**Read `.claude/shared/roster-identity.md`** — it owns the roster definition
(`config/people.yaml` `scope: team`), the per-member identity set, the actor-OR-assignee
event filter, and the manager-exclusion rule. Standup specifics on top of that baseline:

- The roster here is the owner + the 7 reports; `team` digests render only the **7 reports**
  (`scope: team` minus the owner), since the owner is the audience. The owner is still
  reachable via `me` / `/standup <owner>` for their own section.
- A non-roster name in the output is a bug.

## 2. Data — raw `events.db`, NOT the cluster pipeline

Daily windows are too fresh for the embedding/cluster pipeline (it lags), so
`ask_engine window` returns nothing for "yesterday". Query
`work-context/index/events.db` directly, roster-filtered.

### 2a. FAST PATH — run the single-shot gather FIRST (do not hand-query)

**Performance:** the DB work is <0.2s; the old cost was ~10 sequential model-driven
SQL round-trips (each a full turn re-reading this prompt). So gather EVERYTHING in ONE
call, then format/enrich in ONE turn.

```bash
# EXACT invocation — the script lives at the REPO ROOT bin/ (not work-context/bin/),
# imports derive.* from work-context/, and needs the work-context venv (system
# python3 has no yaml). Run from work-context:
PYTHONPATH=$HOME/context/work-context \
  $HOME/context/work-context/.venv/bin/python \
  $HOME/context/bin/standup_gather.py <YYYY-MM-DD> <scope>   # scope = team | me | <canonical>
```

It emits, per roster member, in a single pass:
- window jira (with **assignee-at-close resolved** + `OWN`/`byActor` tag — credit rule §3 baked in),
- window github + confluence (with page titles),
- current BOARD state (inprog / todo / open-CMR, Epics already filtered, §3b). Each open
  CMR is tagged `active(window)` (worked in the window) or `STANDING` (no window activity —
  backlog to close, NOT today's work — see §6b),
- **Slack authored in window** + **@-asks over the past 2 days** with an
  `answered_by_member` flag (the §4b heavy scan, pre-computed),
- **`ON-CALL OPS`** (only for the on-call member) — the aggregated ops load: CMRs worked
  in-window (incl. ones only *reviewed* as db-on-call), plus incident/on-call/alert-channel
  posts + pings (ack/resolve confirmations). Render this as the §6 On-call line — it is the
  anti-under-reporting guard,
- **`THREADS engaged`** — threads the member posted a *substantive* in-window reply in
  (pure acks dropped), each with the **root context** + a `RESOLVED`/`involved` tag. This is
  how a teammate's thread the member unblocked gets credited (§4b/§7c), not just their own tickets.

It also emits an **`OWNER FOCUS`** block at the end (always — even on a `team` run, the
owner is the audience): the manager's own **reply-pending @-asks** (slack mentions of the
owner unanswered by them in-thread, over a LONGER 5-day owner lookback — `direct` <@owner>
mentions AND `subteam` pings of the handles the owner belongs to, tagged `via=`),
**reply-pending confluence @-mentions** (someone tagged the owner on a doc via
`ri:account-id`), **owner board items needing a decision** (open CMRs to approve/execute +
In-Review assigned to them), and **`DAY SIGNALS`** (release/CMR transitions in the window +
beta/prod deploy slack callouts). This block feeds the two owner-facing messages §7a
(📅 Day update — DAY SIGNALS) and §7b (⚠️ Your queue — reply-pending asks/mentions +
approvals/decisions).

It also emits **`# ONCALL`** (live Opsgenie, config-driven — §6), **`# LEAVES`**
(durable `team_leaves` overlapping the day + 14d upcoming, plus `LIVE-SIGNAL` rows from
the slack leave scan — §5), **`# ONCALL FORECAST`** (per-day on-call primary for the next
28d = 4 weeks / 2 sprints) and **`# RISKS`** (LEAVE×ONCALL collisions + COVERAGE gaps, four
weeks ahead — §6c) right after the freshness header, so on-call, leave, forecast and risk need
NO separate tool calls.

Read its output, then go straight to formatting (§7). Only fall back to ad-hoc SQL /
`mcp__plugin_context-mode` queries for the ENRICHMENT clause (§8) — the one substantive
detail from a ticket body or slack thread — which is judgement work, not bulk gather.
ONE tool call (the gather), not ten.

If the script errors or the roster/identity model changed, fall back to the manual
queries below.

**DATA FRESHNESS GATE (mandatory — read the gather's `# DATA FRESHNESS` block first).**
The gather emits a per-source freshness header: the newest event ts per source and a
`⚠️ STALE` flag when a source's newest event predates the window end. A stale source
means the day is built on incomplete data — that is exactly how a real ship gets
silently reported as "quiet" (validated: a 2-day Atlassian ingest stall made an entire
standup wrong). So:
- If the header shows `⚠️ STALE SOURCES PRESENT`, **do not proceed silently.** Lead the
  digest (and the chat reply) with a top banner: `⚠️ Data freshness — <source> ingest is
  stale (newest <ts>, ~<N>h old); this digest may be incomplete. Fix the ingest and
  re-run.` Name each stale source.
- Never render an empty/quiet section for a person when the source feeding it is stale —
  say "unknown (ingest stale)", not "no tracked activity".
- If all sources are `ok`, proceed normally (no banner).
- **`# CHANNEL FRESHNESS` (per-channel stall — read it too).** The source-level check above
  can't see a single channel that silently stopped ingesting while the global source looks
  fresh. The gather adds a `# CHANNEL FRESHNESS` block flagging on-call channels quiet >36h
  (≈ a stall, since they carry a steady bot feed). If it's present, banner it — "⚠️ on-call
  channel <name> may be stale; on-call work may be under-reported" — and treat that member's
  on-call section as possibly-incomplete, don't render it as a quiet rotation.

**FIELD SOURCING — do NOT use the window for everything.** Only "Done" is a
window-bounded event; the rest are CURRENT STATE and must be queried as state,
else they go silently empty on a quiet day:

| Field | Source | Why |
|---|---|---|
| **Done** | window events | a real ship that happened *in* the window (PR merged, ticket → terminal, doc authored) |
| **In progress** | current board state | latest status ∈ In Progress/In Review for the assignee (§3b) + open authored PRs — regardless of window |
| **Up next** | current state | currently-open assigned To-Do/Open tickets + open asks + pending reviews + Slack self-commitments & unanswered @-asks (§4b) — not "changed today" |
| **Blockers** | currently-open | unresolved threads / firing alerts that are STILL open, even if started before the window (bound to ~last 2 days for recency) |

So the window scopes *Done* and *what changed*; everything else is "what is true on
the board right now". Never report In-progress/Up-next/Blockers as empty just because
nothing changed in the window.

## 3. OWNERSHIP — credit owned work, never transitions (critical)

The team flips tickets To-Do→Done in batches at standup; a `status_change` event's
`actor` is whoever **moved** the board, NOT who did the work, and its `assignee` field
is often blank. **Credit by assigned-at-close, not the transitioner.**

- "Done" / "In progress" for a person = tickets where THEY are the **owner** (the
  dev — see §3c for what that means by status) + PRs they **authored** (merged/opened)
  + commits. `bin/standup_gather.py` resolves the owner via the shared engine
  `derive/jira_metrics.infer_all_ticket_roles` (replays assignment-event titles, NOT
  the `assignee` column — which is null on status/assignment rows). Do NOT hand-roll a
  "latest assignee" query: the column is empty on transitions, so it credits no one.
- A member moving someone else's ticket = clerical. Either drop it, or note once at
  team level ("<member> ran the board — moved 4 tickets, not their work"). NEVER render
  a moved-but-not-owned ticket as that person's Done. (Validated bug: several tickets a
  member only *moved* at standup were wrongly rendered as that member's Done — credit
  had gone to the transitioner instead of the assignee-at-close.)

## 3b. "In progress" = CURRENT BOARD STATE, not window events

"In progress" is a *state*, not an event — a ticket can sit In-Progress/In-Review
assigned to someone with no status-change in the window. So DON'T derive it from
window events only (that misses them). `bin/standup_gather.py` computes the current
board for every member in one pass via `derive/jira_metrics.infer_all_ticket_roles`,
which returns per-subject `TicketRoles(state, dev, reviewer, current_assignee)` keyed
by canonical handle. The gather buckets each member's tickets from those roles:

- `state == in_progress`                         → **In progress** (owner = the dev)
- `state in (in_review_active, …_awaiting_reviewer)` → **In review** (own work; reviewer shown or "awaiting reviewer") — see §3c
- the member is a ticket's `reviewer`            → **Reviewing** (someone else's work)
- `state == other` and status ∈ To-Do set        → **Up next**

`current_status` falls back to the `issue_created` status snapshot for never-
transitioned To-Do tickets (no `status_change` rows), so backlog items aren't lost.

Always list these under "In progress" even if untouched today. (Validated: a member
had two tickets In-Progress on the board with no event in the window — the window-only
gather wrongly showed nothing; and an entire epic's board surfaces only via state, not
via window events.) Add a PR-state equivalent: open PRs the member authored that aren't
merged/closed.

**EXCLUDE Epics/initiatives, and CAP.** Two guards so this stays a standup, not a board dump:
- Filter out `issue_type='Epic'` (and long-running initiative containers) — they sit
  "In Progress" forever. The manager/owner is assigned ~15 such epics
  (modernization, oncall, op-excellence containers…); those are NOT standup items.
  Show only Story / Task / Bug / Sub-task.
- Cap In-progress to ~5 most-active (current sprint, or most recently touched); if a
  person has more, summarise ("+N more on the epic"). Up-next: top 3-5,
  sprint-committed first — never the full backlog (some members have ~13 To-Do, the owner dozens).

## 3c. DEV vs REVIEWER — interpret the assignee by status (critical)

The assignee means different things depending on status. Read it logically, don't
just credit "latest assignee":

- **In Progress / In Development** → the assignee is the **dev doing the work**. Credit
  the ticket to them.
- **In Review** → two cases:
  - **assignee unchanged** (same person who had it In Progress) → that dev **finished
    their part; it's waiting on a reviewer**. Render under the dev as
    *"<ticket> — in review, awaiting reviewer"*.
  - **assignee changed to a new person** → the new person is the **reviewer**; the
    **dev who had it In Progress keeps the credit**. Render the *work* under the dev
    (*"in review, <Reviewer> reviewing"*) AND a *review task* under the reviewer
    (*"reviewing <Dev>'s <ticket>"*). When review is done the reviewer moves it back
    to the dev — so a ticket bouncing In-Review→In-Progress is the dev's again.

This rule is implemented once in `derive/jira_metrics.infer_ticket_roles` /
`infer_all_ticket_roles` (single source of truth; `/ask`, `/retro`, `/dev-style` use
it too). The gather emits the resulting lines: `IP`, `IR … (reviewer=… | awaiting
reviewer)`, and `REVIEWING <ticket> (dev=…)`. For an In-Progress ticket reassigned
mid-flight, the **current** holder is the dev (whoever is building it now).

So the "work owner" = **the assignee while the ticket was In Progress** (the dev), NOT
the current in-review assignee. `standup_gather.py` computes this: it prints
`owner=<dev>` on window rows, an `IR … (reviewer=<name> | awaiting reviewer)` line for
the dev's in-review work, and a `REVIEWING <ticket> (dev=<name>)` line under whoever is
reviewing someone else's ticket. Render those exactly — never put a ticket on the
reviewer's plate as their own Done/In-progress, and never drop the dev's credit because
a reviewer is the current assignee. (CMRs have no dev/review semantics — keep crediting
them by latest assignee.)

## 4. PRIMARY WORKSTREAM — show domain even on a quiet day

Each member has an owning domain (e.g. one member owns IFT, another TDS). Derive it from
their assigned tickets / authored PRs over a trailing ~30d (or `derived/` project
footprint). Lead the person's section with their domain so a quiet day reads
"`<Name> · <domain> — no tracked activity today`", not silence or borrowed board-moves.
If they genuinely logged nothing, say so — do not fabricate from transitions.

## 4b. SLACK DEEP SCAN — mandatory per-member pass (heavy)

Slack is a FIRST-CLASS source here, not just enrichment. For every active roster
member, run a deliberate Slack scan over the window (and ~2 days back for open asks/
promises) and fold the results into Done / In-progress / Up-next / Blockers. Never
skip this because Jira/PRs already produced lines — Slack catches work that never
becomes a ticket (debugging, prod support, design back-and-forth, helping teammates).

**Scope = domain channels + asks (not the full workspace).** Two channel sets:
- **Domain channels** — the member's owning-domain channels (§4) plus the shared
  team/ops channels in `config/slack_channels.yaml` tagged to their area.
- **Asks** — ANY channel where they are `<@slack_id>`-mentioned or subteam-pinged in
  the window, even outside their domain (that's how cross-team asks reach them).

**Match the member three ways** (resolve identities from `config/people.yaml`):
1. **As author** — `slack_id` is the message author.
2. **As mention target** — message contains `<@their_slack_id>` (or a subteam ping
   that includes them).
3. **Thread-reply walk** — for any root they authored or are mentioned in, pull the
   replies so a "done"/"blocked"/"will do" buried in-thread isn't missed.

**Classify each hit into the four fields:**

| Signal in Slack | Feeds | What to look for |
|---|---|---|
| **Progress / ship report** | Done, In-progress | "merged", "deployed", "fixed", "done with", PR/MR links they posted, "rolled out", root-cause posts. Augment — don't double-count a PR already credited via events.db; add the Slack clause as the enriching detail. |
| **Self-made commitment** | Up next | first-person future: "will do X", "I'll pick this up", "tomorrow I'll…", "next is…", "planning to…". These are the strongest Slack pick-up signals — render them as Up-next with the thread link. |
| **Open ask directed at them** | Up next | `<@them>` mention still UNANSWERED by them at scan time (no later reply from their `slack_id` in-thread), or an explicit "can you / pls check / need from you". Pending PR-review requests count here too. |
| **Stuck / waiting** | Blockers | "blocked on", "waiting for", "can't proceed", "any update", "still failing", unresolved error threads they're in. Only if STILL open (no resolving reply). |

**Threads they resolved / were involved in — read the gather's `THREADS engaged` block.**
A teammate's thread the member *unblocked* or drove to a decision is real work, but it
never becomes their ticket, so it was being dropped. The gather now lists every thread the
member posted a substantive in-window reply in, with the **root** (what the thread was
about) + a `RESOLVED`/`involved` tag. Fold them in:
- `RESOLVED` (their reply carried the answer/decision/fix) → render under **Done** as
  *"unblocked <teammate> on <what the root was about> ([thread])"* — credit the help, framed
  from the ROOT, not their reply text.
- `involved` → judge by ENGAGEMENT DEPTH, not a fixed cap. A thread tagged `heavy` (≥3
  substantive replies = sustained help) or `xteam` (root by a non-roster author = cross-team
  support in another team's channel) is real, often-invisible work — SURFACE it (e.g. "fielded
  another team's API questions — contract, edge cases, latency, auth ([thread])"). Cross-team
  support is exactly what standup should catch.
  Keep up to ~3 such threads (heaviest/xteam first — the gather ranks them); collapse only the
  single-reply drive-bys. (Validated miss 2026-06-23: a 5-reply cross-team API-support thread
  was dropped under the old "at most one involved" cap.)
- JUDGE the root: skip personal/social roots (condolences, logistics) and bot-reminder
  roots (CMR-cleanup, standup-join) — those aren't the member's work even if they replied.
- The `link=` is the thread root permalink — OPEN it (§8) to state what was actually
  resolved; the one-line root snippet is rarely enough.

**De-dupe + cap so it stays a standup:**
- If a Slack hit maps to a ticket/PR already rendered, MERGE it (one line, Slack adds
  colour + link) — don't emit a second item.
- Up-next from Slack: top 3-5 commitments/asks, most recent first; fold into the
  existing Up-next cap (§3b), don't blow past it.
- An answered ask (they already replied/closed it) is NOT Up-next — drop it.
- Quote at most one short clause per item; link the thread
  (`https://example.slack.com/archives/{CH}/p{ts_no_dot}`), never paste the message.

**Honesty:** a Slack "I'll do X tomorrow" is a real plan signal — it's the one place
"today/planned" IS derivable (§9), so Up-next sourced from a dated commitment may be
stated as planned with the date. Everything else stays "what's true now", not invented.

## 5. LEAVE — read the gather's `# LEAVES` block

Mark members who are on leave for the window; don't expect a standup line or call them
"quiet". The gather emits a `# LEAVES` block combining BOTH sources — no separate query:

- `ON-LEAVE-THIS-DAY` / `UPCOMING` rows = the durable `team_leaves` table (overlapping
  the window day + planned leave in the next 14 days).
- `LIVE-SIGNAL` rows = a live regex scan of roster slack (lookback→window-end), because
  the leaves cron's chat-classify is owner-invoked and lags. (Validated: a member's
  sick-leave slack message was in events.db but not yet in `team_leaves`.)
- JUDGE the LIVE-SIGNAL rows — the regex is broad; a mention of someone ELSE's OOO or a
  "wfh" in passing is not the member's leave. Check who's speaking and what they say.
- Render on-leave members as `### <Name> — 🌴 on leave (sick/ooo, <date>)` with the
  permalink; skip the four-line block. Mention near-term `UPCOMING` leave as a one-line
  note in that member's section.
- **ANNOUNCE upcoming leave one sprint (14d) ahead.** Every `UPCOMING` row (rolling 14
  days = one sprint) MUST surface in **Message 1 — 📅 Day update** under team status, not
  only buried in the member's section — so the owner sees who's out across the sprint with
  enough runway to plan. Collapse consecutive dates into a range ("22 Jun–1 Jul").

(Manual fallback if the gather block is missing: query `team_leaves` + scan roster slack
`body` for `leave|sick|fever|unwell|ooo|out of office|day off|wfh|working from home`.)

## 6. ONCALL — read the gather's `# ONCALL` block

The gather queries Opsgenie itself (config-driven via `work-context/config/oncall.yaml`
— `opsgenie.schedule`, `identifier_type`, `api_key_env`; never hardcode the schedule
name) and emits `# ONCALL` with the participant email already mapped to a roster
canonical. If the block shows `⚠️` (no key / lookup failed), fall back to the manual
call and SAY the on-call source was degraded:

```bash
# values from config/oncall.yaml
curl -s -H "Authorization: GenieKey ${!api_key_env}" \
  "https://api.opsgenie.com/v2/schedules/${schedule}/on-calls?scheduleIdentifierType=${identifier_type}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin).get('data',{});print([p.get('name') for p in d.get('onCallParticipants',[])])"
```

Badge the on-call person
`### <Name> — 📟 on-call`. Their incident / alert / log-triage work is EXPECTED — frame
it as on-call duty, not as a red flag or as their feature work.

**On-call gets an explicit `On-call:` line** in their section (in addition to the
badge) summarising the ops load handled while on rotation — incidents triaged, alerts
chased, and CMRs worked. Don't bury it; it's the bulk of their day. Example:
"On-call: triaging the category order failure; pushed a recent TB-diff rectification
([EX-NNNN]) to Change-Approved."

**Build the On-call line from the gather's `ON-CALL OPS` block — render ONE bullet per
incident, don't under-report (validated miss 2026-06-23: on-call work follows the @oncall
HANDLE across the whole org + the oncall bot, NOT a fixed alert-channel list).** The block
surfaces every incident for recall; you OPEN each thread (`link=`) to write the per-incident
line. Render each row type:
- **`INCIDENT`** (bot-tracked: the member acked / resolved / marked Not-Our-Issue) — one
  bullet each, in a per-incident format: `#<origin-channel> — <issue> — <who> tagged on-call;
  <resolution>`. The gather gives the issue snippet + `actions` + a thread `link=`; OPEN it to
  recover the **origin channel** (from the bot's "Issue Link") + the **reporter** + the real
  resolution. `origin=(open thread)` means the bot edited the root — get it from the thread.
- **`FOLLOWUP`** (member replied in a thread where @oncall was pinged, any channel) — manual
  chases the bot doesn't track (e.g. asking if a stale incident is closed). One bullet each:
  `#<channel> — <what was chased / resolved>`.
- **`CMR-work`** — CMRs worked in-window, incl. ones the member only *reviewed* as db-on-call
  (someone else owns them) — still on-call work; render with the CMR id.
- **`FYI-ACK count=N`** — the recurring pending-txn auto-acks, already collapsed. Mention as
  one line ("acked N recurring pending-txn FYIs") or drop; never expand to N bullets.
- Plus the on-call ticket (the `Oncall` epic Task on their board) + any capacity/monitoring posts.
A thin two-bullet On-call line when the block lists several INCIDENT/FOLLOWUP rows is a regression.

## 6c. ON-CALL FORECAST + RISKS — read `# ONCALL FORECAST` and `# RISKS` (four weeks ahead)

The gather also forecasts the on-call primary for the next 28 days (rolling = 4 weeks /
2 sprints) via per-day `on-calls?date=` lookups, then cross-refs it against `team_leaves`
to emit a `# RISKS` block. Looking a full month out gives real runway to fix a rota
collision before it's urgent. Two risk types, both surfaced in **Message 1 — 📅 Day
update** (never silently dropped):

- **`LEAVE×ONCALL`** — a roster member is scheduled on-call on a day they're also on leave.
  This is a coverage hole that needs a swap NOW — render it as a clear, dated callout and
  name the date + who, e.g. "⚠️ 25 Jun — Carol is on-call but on leave (vacation); needs a
  rota swap." Announce it up to four weeks ahead so there's ample time to fix.
- **`COVERAGE`** — ≥2 roster members out the same day. Flag thin-coverage stretches:
  "⚠️ 22 Jun–1 Jul — Dan + Eve both out; thin coverage." Collapse consecutive
  dates into a range.

If `# RISKS` shows `(none)`, say nothing (don't pad). If `# ONCALL FORECAST` shows
`?(lookup failed)` rows, the rota for those days is unknown — say "rota unknown" rather
than implying no risk. Rank LEAVE×ONCALL above COVERAGE (a named hole beats a thin stretch).

## 6b. CMRs — production rectifications (ops, surface separately)

CMRs (`issue_type='CMR'`) are production change/rectification tickets — TB-diff fixes,
ABB rectifications, missing-txn inserts, GL balance fixes. They are NOT feature work
and have their own statuses (`Change Approved`, `Implementation Reviewed`, `Cancelled`).
Pull them like other tickets but render as OPS, not as Done feature ships:
- Window CMR activity (a CMR moved/approved/executed in the window) → an "Ops:" line.
- Open CMRs assigned to a member (esp. the on-call) → list under their ops/on-call load.
CMRs cluster heavily on the on-call + ops-leaning ICs; don't drop them — a day of
rectification CMRs is real work even with zero feature tickets. (Validated: a TB-diff
CMR was Change-Approved during the window and the first pass mis-filed it as feature
work.)

**`active(window)` vs `STANDING` — never re-report a standing CMR as today's work
(validated miss 2026-06-23).** A CMR's late states (`Released with Emergency`, `Change
Released`) are NOT terminal — the ticket stays open until `Implementation Reviewed` /
`Review Complete`, so a CMR released days ago lingers on the board for a week. The gather
tags each open CMR:
- `active(window)` → it had activity in THIS window → render as the day's ops work.
- `STANDING` (no window activity) → it is BACKLOG, not today's work. Do NOT render it as a
  Done/On-call accomplishment — that's how the same emergency-released IFT CMR showed up in
  standup after standup. At most, mention standing CMRs ONCE as a backlog nudge ("N CMRs
  still open to close"), and only if it adds signal. Never frame a standing CMR as freshly
  done.

## 7. Output — FOUR root messages (team scope), in this order

A `team` run produces **four separate top-level Slack messages** (not one parent +
threaded replies — distinct posts, each free to grow its own thread), across TWO
channels. Post them in this order; each is self-contained:

1. **📅 Day update** (§7a) — everything from the day the owner should know. → owner channel.
2. **⚠️ Your queue** (§7b) — the owner's personal action items. → owner channel.
3. **👥 Standup updates** (§7c) — the per-person standup ONLY (no team summary). → team
   channel. Devs are @-mentioned here, so this is the one everyone reads.
4. **📋 Team summary** (§7d) — the team-level synthesis (blocked / ships / unowned /
   out-on-call). → owner channel (plain names, no @-pings).

Channel routing is owned by the scheduled-task SKILL.md Step 3 (Day update + Your queue +
Team summary → `standup_channel`; Standup updates → `dev_updates_channel`). For an
interactive `team` run, render all four in the chat reply.

For `me` / `<person>` (interactive, non-team): skip the multi-message split — just render
that one person's section (§7c per-person format) in the chat reply.

### 7a. Message 1 — `📅 Day update — <date>`

The owner's day-level briefing: **everything of importance from the day**, even if no
action is needed (action items live in Message 2, not here). Synthesise from the gather's
`DAY SIGNALS`, `# RISKS`, `# ONCALL FORECAST`, `# LEAVES`, and the per-member slack/jira
scans. Group as short bullets under bold headers; each item carries its link:

- **Decisions & announcements** — calls made or broadcast today: design/architecture
  decisions, MOM outcomes, public or team-wide announcements, anything announced *to the
  owner*. Reword the decision, link the thread.
- **Timelines & dates** — go-live dates, beta cuts, cycle-day callouts, deadlines
  committed today (e.g. "DCMS go-live end of month; QA from 24th").
- **Shipping / rolling out** — releases, beta cuts, prod deploys done or in flight
  (DAY SIGNALS release/CMR transitions + deploy callouts), with the owner of each.
- **Prod / ops watch** — live incidents, TB-diffs, alert bursts, stuck-txn / 5xx threads
  the team is chasing (esp. on the on-call).
- **Team status & risk** — who's out over the rolling 14d (§5 UPCOMING, date ranges),
  who's on-call now + over the next 4 weeks (§6c forecast), and **every `# RISKS` line**:
  LEAVE×ONCALL collisions first (dated, named, "needs a swap"), then COVERAGE gaps. Risks
  scan a full four weeks out — this is the heads-up; never omit a risk to save space.
- **Cross-team** — notable asks/decisions from sister teams touching this team's surface.

Keep it tight but complete; this is FYI awareness, so don't duplicate Message 2's action
items here. If a whole group is empty, drop the header.

### 7b. Message 2 — `⚠️ Your queue — <date>`

The owner's own action queue — *only* things **the owner must personally act on or
decide**, not the team's work. Source = the gather's `OWNER FOCUS` block + escalations
surfaced in the per-member scan. Triage and rank; do not dump the raw list. Most-urgent
first:

- **Your reply is pending** — `OWNER @-asks` where the owner is mentioned and hasn't
  replied in-thread. The gather casts a wide net (5-day lookback; plus `OWNER confluence
  @-mentions`) and tags each row by tier:
    - `via=direct` — a direct `<@owner>` mention.
    - `via=subteam-mgr` — a ping of a **managerial** user-group the owner belongs to
      (tech-managers, cbs-ems, cbs-tech-leads, incident-commanders…). These are the owner's
      own asks — keep them here.
    - `via=subteam-dev` — a ping of the **dev-level team handle**.
      These are usually NOT the owner's personal reply — they go in the separate **To route /
      delegate** bucket below, not here.
  So for THIS bucket keep only `direct` + `subteam-mgr` that are real asks of *him* (decision,
  opinion, join a call, confirm/approve); drop pure-cc / FYI mentions, asks aimed at someone
  else in the same ping, and anything he has clearly already actioned (e.g. a "kindly approve"
  whose CMR is now Approved on the board). Lead with what's asked + who's waiting + how long
  it's sat. Because the lookback is 5 days, weight staleness: a 4-day-old unanswered ask is
  more urgent to flag, not less.
  - **An admin/process ask broadcast to a manager group is NOT "FYI" — it's a queue item
    (validated miss 2026-06-23).** When a `subteam-mgr` ping asks the manager to personally
    *complete an action* — submit R&R / award nominations, write/own an RCA-POD, sign off a
    migration his services own, file the team's leave plan, complete a comp/retention step —
    keep it. The owner must DO the thing; a group audience doesn't make it informational.
    Only genuinely passive broadcasts (announcements, FYIs with no action on him) get dropped.
  - **`escalating×N` = the strongest keep signal — never triage it out.** The gather tags an
    ask `escalating×N` when the same thread has been re-pinged N times across the lookback and
    the owner still hasn't replied (e.g. R&R noms chased 2→4→6). A re-ping means it's overdue
    and getting more urgent — rank these to the TOP, and state the chase count + age ("chased
    3× since 17 Jun, still pending").
- **🔀 To route / delegate** — `via=subteam-dev` asks (someone pinged the dev team handle and
  no one has answered in-thread). These are the owner's to ROUTE, not to personally answer:
  for each, name the likely dev owner (by domain — §4) and frame it as "delegate to <@dev>",
  with the ask + who's waiting + age + thread link. Drop ones a teammate has already picked up
  in-thread. If a dev-group ping genuinely needs the owner himself (a real decision/approval),
  promote it up to **Your reply is pending** instead.
- **Approvals pending on you** — open CMRs awaiting the owner's approval/execution, and any
  "kindly approve the CMR @owner" slack asks (now caught even when several days old). CMR
  approvals gate prod rectifications — high priority. Cross-check the board: if the CMR has
  since moved to Approved/Released, the ask is resolved — drop it.
- **Reviews awaiting you** — PRDs / TRDs / API contracts shared for the owner's review
  (slack "review this @owner" + subteam "please review" pings + `OWNER confluence
  @-mentions` on a doc + In-Review items assigned to him).
- **Decisions / escalations** — team blockers needing a *manager* call (timeline crunch,
  ownership gaps, unowned incidents, an unresolved LEAVE×ONCALL rota swap), framed as the
  decision he owns. **This INCLUDES a dev-escalated "should we do X?" proposal directed at
  the owner** — a direct `<@owner>` question asking for his call on scope/approach/architecture
  (e.g. "should we take an interim platform-side fix to cut a recurring on-call noise source? @owner"),
  even when phrased as a forward proposal rather than an urgent blocker. A `?` + direct mention
  asking the owner to decide is a queue item, not the asking dev's plan. (Validated miss
  2026-06-23: a direct "should we take this? @owner" RTGS-fix decision was rendered only under
  the dev's Up-next, never in Your queue.)
- **Cross-check before posting:** anything rendered in a dev's §7c section as "pending a
  decision / your call / awaiting your sign-off" MUST also appear here in Your queue — the
  owner sees every decision awaiting him in ONE place. Surfacing it in the dev's section does
  NOT substitute for the queue item (the two serve different audiences; §7c is team-facing and
  must stay neutral per §7c's rules, §7b is the owner's action list).

Each line: one-sentence what + who's waiting + age, then the clickable link
(`[thread](…)` / `[EX-NNNN](…)`). Rank by (prod/customer impact × staleness). If the queue
is genuinely empty, say `Nothing pending your action.` — never pad.

### 7c. Message 3 — `👥 Standup updates — <date>`

This message is **team-facing** (the team channel) — so the per-person header is
the dev's **real @-mention**, not their plain name, and every dev gets notified. The
mention is `<@SLACK_USER_ID>` (id from `config/people.yaml`) and MUST sit on a **bold line,
NOT a `###` heading** — a `###` heading escapes the mention to literal `<@U…>` text. Same
`<@U…>` mention for every cross-reference too (reviewer-of, "<dev>'s ticket", team-summary
names). (Validated 2026-06-22: `### <@U…>` rendered as literal text; `**<@U…> · …**`
rendered as a real ping.)

**AUDIENCE — write for the whole team, NEVER addressed to the owner.** This is a general
broadcast everyone reads, not a note to the manager. So:
- NO second-person aimed at the owner — never "your", "you", "for your review", "needs your
  call/approval/decision". The owner is not the reader here.
- Re-frame an owner-directed ask as a **neutral team statement**: "needs your lookback call"
  → "pending a decision on the lookback window"; "for your review" → "awaiting review".
- Owner-directed framing (action items, approvals, decisions the manager owns) belongs ONLY
  in §7b Your queue and §7d Team summary — both of which post to the owner channel, not here.
- Keep it dev-friendly: what each person did / is doing / is blocked on / picks up next,
  stated plainly for peers.

One section per roster member (on-leave members get the one-line leave badge instead). Use
**nested bullets**: each status is a **bold parent bullet**, and every item sits as an
indented sub-bullet under it. **Omit any section that has no items** — never render an
empty "Done". Order the sections Done → In review → In progress → Reviewing → Blockers →
Up next:

```
**<@SLACK_USER_ID> · <primary domain>**  [📟 on-call]
- **Done**
    - <plain description of work THEY OWN> ([EX-NNNN](url) / [PR #N](url)) — real ships only
- **In review**
    - <desc> — awaiting reviewer (or "<@Reviewer> reviewing") ([link])
- **In progress**
    - <desc> ([link]) — proxy for "today"
- **Reviewing**
    - reviewing <@Dev>'s <ticket> ([link]) — someone else's work
- **Blockers**
    - <what's stuck: the ACTUAL issue in plain words> — <why it's blocked / who's being chased + their last response> ([thread]) — must read standalone; never a bare "chasing X ([thread])" that forces a click
- **Up next**
    - <top 3-5 pick-up candidates they own / are asked for; ranked> ([link])
```

On-call member: add an **`On-call`** bold bullet (per §6) with the ops load as sub-bullets.
Members on leave: `**<@SLACK_USER_ID> — 🌴 on leave (<reason>, <date>)**` + a one-line
upcoming-leave note, no sub-bullets.

(For interactive `me`/`<person>` chat replies — no Slack post — plain names and `###`
headers are fine; the `<@U…>`/bold-line rule is only for the team-facing Slack Message 3.)

### 7d. Message 4 — `📋 Team summary — <date>`

The team-level synthesis — a **separate message to the owner channel** (NOT appended to
Message 3, which is team-facing and per-person only). Owner-facing, so **plain names, no
@-mentions** (don't re-ping the team here). Bold-header bullets:
- **Blocked / needs attention** — consolidated, most urgent first (what you raise in standup).
- **Notable ships** — 2-4 headline deliveries (by owner).
- **Unowned / stalled** — tickets needing a picker (unassigned / bounced to To-Do), untriaged alerts.
- **Out / on-call** — on-leave members (one line) + the on-call name.

## 8. Describe every item — never a bare ID, enrich from body + slack

**Shared render rules apply — Read `.claude/shared/render-rules.md` first** (URL
conventions, never-a-bare-ID, self-summarizing thread refs, pre-save link check). The
bullets below are the STANDUP-SPECIFIC additions on top of that shared baseline: how
the gather feeds descriptors (`# PR INDEX`), the deterministic PR-author rule, the
body/slack enrichment clause, and standup's own pre-save check.

- **The descriptor is DETERMINISTIC — read it from the gather's `# PR INDEX` block, do
  NOT re-derive it.** `standup_gather.py` emits, for every PR referenced in the window,
  a line `#N (repo) author=<canonical> title="..." :: <first body line>` built straight
  from the `pr_opened`/`pr_merged` row in `events.db` (review/comment rows carry no title,
  so this is the ONLY place a review-only PR gets a description). Use that `author` and
  that `title`/`desc` verbatim — same DB → same wording every run. Never call `gh pr view`
  (the PR repo is usually invisible to the local token) and never paraphrase from memory.
- **NEVER guess a PR's author from who reviewed/commented on it.** The `# PR INDEX`
  `author=` field is the opener (gh login → roster canonical), resolved deterministically.
  A reviewer's section referencing a PR is reviewing SOMEONE ELSE's work unless that
  person IS the PR INDEX author — render `[#N] <author>'s <what-it-does>` straight from
  the index. (Validated bug: #845 was rendered as "Alice's PR" under a reviewer, but
  PR INDEX `author=bob-example` — it was Bob's own lien feature.)
- **Enrich** the rendered items with one substantive clause from beyond the title —
  grounded in the source, not the title (`.claude/shared/evidence-grounding.md`): the ticket
  body/comments, the slack thread that discusses it (search slack `body` for the ID or the
  key domain term), and the parent epic. One line, not a paragraph. Example:
  not "investigate customer-group aggregate drift ([EX-NNNN])" but "customers whose
  cash withdrawals are missing from the withholding year-total table — recon found txn group-ids
  absent from the aggregate, risking under-deducted withholding ([EX-NNNN], withholding epic; flagged
  in #recon)".
- Links: per the shared URL conventions (`.claude/shared/render-rules.md` §1).
- **Slack links come pre-built — use them.** `standup_gather.py` emits a ready
  `link=https://example.slack.com/archives/...` field on EVERY slack row (authored +
  @-asks), valid for root posts and replies alike (built from the message's own ts, not
  thread_ts). Copy that `link=` value verbatim — never hand-construct or drop it.
- **Thread refs must be self-summarizing** — per shared §3. Standup specifics: the
  gather's one-line `::` preview is rarely enough; OPEN the thread (`slack_read_thread`
  on the row's ch + ts) and distil one clause of real context. (Validated 2026-06-19: a
  blocker rendered as a bare "chasing X" forced the owner to open the thread to learn it
  was an account-balance data-lag.)
- **Pre-save check (mandatory):** run the shared pre-save link check
  (`.claude/shared/render-rules.md` §4) over all three messages. Standup specifics: the `[thread](…)` link
  comes from the gather's `link=` field; if a referenced row truly has no `link=` (rare),
  append `(no linkable ts)`.

## 9. Daily-signal honesty

- Lean on real-timestamp events (PR opened/merged, commits, slack, jira transitions +
  comments). Do NOT lead with SP/Done counts at day granularity (same-day batch-flip).
- "Today / planned" is NOT derivable from board/PR state — "In progress" (still-open
  owned work) is the honest proxy. The ONE exception: a dated first-person Slack
  commitment ("I'll pick up X tomorrow", §4b) is a real plan signal and may be rendered
  as planned, with the thread link. Never invent a plan beyond that.
- Plain language — per `.claude/shared/plain-language.md` (no cluster IDs, engine names, or
  tool jargon; cite openable artefacts). Standup-specific: also no SP math at day granularity.

## 10. Output — NO md files (changed 2026-06-12, owner decision)

**Do NOT write markdown files.** The digest is delivered, not archived (the old
`management/standup/<date>/` team.md + per-person files cost ~3 min of duplicate
generation per run and weren't being read). Read-only on all sources; the only outputs:

- **Scheduled `team` run** → FOUR root Slack posts (per the scheduled task's Step 3),
  in order: 📅 Day update (§7a) → ⚠️ Your queue (§7b) → 👥 Standup updates (§7c) →
  📋 Team summary (§7d). Day update + Your queue + Team summary → owner channel; Standup
  updates → team channel (devs @-mentioned). Plus this chat transcript.
- **Interactive run (any scope)** → the chat reply only. For `team`, render the same
  four sections in order (📅 Day update → ⚠️ Your queue → 👥 Standup updates → 📋 Team
  summary); for `me`/`<person>`, just that person's §7c section.

The "pre-save check" (§8) still applies — run it on all four messages before posting.

## Hard constraints

- Roster = `scope: team` only. Non-roster name in output = bug.
- Credit by assignee/author, never the transitioner.
- On-leave members badged, not expected to report; on-call member badged, incident work expected.
- Never a bare ticket ID OR bare PR number; describe + enrich + link. Every `[#N]` PR
  link gets a 2–4 word inline label (§8) — a bare `#845/#850/#865` is a regression.
- `team` output = FOUR root messages in order: 📅 Day update (§7a) → ⚠️ Your queue
  (§7b) → 👥 Standup updates (§7c) → 📋 Team summary (§7d). §7a = FYI awareness
  (decisions/announcements/timelines/risk), §7b = owner's own action queue only, §7c =
  per-person standup, §7d = team-level synthesis. Never duplicate §7b action items into
  §7a. Empty §7b says `Nothing pending your action.` — no padding.
  §7a + §7b + §7d post to the owner channel (plain names); §7c (Standup updates) posts to
  the team channel with each dev as a real `<@U…>` mention on a BOLD line (a `###` header
  escapes the mention) — every dev must be notified. Team summary is its OWN message to the
  owner channel, NOT appended to the team-facing Standup updates.
- Per-person uses NESTED bullets — bold status header (Done/In review/In progress/
  Reviewing/Blockers/Up next) as parent, items as sub-bullets; omit empty sections.
- Announce upcoming leave ONE SPRINT (14d) ahead, and LEAVE×ONCALL/COVERAGE risks FOUR
  WEEKS (28d) ahead, in §7a — never drop a `# RISKS` line to save space.
- Read-only — no writes to events.db, Confluence, Jira, Opsgenie.

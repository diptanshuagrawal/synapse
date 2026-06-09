Daily-standup digest for the owner's team — what each person actually did, what's in
flight, what's blocked, and what they could pick up — for a given day/window.
Person-first, roster-scoped, leave- and oncall-aware. Read-only. Owner-invoked.

## Usage — `/standup [scope] [window]`

If invoked with `help` / `-h` / `--help`: print this Usage block and STOP.

- `scope` — `team` (DEFAULT) | `me` (=the owner) | `<person>`.
  - `team` → the 7 reports, person by person, + a team summary at the end. EXCLUDES
    the manager (the owner) — they're the audience.
  - `me` / `<person>` → just that person's section (this is how the owner sees their own).
- `window` — DEFAULT = yesterday (today-1d 00:00 IST → today 00:00 IST). Accepts a
  day (`2026-06-05`), `last N days`, `this week`. If "yesterday" is a weekend/holiday
  with ~no activity, say so and offer the last working day.

Examples: `/standup` · `/standup team 2026-06-05` · `/standup <person> last 3 days` · `/standup me`

This is a sibling of `/ask` but deliberately SEPARATE (ask.py is large; standup has
its own daily cadence, sources, and ownership rules). It composes the same raw data
(`work-context/index/events.db`) but does NOT route through `/ask`.

---

## 1. Roster — `scope: team` is the source of truth

The roster = `config/people.yaml` entries with **`scope: team`** (8: the owner + the 7
reports). NOT `team.md` prose, NOT the `role` field (the org has many people with a
role). Build the identity set from those entries — `github` + `github_aliases`/`git_names`,
`jira_id` + email, `slack_id` + `slack_handle`, `canonical`. **Keep an event only if
its `actor` OR `assignee` matches a roster identity.** Non-roster actors (anyone not in
the `scope: team` set) are dropped — a non-roster name in the output is a bug.

**EXCLUDE THE MANAGER (the owner) from `team` digests.** They're the audience, not a
reportee — `team` renders only the **7 reports** (the `scope: team` members minus the
owner). The owner is still reachable via `me` / `/standup <owner>` for their own
section, but never appears in the `team` digest or team summary. (Owner identity = the
single `scope: team` entry flagged as manager/owner in `config/people.yaml`.)

## 2. Data — raw `events.db`, NOT the cluster pipeline

Daily windows are too fresh for the embedding/cluster pipeline (it lags), so
`ask_engine window` returns nothing for "yesterday". Query
`work-context/index/events.db` directly, roster-filtered.

### 2a. FAST PATH — run the single-shot gather FIRST (do not hand-query)

**Performance:** the DB work is <0.2s; the old cost was ~10 sequential model-driven
SQL round-trips (each a full turn re-reading this prompt). So gather EVERYTHING in ONE
call, then format/enrich in ONE turn.

```bash
python3 bin/standup_gather.py <YYYY-MM-DD> <scope>   # scope = team | me | <canonical>
```

It emits, per roster member, in a single pass:
- window jira (with **assignee-at-close resolved** + `OWN`/`byActor` tag — credit rule §3 baked in),
- window github + confluence (with page titles),
- current BOARD state (inprog / todo / open-CMR, Epics already filtered, §3b),
- **Slack authored in window** + **@-asks over 7d** with an `answered_by_member` flag
  (the §4b heavy scan, pre-computed).

Read its output, then go straight to formatting (§7). Only fall back to ad-hoc SQL /
`mcp__plugin_context-mode` queries for the ENRICHMENT clause (§8) — the one substantive
detail from a ticket body or slack thread — which is judgement work, not bulk gather.
Run Opsgenie (§6) and that's it. Two tool calls (gather + on-call), not ten.

If the script errors or the roster/identity model changed, fall back to the manual
queries below.

**FIELD SOURCING — do NOT use the window for everything.** Only "Done" is a
window-bounded event; the rest are CURRENT STATE and must be queried as state,
else they go silently empty on a quiet day:

| Field | Source | Why |
|---|---|---|
| **Done** | window events | a real ship that happened *in* the window (PR merged, ticket → terminal, doc authored) |
| **In progress** | current board state | latest status ∈ In Progress/In Review for the assignee (§3b) + open authored PRs — regardless of window |
| **Up next** | current state | currently-open assigned To-Do/Open tickets + open asks + pending reviews + Slack self-commitments & unanswered @-asks (§4b) — not "changed today" |
| **Blockers** | currently-open | unresolved threads / firing alerts that are STILL open, even if started before the window (bound to ~last 7d for recency) |

So the window scopes *Done* and *what changed*; everything else is "what is true on
the board right now". Never report In-progress/Up-next/Blockers as empty just because
nothing changed in the window.

## 3. OWNERSHIP — credit owned work, never transitions (critical)

The team flips tickets To-Do→Done in batches at standup; a `status_change` event's
`actor` is whoever **moved** the board, NOT who did the work, and its `assignee` field
is often blank. **Credit by assigned-at-close, not the transitioner.**

- "Done" / "In progress" for a person = tickets where THEY are the **assignee**
  (resolve via the latest non-null `assignee` for that subject:
  `SELECT assignee FROM events WHERE subject=? AND assignee<>'' ORDER BY ts DESC LIMIT 1`)
  + PRs they **authored** (merged/opened) + commits.
- A member moving someone else's ticket = clerical. Either drop it, or note once at
  team level ("<member> ran the board — moved 4 tickets, not their work"). NEVER render
  a moved-but-not-owned ticket as that person's Done. (Validated bug: several tickets a
  member only *moved* at standup were wrongly rendered as that member's Done — credit
  had gone to the transitioner instead of the assignee-at-close.)

## 3b. "In progress" = CURRENT BOARD STATE, not window events

"In progress" is a *state*, not an event — a ticket can sit In-Progress/In-Review
assigned to someone with no status-change in the window. So DON'T derive it from
window events only (that misses them). For each member, query the current board:
tickets where they are the **assignee** and the ticket's LATEST status (across all
time) ∈ {In Progress, In Review, In Development}:

```sql
SELECT subject, latest_status FROM (
  SELECT e.subject,
    (SELECT to_status FROM events x WHERE x.subject=e.subject AND x.to_status IS NOT NULL
     ORDER BY x.ts DESC LIMIT 1) latest_status
  FROM events e WHERE e.assignee = :member_identity GROUP BY e.subject
) WHERE latest_status IN ('In Progress','In Review','In Development');
```

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

## 4. PRIMARY WORKSTREAM — show domain even on a quiet day

Each member has an owning domain (e.g. one member owns IFT, another TDS). Derive it from
their assigned tickets / authored PRs over a trailing ~30d (or `derived/` project
footprint). Lead the person's section with their domain so a quiet day reads
"`<Name> · <domain> — no tracked activity today`", not silence or borrowed board-moves.
If they genuinely logged nothing, say so — do not fabricate from transitions.

## 4b. SLACK DEEP SCAN — mandatory per-member pass (heavy)

Slack is a FIRST-CLASS source here, not just enrichment. For every active roster
member, run a deliberate Slack scan over the window (and ~7d back for open asks/
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

## 5. LEAVE — live scan + the durable table (both)

Mark members who are on leave for the window; don't expect a standup line or call them
"quiet".

- **Live scan (freshness):** the leaves cron's chat-classify is owner-invoked and lags,
  so scan the window (± a couple days) of roster slack for leave signals directly —
  `body` matching `leave|on leave|sick|fever|unwell|ooo|out of office|day off|taking
  the day|wfh|working from home`. (Validated: a member's sick-leave slack message was
  in events.db but not yet in `team_leaves`.)
- **Durable table:** also read `team_leaves` / `derived/team-leaves.md` for planned
  leave (e.g. a member's multi-day planned leave).
- Render on-leave members as `### <Name> — 🌴 on leave (sick/ooo, <date>)` with the
  permalink; skip the four-line block.

## 6. ONCALL — Opsgenie (live, config-driven)

The on-call source is CONFIG-DRIVEN — read `work-context/config/oncall.yaml`; never
hardcode the schedule name. It supplies `opsgenie.schedule`, `identifier_type`, and
`api_key_env`. Then:

```bash
# values from config/oncall.yaml
curl -s -H "Authorization: GenieKey ${!api_key_env}" \
  "https://api.opsgenie.com/v2/schedules/${schedule}/on-calls?scheduleIdentifierType=${identifier_type}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin).get('data',{});print([p.get('name') for p in d.get('onCallParticipants',[])])"
```
(Currently `schedule: SERVICE-EXAMPLE-POD_schedule`, `api_key_env: OPSGENIE_API_KEY`.)

Map the returned email to a roster canonical and badge that person
`### <Name> — 📟 on-call`. Their incident / alert / log-triage work is EXPECTED — frame
it as on-call duty, not as a red flag or as their feature work.

**On-call gets an explicit `On-call:` line** in their section (in addition to the
badge) summarising the ops load handled while on rotation — incidents triaged, alerts
chased, and CMRs worked. Don't bury it; it's the bulk of their day. Example:
"On-call: triaging the category order failure; pushed a recent TB-diff rectification
([EX-NNNN]) to Change-Approved."

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

## 7. Output — person-first, team summary at the end

One section per roster member (on-leave members get the one-line leave badge instead).
Each active member:

```
### <Name>  [· <primary domain>]  [📟 on-call]
- Done: <plain description of work THEY OWN> ([EX-NNNN](browse-url) / [PR #N]) — real ships only
- In progress: <plain description> ([link]) — proxy for "today"
- Blockers: <what's stuck, in words> — <the one-line ask> ([thread link]) — or "none"
- Up next: <top 3-5 pick-up candidates they own / are asked for; ranked> ([link])
```

Then at the END, `## Team summary`:
- **Blocked / needs attention** — consolidated, most urgent first (what you raise in standup).
- **Notable ships** — 2-4 headline deliveries (by owner).
- **Unowned / stalled** — tickets needing a picker (unassigned / bounced to To-Do), untriaged alerts.
- **Out today** — on-leave members (one line) + the on-call name.

For `me` / `<person>`: just their section.

## 8. Describe every item — never a bare ID, enrich from body + slack

- Lead each item with a plain-English phrase of WHAT it is (reworded from the ticket's
  real summary — pull from its `issue_created` title, NOT a `status_change` transition
  string), then the ID as a trailing clickable link.
- **Enrich** the rendered items with one substantive clause from beyond the title — the
  ticket body/comments, the slack thread that discusses it (search slack `body` for the
  ID or the key domain term), and the parent epic. One line, not a paragraph. Example:
  not "investigate customer-group aggregate drift ([EX-NNNN])" but "customers whose
  cash withdrawals are missing from the withholding year-total table — recon found txn group-ids
  absent from the aggregate, risking under-deducted withholding ([EX-NNNN], withholding epic; flagged
  in #recon)".
- Links: `EX-NNNN` → `https://your-org.atlassian.net/browse/EX-NNNN`; Confluence →
  real `_links.webui` URL (`…/wiki/spaces/<KEY>/pages/<id>/<slug>`, never `/wiki/pages/<id>`),
  section anchor = heading text with spaces→hyphens (`#4.-Hook-Fire-Order`); slack →
  `https://example.slack.com/archives/{CH}/p{ts_no_dot}`.
- **Slack links come pre-built — use them.** `standup_gather.py` emits a ready
  `link=https://example.slack.com/archives/...` field on EVERY slack row (authored +
  @-asks), valid for root posts and replies alike (built from the message's own ts, not
  thread_ts). Copy that `link=` value verbatim — never hand-construct or drop it.
- **Pre-save check (mandatory):** before writing any file, scan every rendered line that
  mentions a slack thread/ask/message ("flagged in", "asked", "thread", "in #channel",
  a teammate quote). Each MUST carry a `[thread](…)` link from the gather's `link=` field.
  If a referenced row truly has no `link=` (rare), append `(no linkable ts)` so the gap is
  explicit — a bare slack reference with no link is a bug, not an option.

## 9. Daily-signal honesty

- Lean on real-timestamp events (PR opened/merged, commits, slack, jira transitions +
  comments). Do NOT lead with SP/Done counts at day granularity (same-day batch-flip).
- "Today / planned" is NOT derivable from board/PR state — "In progress" (still-open
  owned work) is the honest proxy. The ONE exception: a dated first-person Slack
  commitment ("I'll pick up X tomorrow", §4b) is a real plan signal and may be rendered
  as planned, with the thread link. Never invent a plan beyond that.
- Plain language — no cluster IDs, no SP math, no tool jargon.

## 10. Save — md files (mandatory)

Every run writes markdown under `management/standup/<YYYY-MM-DD>/` (`mkdir -p`).
**A same-day re-run OVERWRITES the existing files in place** (latest run wins — the
digest is a snapshot, not a log; stale `-2`/`-3` copies just confuse). Read-only on all
sources. (If you ever need to keep a prior run, copy it out manually first.)

- **`team` scope** writes BOTH:
  - the combined digest → `management/standup/<date>/team.md` (all 7 + team summary), AND
  - one **per-person file** per report → `management/standup/<date>/<canonical>.md`
    (that person's section only — its own header + the four lines / on-call / leave).
    So each report's update is an individual, shareable md (drop into a 1:1, ping the
    person, track over time).
- **`me` / `<person>` scope** writes just `management/standup/<date>/<canonical>.md`.

Each file is self-contained markdown (own title + date + the rendered section). The
chat reply shows the team digest and ends with `**Saved to:** <dir>` listing the files.

## Hard constraints

- Roster = `scope: team` only. Non-roster name in output = bug.
- Credit by assignee/author, never the transitioner.
- On-leave members badged, not expected to report; on-call member badged, incident work expected.
- Never a bare ticket ID; describe + enrich + link.
- Read-only — no writes to events.db, Confluence, Jira, Opsgenie.

# /ask · narrative style — Synthesise + cite (shared by summarize / person_range / team_range / rootcauses)

person_range / team_range ALSO load `.claude/ask/person-template.md` (the locked
output template).

## The bar

Reader = a manager who knows the team but NOT this tool's internals. They must
never see cluster IDs, "reply_count=17", or bare bullet lists of EX-XXXX URLs —
only prose that extracts the *actual decisions, rollout dates, percentages,
post-rollout impact, who decided what* from the underlying threads/tickets/PRs.
Treat AI summaries in Linear / Notion / Slack-AI as the reference quality. If
the output reads like a cluster-ID dump, label-only bullets, or
"AUTHOR/REVIEWER" jargon, the synthesis step didn't happen — go back and read
the JSON deeper.

## Contribution ≠ authorship — critical distinction

Creating a ticket / opening a thread / opening a PR is a WEAK signal of actual
work. Consequential work shows up as: substantive comments on others' tickets
(>300 chars, decision content / rectification SQL / ops steps), commits inside
others' PRs, page-updated deltas + inline comments on others' pages,
substantive slack replies (>200 chars, decisions/approvals/post-rollout
observations), transitions triggered on others' tickets. Lead impact claims
with **substantive contribution**, not authorship — a heavy substantive
responder on a multi-decision Epic usually out-impacts whoever created the
placeholder ticket.

## Mandatory deep-read step

Grounds claims in the source, not the title — `.claude/shared/evidence-grounding.md`.
For the top 5-8 clusters/subjects by relevance, before writing:
1. Pull the underlying member content (issue bodies, thread bodies, PR
   descriptions, MatterAI summaries, page bodies) from the deepread bundle /
   events.db. Cluster `label` + `decisions_json` are summaries of summaries;
   the meaning lives in the raw bodies.
2. Extract concrete facts: numbers, percentages, dates, who
   approved/decided/rolled back, specific actions ("disabled X job", "deployed
   beta-only PR #2268", "rectified ₹X TB diff on account NNNN").
3. Weave cross-source storylines into ONE narrative: "decision made on
   Confluence X, approval requested in #channel Y on date Z, executed via
   ticket EX-NNNN".

## Output shape (section order LOCKED)

1. TL;DR — 5-6 bullets, ≤25 words each. Most consequential first.
2. Signals — 5-7 sub-sections (How he worked / When he worked / What shipped /
   Pace + velocity / Ops track / Workstreams / Quality). Each opens with a
   **bold lead phrase** then 2-5 bullets. NO wall-of-text paragraphs, NO
   metric dumps, NO calculation breakdowns, NO internal terms.
3. Data silent on — what the data can't say. Plain English.
4. Novel observations — emergent patterns specific to this person/window.
5. Gaps / coaching opportunities — patterns to discuss in 1:1.
6. Interventions to consider (person_range only) — L1/L2/L3 menu.
7. Detail — multi-paragraph narrative, themes not clusters. Inline links.
8. Confirmed by data — last. Bullet list with citations; the audit trail.

## Translation hard rules

General rule + grep-check practice: `.claude/shared/plain-language.md`. Below
is the /ask-pipeline-specific elaboration.

- **Rule zero — NO internal entities or pipeline names ANYWHERE in the output,
  "Confirmed by data" included (no exempt section).** Forbidden everywhere:
  the word "cluster" / cluster IDs; engine/script/table names (`person_v3`,
  `person_deepread`, `ask_engine`, `topic_brief`, `events.db`,
  `jira_metrics`); JSON field paths / signal keys (`v1_signals`,
  `project_footprint`, `role_drift`, `domain_ownership`, `delivery.shipped`,
  `sp_completion_rate`, `attribution_chain`, anything with `::`, `_json`,
  `.primary`, literal `window_role`/`lifetime_role`); pipeline jargon
  ("lookahead", "window edge", "boundary artefact", "sandbox"). Every claim
  cites an artefact a human can open — ticket ID (EX-NNNN), PR
  (`owner/repo#N`), doc title, slack link — or a plain-English activity
  description. If a fact can't be expressed without naming the engine, it
  doesn't belong in the output.

- **Name the workstream, never the cluster.** "Cluster 56" → "the service-a
  balance-service and withholding workstream". Translate participant roles:
  AUTHOR → "owns it / drove", DECIDER → "called the shots on", RESOLVER →
  "drove resolution of", REVIEWER → "is the main reviewer", RESPONDER →
  "weighed in on".

- **Prefer project-level voice.** When the output has a `project_footprint`
  block (per-person) or `projects_active_in_window` (team), render workstreams
  via the human-readable `projects.yaml::name` slug names — never cluster
  lists. Bad: "active on clusters 281, 297, 352 + 8 others". Good: "drove
  service-c Revamp + instant-pay on service-a rollout + accounting refactor".

- **Never render cluster-derived counts (`cluster_count`,
  `window_event_count`, `member_count_total`) as numbers in prose.** They are
  sandbox-only: they decide WHICH workstreams to highlight, never appear in
  what gets written. Express scope via real, verifiable artefacts (tickets
  shipped, PRs opened/merged/reviewed, comments, page edits, replies — from
  `contribution` + `assigned_tickets[]` / `prs[]` / `confluence[]`).
  Bad: "touched 3 clusters across 11 events". Good: "owned the
  accounting-refactor track — shipped 7 tickets and reviewed 12 PRs".

- **Use window_role, NEVER lifetime_role, for "what did X do in <window>".**
  Both exist per footprint entry; `top_role_in_project` is already
  window-derived — trust it. lifetime_role is context only — surface it only
  for historical questions or a notable drift ("AUTHOR historically, mostly
  reviewer this month — engagement shape shifted"). Judge window engagement by
  the person's events IN the window, not lifetime cluster size — a 70-member
  workstream with 3 window events is light-touch: mention briefly in artefact
  terms ("touched accounting-refactor lightly — one ticket, a couple of review
  comments") or omit.

- **Translate window mechanics to time.** Never "lookahead" / "window edge" /
  "boundary artefact". Say: "looked one month later", "his April work mostly
  finished in May", "by end of May those tickets had shipped".

- **Never paste raw metrics with calculations.** Bad: "sp_completion 61.1% =
  33.4 / (33.4 + 19.25 + 2.0)". Good: "shipped about two-thirds of his planned
  story points; the remaining third was still in-progress at month-end and
  most of it shipped in early May".

- **Behavioral signals in plain English.** Bad: "p50 first-reply latency 67.9
  min, p90 5941 min". Good: "usually replies on slack within an hour, but a
  handful of asks went unanswered for several days". Bad: "after_hours_share
  25.7%". Good: "about a quarter of his activity falls outside the team's 12-8
  window — lowest of his peers".

- **Distinguish review VOLUME from review DEPTH.** `substantive_pr_reviews`
  counts only long-form prose (>200 chars). When it reads 0, ALSO check the
  raw any-length review count. Frame as "reviewed N PRs but always with short
  approves — never long-form", not "0 reviews".

- **Reviews are activity, NOT deliverables.** What-shipped / Detail cover only
  work the person AUTHORED or DROVE. Review counts live in the how-he-worked
  signal — never itemised as accomplishments.

- **MatterAI flags — only on the person's OWN PRs.** Flags on PRs they merely
  reviewed belong to the author's summary, never the reviewer's.

- **`status → Done` is NOT proof of work.** A dev running standup transitions
  tickets they never coded. Delivery / SP / ownership claims credit by
  **assigned-at-close** — `jira_metrics.py` owns this computation; consume it,
  never reimplement. Transitioner count is clerical: "moved N tickets to Done
  (any assignee)", never "delivered N tickets". Domain ownership = PR-author
  share, not Done-transition count.

- **One sub-section per domain/workstream** — bold lead phrase + bullets of
  specific items (subject ID + title + brief context). Tables are for raw
  activity counts ONLY — never collapse the narrative into a domain matrix.

- **Name the tier band naturally.** Bad: "sp_completion 61.1% reads below SDE2
  band 70-80% per tier_expectations.yaml". Good: "shipped about two-thirds —
  under the SDE2 norm of three-quarters to four-fifths".

- **Sentences short.** 8-14 words. Drop hedging; drop "intentionally" /
  "honestly" / "frankly" / "literally". Just say it.

- **Pre-save grep-check (MANDATORY).** Before writing the file, scan the
  ENTIRE rendered output — every section INCLUDING "Confirmed by data" — for
  these forbidden strings; rewrite any line containing them in plain-English
  artefact terms:
  ```
  cluster   clusters   cluster_count   cluster_id   window_event   member_count
  events?   lookahead   boundary   sandbox   _pct   sp_completion   p50   p90
  ::   _json   .primary   window_role   lifetime_role   role_drift
  v1_signals   project_footprint   domain_ownership   attribution_chain
  person_v3   person_deepread   ask_engine   topic_brief   events.db   jira_metrics
  substantive_   matterai   (plain English)   (analyzed)   (objective baseline)
  ```
  Any engine/script/table name, JSON field path, signal key, cluster
  reference, `_pct`/`p50` metric, or `(…)` heading suffix = rewrite. A
  Confirmed-by-data bullet reads "claim — real artefacts + plain-English
  activity", never "claim — `field.path` = value". Any §Signals paragraph over
  ~4 sentences → break into bullets.

## Length guidance

- summarize: 3-5 paragraphs in Detail, 4-8 inline citations.
- person_range: 4-6 paragraphs in Detail grouped by theme (NOT by cluster) —
  one workstream per paragraph, with concrete actions, dates, decisions, and
  2-3 inline citation links woven into prose.
- attention: TL;DR is the worklist (each bullet states WHAT'S BLOCKED); Detail
  may be skipped if TL;DR covers it.
- ticket_gaps: TL;DR + bullet list of gaps. No Detail needed.
- rootcauses: prose categorised by domain in Detail; cite tickets inline.

## Anti-patterns (refuse to emit)

- Bare cluster_id dumps (`cid=53 rc=17 status=ACTIVE`)
- Source-name parentheticals ("(slack thread)", "(jira ticket)") — name the work
- "AUTHOR/REVIEWER/RESPONDER" jargon from participant_roles
- Generic labels like "[Cluster 153]" — name the work
- subject_id → URL lists at the bottom — links must be inline
- Skipping "Data silent on" because everything looks confirmed
- Padding "Novel observations" with rubric-restatement when nothing emerged
- Calling someone "AUTHOR" of impact when they only created the placeholder
  ticket — check substantive comments + page edits + commits-in-PR first

# /ask · narrative style — Synthesise + cite (shared by summarize / person_range / team_range / rootcauses)

Loaded by the `/ask` router for narrative intents. person_range / team_range ALSO
load `.claude/ask/person-template.md` (the locked output template).

## Output style: narrative grounded in contribution-centric signals

The reader is a manager who knows the team but does NOT know this tool's
internals. They should never see cluster IDs, "reply_count=17", or bare
bullet lists of EX-XXXX URLs. They should read meaningful prose that
extracts the *actual decisions, rollout dates, percentages, post-rollout
impact, who decided what* from the underlying threads/tickets/PRs.

This is the bar. Treat AI summaries in Linear / Notion / Slack-AI as the
reference quality. Lower-quality output (cluster-ID dumps, label-only
bullets, "AUTHOR/REVIEWER" jargon) means you have NOT done the synthesis
step — go back and read the JSON deeper.

## Contribution ≠ Authorship — critical distinction

**Who CREATED a ticket / opened a thread / opened a PR is a weak signal of
actual work.** Most consequential work shows up as:

- Substantive comments on others' tickets (length > 300 chars, contains
  decision content or rectification SQL or operational steps)
- Commits inside others' PRs (commit_in_pr where actor is not the PR opener)
- Page-updated events on Confluence (delta bytes > some threshold) — not
  just page_created
- Inline comments on others' pages
- Substantive Slack replies (length > 200 chars, contains decisions or
  approvals or post-rollout observations)
- State transitions triggered on others' tickets

When framing impact, lead with **substantive contribution**, not ticket
authorship. A heavy substantive responder on a multi-decision Epic is
often more impactful than the person who created the placeholder ticket.

## Mandatory deep-read step

Before writing the answer, for the top 5-8 clusters / subjects by relevance:
1. Pull the underlying member content from `events.db` — issue bodies,
   thread bodies, PR descriptions, MatterAI summaries, page bodies. Do
   NOT rely only on the cluster `label` + `decisions_json`. Those are
   summaries of summaries; the meaning lives in the raw bodies.
2. Extract concrete facts: numbers, percentages, dates, named people who
   approved/decided/rolled back, post-rollout observations, specific
   ticket actions ("disabled X job", "deployed beta-only PR #2268",
   "rectified ₹X TB diff on account NNNN").
3. Connect dots across sources — if a Confluence page documents the
   decision, a Slack thread asks for approval, and a Jira ticket
   executes it, weave them into ONE storyline ("decision made on
   Confluence X, approval requested in #channel Y on date Z, executed
   via ticket EX-NNNN").

## Output shape (sections — order matters)

The reader is a manager who knows the team but does NOT know this tool's
internals. They never see "cluster 56", "lookahead window", "boundary
artefact", "p50 latency", "substantive_pr_reviews" — those are tool-side
terms. Translate every signal into plain English BEFORE it lands in prose.

Section order (locked):

1. TL;DR — 5-6 bullets, ≤25 words each. Most consequential first.
2. Signals — 5-7 sub-sections (one per group: How he worked / When he
   worked / What shipped / Pace + velocity / Ops track / Workstreams /
   Quality). Each sub-section opens with a **bold lead phrase** then
   2-5 bullets. NO wall-of-text paragraphs — bullet every distinct
   observation. NO metric dumps, NO calculation breakdowns, NO
   internal terms.
3. Data silent on — what the data can't say. Plain English.
4. Novel observations — emergent patterns specific to this person/window.
5. Gaps / coaching opportunities — patterns to discuss in 1:1.
6. Interventions to consider (person_range only) — L1/L2/L3 menu.
7. Detail — multi-paragraph narrative, themes not clusters. Inline links.
8. Confirmed by data — last section. Bullet list with citations. This is
   the audit trail for the manager who wants to verify a specific claim.

Hard rules for natural-language translation:

- **NO internal entities or pipeline names ANYWHERE in the output (rule
  zero — applies to every section, audit included).** The reader sees a
  manager's narrative grounded in real, verifiable artefacts — never the
  machinery that produced it. Forbidden everywhere: cluster IDs / the word
  "cluster", engine/script/table names (`person_v3`, `person_deepread`,
  `ask_engine`, `topic_brief`, `events.db`, `jira_metrics`), JSON field
  paths or signal keys (`v1_signals`, `project_footprint`, `role_drift`,
  `domain_ownership`, `delivery.shipped`, `substantive_jira_comments`,
  `sp_completion_rate`, `attribution_chain`, anything with `::`, `_json`,
  `.primary`, `window_role` / `lifetime_role` as literal tokens), and
  pipeline jargon ("lookahead", "window edge", "boundary artefact",
  "sandbox"). Every claim — including in "Confirmed by data" — cites the
  artefact a human can open: a ticket ID (EX-NNNN), a PR (`owner/repo#N`),
  a doc title, a Slack thread link, or a plain-English description of the
  activity ("left long investigative comments on the Year-End-Job tickets").
  If you cannot express a fact without naming the engine, the fact does not
  belong in the output.

- **Never reference cluster IDs.** "Cluster 56" is not a thing the reader
  knows. Name the workstream instead: "the service-a balance-service and withholding
  workstream". Translate `participants_json::role` (AUTHOR / REVIEWER /
  RESPONDER) into plain English: "owns it" / "is the main reviewer" /
  "shows up in the discussion".

- **Prefer project-level voice (project_footprint block).** When the
  output has a `project_footprint` block (per-person) or
  `projects_active_in_window` rollup (team), render workstreams via
  project slug names — NOT cluster lists. Bad: "active on clusters 281,
  297, 352 + 8 others". Good: "drove service-c Revamp + instant-pay on service-a rollout +
  accounting refactor". The slug names in `projects.yaml::name` are
  human-readable; use those. Cluster IDs stay invisible to the reader.
  Map `top_role_in_project` to plain English: AUTHOR → "drove",
  DECIDER → "called the shots on", RESOLVER → "drove resolution of",
  REVIEWER → "reviewed", RESPONDER → "weighed in on".

- **Never render `cluster_count` / `window_event_count` /
  `member_count_total` as numbers in prose.** These are sandbox-only
  signals — used to DECIDE which workstreams to highlight, never shown to
  the reader. The reader does not know what an HDBSCAN cluster is and
  should never need to. Express scope via **real artefacts the reader can
  verify**: tickets shipped, PRs opened/merged/reviewed, comments left,
  Confluence edits, slack replies (from the `contribution` block +
  `assigned_tickets[]` / `prs[]` / `confluence[]` arrays).
  - Bad: "drove 11 workstreams under accounting-refactor", "touched 3
    clusters across 11 events", "1 cluster, 95 events on TD/withholding", "24
    distinct projects touched".
  - Good: "owned the accounting-refactor track — shipped 7 tickets and
    reviewed 12 PRs", "heavy reviewer on the TD/withholding stream — 81 long-form
    comments across 8 reviews".
  - Cluster is the engine; workstream is the surface. Cluster-derived
    counts and cluster IDs appear NOWHERE in the output — not in TL;DR,
    Signals, Novel, Gaps, Detail, AND NOT in the "Confirmed by data" audit
    section either. There is no audit-section exception. The reader never
    sees the engine.

  **Use window_role, NEVER lifetime_role.** Each
  `project_footprint::clusters[]` entry carries both `window_role`
  (derived from events in the asked window) and `lifetime_role` (from
  topic_brief.participants_json — cumulative across all time). For
  "what did X work on in <window>" questions, render against
  `window_role` always. `top_role_in_project` is already derived from
  window_role server-side; trust it.

  `lifetime_role` is for context only ("he's been AUTHOR on this
  workstream historically but only RESPONDER this window"). Surface
  lifetime_role explicitly only when owner asks about historical
  context or when `role_drift_cluster_count > 0` on a slug is
  interesting enough to call out (e.g. "AUTHOR historically, mostly
  reviewer this month — engagement shape shifted").

  **Judge window engagement by the person's events IN the window, not
  lifetime cluster size — but as sandbox reasoning only, never as rendered
  numbers.** A workstream with 70 lifetime members but only 3 of the
  person's events this window is light-touch: use that to decide emphasis
  (brief mention, or omit), then express it in artefact terms — "touched
  accounting-refactor lightly — one ticket, a couple of review comments" —
  NOT "3 clusters across 11 events". `window_event_count` and
  `cluster_count` inform the WHAT-to-write decision; they never appear in
  WHAT gets written (per the rule above).

- **Never say "lookahead" / "window edge" / "boundary artefact" / "primary
  vs companion window".** Translate to time: "looked one month later",
  "his April work mostly finished in May", "by end of May, those tickets
  had shipped". The reader should understand without learning tool jargon.

- **Never paste raw metrics with calculations.** Bad: "sp_completion 61.1%
  = 33.4 shipped / (33.4 + 19.25 in-flight + 2.0 cancelled)". Good:
  "shipped about two-thirds of his planned story points; the remaining
  third was still in-progress at month-end and most of it shipped in
  early May".

- **Behavioral signals translate to plain English.** Bad: "p50 first-reply
  latency 67.9 minutes, p90 5941 minutes". Good: "usually replies on
  slack within an hour, but a handful of asks went unanswered for several
  days". Bad: "after_hours_share 25.7%". Good: "about a quarter of his
  activity falls outside the team's 12-8 window — lowest of his peers".

- **Distinguish review VOLUME from review DEPTH.** The
  `substantive_pr_reviews` metric counts only long-form review prose
  (body > 200 chars). When that metric reads 0, ALSO check raw count of
  any-length reviews. Many ICs review heavily but with short "LGTM" or
  "Requested changes" comments. Frame as: "reviewed N PRs but always
  with short approves — never long-form" rather than "0 reviews".

- **Reviews are activity, NOT deliverables.** The narrative body (What
  shipped / Detail) covers only work the person AUTHORED or DROVE — PRs
  they wrote, tickets they drove to Done, docs they authored, ops actions
  they executed. Review activity ("156 reviews given") is a count — it
  lives in the activity-stats table / "How he worked" signal, never
  itemised as an accomplishment in the narrative. Do not list things they
  reviewed as their output.

- **MatterAI flags — only on the person's OWN PRs.** Critical/quality
  flags from PRs the person authored are narrative-relevant (risk in their
  output). Flags on PRs they merely reviewed belong to the AUTHOR's
  summary, not theirs — never attribute a reviewed-PR flag to the reviewer.

- **`status → Done` is NOT proof of work.** A dev running standup
  transitions tickets they never coded. For delivery / SP / ownership
  claims, credit by **assigned-at-close** (latest assignee before the Done
  event) — `jira_metrics.py` owns this computation; consume it, do not
  reimplement. Transitioner count is a separate clerical signal: render as
  "moved N tickets to Done (any assignee)", never "delivered N tickets".
  Domain ownership = PR-author share in that domain, not Done-transition count.

- **Section structure: one sub-section per domain / workstream**, each a
  bold lead phrase + bullets of specific items (subject ID + title + brief
  context). Tables are for raw activity counts ONLY — never lead with, or
  collapse the narrative into, a domain matrix.

- **Name the tier band naturally.** Bad: "sp_completion 61.1% reads below
  SDE2 band 70-80% per tier_expectations.yaml". Good: "shipped about
  two-thirds — under the SDE2 norm of three-quarters to four-fifths".

- **Sentences short.** 8-14 words. Drop hedging. Drop "intentionally" /
  "honestly" / "frankly" / "literally". Just say it.

- **Pre-save grep-check (mandatory).** Before writing the file, scan the
  ENTIRE rendered output — every section INCLUDING "Confirmed by data"
  (there is no exempt section) — for these forbidden strings and rewrite
  any line that contains them in plain English / artefact terms:
  ```
  cluster   clusters   cluster_count   cluster_id   window_event   member_count
  events?   lookahead   boundary   sandbox   _pct   sp_completion   p50   p90
  ::   _json   .primary   window_role   lifetime_role   role_drift
  v1_signals   project_footprint   domain_ownership   attribution_chain
  person_v3   person_deepread   ask_engine   topic_brief   events.db   jira_metrics
  substantive_   matterai   (plain English)   (analyzed)   (objective baseline)
  ```
  Any engine/script/table name, JSON field path, signal key, cluster
  reference, `_pct`/`p50` metric, or `(…)` heading suffix anywhere in the
  file = rewrite. A "Confirmed by data" bullet must read as "claim — real
  artefacts (EX-NNNN, repo#N, doc title, slack link) + plain-English
  activity", never as "claim — `field.path` = value". Any §Signals
  paragraph over ~4 sentences → break into bullets.

## Length guidance

- summarize: 3-5 paragraphs in Detail with 4-8 inline citations
- person_range: 4-6 paragraphs in Detail grouped by theme (NOT by
  cluster). Each paragraph = one workstream the person owned, with
  concrete actions, dates, decisions, and 2-3 inline citation links
  woven into the prose.
- attention: TL;DR is the worklist (each bullet states WHAT'S BLOCKED).
  Detail section may be skipped if TL;DR fully covers.
- ticket_gaps: TL;DR + bullet list of gaps. No Detail section needed.
- rootcauses: prose categorised by domain in Detail; cite tickets inline.

## Anti-patterns (refuse to emit)

- Bare cluster_id dumps (`cid=53 rc=17 status=ACTIVE`)
- Source-name parentheticals like "(slack thread)" or "(jira ticket)" —
  name the actual work instead
- "AUTHOR/REVIEWER/RESPONDER" technical jargon from participant_roles
- Generic labels like "[Cluster 153]" — name the work
- Bullet lists of subject_id → URL pairs at the bottom — links must be inline
- Skipping the "Data silent on" section because everything looks confirmed
- Padding "Novel observations" with rubric-restatement when nothing emerged
- Calling someone "AUTHOR" of impact when they only created the
  placeholder ticket — check substantive_jira_comments + confluence_edits
  + commit_in_pr first

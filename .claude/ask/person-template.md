# /ask · person output template (person_range + team_range)

Loaded by the `/ask` router for person_range / team_range. The template below is
LOCKED — section order and content rules come from `narrative-style.md`; the
signal groups, reliability gates, and section specs are here.

```
**Answer to: {question}**

## TL;DR

  • 5-6 bullets, ≤25 words each
  • Most consequential action/finding first
  • Each bullet traces to either Signals or Novel observations

## Signals (analyzed)

CONTRIBUTION SIGNALS (work signals — who did stuff)

  authorship                      N    (pr_opened + issue_created + thread_started)
  substantive_pr_commits          N    (commit_in_pr events authored by person)
  substantive_pr_reviews          N    (review comments > 200 chars)
  substantive_jira_comments       N    (comments > 300 chars on others' tickets)
  jira_state_transitions          N    (status_change events triggered by person)
  confluence_edits                N bytes / M events (page_updated by person)
  confluence_inline_comments      N    (on others' pages)
  substantive_slack_replies       N    (replies > 200 chars)
  coordination_spans              N    (own thread/ticket drawing ≥3 distinct replies)
  cited_by_others                 N    (raw/canonical id mentioned in others' content)
  resolver_phrases                N    (explicit "resolved/fixed/deployed/merged by X")
  cross_surface_breadth           N/4  (sources where ≥10 substantive events)
  active_workstreams              N    (clusters touched with status=ACTIVE)
  recurring_share                 X%   (events in RECURRING clusters — deflate if > 30%)

BEHAVIORAL SIGNALS (how / when they engage — slack-derived)

  first_responder_rate            X%   (% of threads-they-replied-to where they
                                        replied first, from actor_behavior_report)
  resolver_rate                   X%   (% of threads where they posted resolution
                                        marker, from actor_behavior_report)
  p50_response_latency            Xm   (median minutes between thread_started and
                                        their first reply — read from actor_behavior_report
                                        OR compute from events.db)
  p90_response_latency            Xm   (worst-case lag — flags if >> p50)
  after_hours_share               X%   (% of events outside 9am-7pm IST —
                                        workload-health signal)
  weekend_share                   X%   (% of events on Saturday/Sunday)
  thread_followup_rate            X%   (% of own thread_started where they
                                        posted ≥1 reply themselves — drop-off signal)
  question_vs_answer_ratio        Q:A  (rough: # replies ending in '?' vs # not)

THROUGHPUT SIGNALS (jira sprint productivity — from events.db schema fields)

  Read `config/tier_expectations.yaml::status_classes` for terminal-state
  classification. Resolve a ticket's current status via:
    SELECT to_status FROM events WHERE subject=? AND to_status IS NOT NULL
    ORDER BY ts DESC LIMIT 1

  Split into shipped (feature delivery), ops_closed (CMR/ops closure),
  cancelled, and in_flight. ICs with high ops_closed share get their
  velocity judged on the ops track, NOT the feature track.

FEATURE TRACK (SP-pointed work)

  story_points_committed          N    (SUM of story_points on tickets
                                        assignee=person AND ever-sprinted
                                        AND status not in cancelled)
  story_points_shipped            N    (same, but status in shipped class)
  sp_completion_rate              X%   (shipped / (shipped + in_flight) —
                                        EXCLUDES cancelled + ops_closed)
  tickets_shipped                 N    (count of tickets currently shipped)
  tier                            X    (read from people.yaml: SDE1/SDE2/SDE3)
  tier_expected_range             X    (read from tier_expectations.yaml)
  tier_deviation                  X    (above-band / in-band / below-band)
                                       ← see Reliability Gates BELOW
                                         before emitting this

OPS TRACK (CMR work — for ops-heavy ICs)

  cmr_authored                    N    (issue_type=CMR, actor=person)
  cmr_assigned                    N    (issue_type=CMR, assignee=person)
  cmrs_closed                     N    (CMRs with to_status in ops_closed
                                        OR shipped — both count as "closed
                                        ops work")
  ops_close_rate                  X%   (cmrs_closed / cmr_authored)
  rectifications_authored         N    (tickets with title LIKE "Fix%" /
                                        "Rectify%" / "Data correction%")

CANCELLATION + QUALITY DRIFT

  cancellation_rate               X%   (tickets in cancelled / total assigned)
                                       ← flag if > 20% — scope churn signal
  tickets_in_flight               N    (status in in_flight class)
  bugs_assigned_to_person         N    (issue_type=Bug, assignee, in window)
  bugs_authored_by_person         N    (issue_type=Bug, actor — finding bugs
                                        vs being blamed for them)

QUALITY SIGNALS (code + ticket quality indicators)

  pr_matterai_quality_p50         X%   (median MatterAI Code_Quality% across the
                                        person's pr_opened in window — extract from
                                        bot comments matching "Code_Quality-NN%")
  pr_matterai_critical_flags      N    (PRs where MatterAI review body contains
                                        "Critical" / "critical issues found")
  pr_revert_count                 N    (PRs with title LIKE "Revert%" by person
                                        OR linked-to person's prior PR)
  bug_to_feature_ratio            X:Y  (bugs_assigned / (Stories + Tasks authored))

Authorship is INTENTIONALLY low in the CONTRIBUTION list — weakest of those.

Behavioral signals are workload-health + style indicators, NOT performance
indicators. High p90 latency or high after-hours share is a flag for the
person's burnout/availability, not for their quality.

Throughput + Quality signals are productivity-and-craft indicators. Always
interpret against tier_expectations.yaml — never as absolute targets.
Surface deviations as flags to discuss, not as verdicts.

### Throughput verdict reliability gates (MANDATORY before emitting tier_deviation)

Before printing a tier_deviation verdict (in-band / below-band / above-band),
check these gates. If ANY gate fails, REFUSE to emit a tier verdict and
print the unreliability flag instead. The data tool's job is to be honest
about what it can and cannot say.

Gate 1 — SP coverage (only on sprinted tickets)

  Team convention: story_points are populated ONLY when a ticket is moved
  into an active sprint. Backlog-only tickets legitimately have story_points
  = NULL — they should NOT degrade SP-coverage.

  - Compute SP_eligible = tickets that have EVER been in a sprint
    (sprint_id IS NOT NULL on issue_created OR ticket has a sprint_change
    event in window). Exclude pure backlog tickets.
  - Compute SP_coverage = (SP_eligible AND story_points IS NOT NULL)
                          / SP_eligible
  - If SP_coverage < 0.70 AND SP_eligible >= 5 (sample-size gate):
    FLAG `sp_coverage_below_70pct` — print
    "Tier-band verdict suppressed: only X% of sprinted tickets have
    story_points set (Y / Z). SP-pointing is being skipped at sprint-load
    time. Surface as a process gap, not a velocity gap."
  - If SP_eligible < 5: FLAG `insufficient_sprinted_tickets` — print
    "Tier-band verdict suppressed: only N sprinted tickets in window —
    too few to compute meaningful velocity."
  - In both cases, surface as a Gap in section 5 with intervention
    candidates (L2 norms: enforce SP-tagging at sprint-load time).

Gate 2 — CMR / ops-heavy role
  - Compute CMR_share = (tickets where issue_type='CMR') / (total assigned)
  - If CMR_share >= 0.30: FLAG `cmr_heavy_role` — print
    "Tier-band verdict needs ops parallel-track: X% of tickets are CMRs
    (production change requests, not story-pointed by convention).
    Standard SP-velocity expectation does not apply cleanly. Compute
    parallel: cmr_closed_count per window vs an ops-band expectation
    (define separately)."

Gate 3 — Window-vs-sprint-cadence sanity
  - Window must span at least 1 sprint (`working_days * 1` per
    config/tier_expectations.yaml). If shorter, FLAG `window_too_short`
    and skip tier_deviation entirely.

When all gates pass, then and only then emit:
  - `tier_deviation: in-band | above-band | below-band` against
    `tier_expected_range`.
  - Frame as "in this window, against SP-tracked work only" — never as
    absolute productivity verdict.

### Ops parallel track (when CMR_share ≥ 0.30)

For ops-heavy ICs (Alice, Bob, others), compute these IN ADDITION
to SP throughput:

  ops_cmrs_authored               N    (issue_type='CMR' authored)
  ops_cmrs_closed_proxy           N    (CMRs with closure-marker comment:
                                        "Reviewed" / "DAM alert was updated" / "Done")
  ops_close_rate                  X%   (closed / authored)
  rectifications_authored         N    (tickets with title LIKE "Fix%" /
                                        "Rectify%" / "Data correction%")

Compare against ops_band_expectation if defined; surface as a separate
verdict from SP throughput. Two parallel tracks: feature velocity (SP)
+ ops velocity (CMR count).

## Confirmed by data

Per-claim mapping back to real, openable evidence — NO field names, NO
cluster IDs, NO engine names (see rule zero). Each bullet = the claim, then
the artefacts a human can open plus a plain-English description of the
activity. Example:

  - "Bob drove the year-end EOD/BOD disable" — he left long investigative
    comments on the Year-End-Job tickets, edited the Year-End-Job page three
    times, and opened the approval thread ([thread](url)). The decision
    rationale is written out in the body of [EX-2479](url), which he
    authored — authorship matters here because the ticket body IS the
    decision.

  - "Bob drove production ledger-balance rectifications" — he commented on
    and moved [EX-1959](url), [EX-2259](url) and [EX-2439](url) to
    done, with the correcting SQL written inline in the ticket bodies.

## Data silent on

User framings the data cannot confirm or deny. Examples:

  - "Highly celebrated performer" — data shows volume + breadth, doesn't
    measure peer perception or stakeholder testimony.
  - "Drove DR-drill" — depends on definition. Data shows ownership of
    EOD/BOD disable piece; multi-person ownership across full DR-drill
    (owner authored parent placeholder, example-dev4 flagged AWS prereq,
    example-dev5 ran deployments).
  - "High impact" — proxy gap: event counts measure volume not influence.
    Critical-path-only-them work, multiplier effects, whiteboard decisions
    don't appear.

ALWAYS surface this section honestly. Never pad. If user's framing IS
well-supported by data, say so explicitly: "framing 'X' is confirmed
by signals Y + Z, no silence here."

## Novel observations (subjective — reading the data, not the rubric)

Things THIS person + window surfaced that the baseline doesn't capture.
Examples of the shape (must come from actual data, not invented):

  - "Every deploy-console deployment ask this window followed the pattern:
    PR link shared first, approval requested second, sync done within
    20 minutes. No exceptions."
  - "Of 9 ledger-balance rectifications across 6 months, Bob wrote
    the SQL inline in 4; commented-on-someone-else's-SQL in 5."
  - "Touched service-b + service-a + deploy-console + 4 Slack channels —
    repos and channels span Platform, deposits, and Ops domains."

This section is mandatory but the SHAPE varies — emergent patterns
specific to this query. If the data is thin or you couldn't surface
anything beyond the rubric: say so plainly ("no novel patterns surface
in this window beyond baseline signals"). Don't pad.

## Gaps / coaching opportunities (signal-derived, NOT judgement)

Patterns the data surfaces that may indicate growth areas or
behavioral flags. Frame as observations to test in 1:1 / growth
conversations, not as verdicts. Distinguish from "Data silent on"
(which is about the data's limits, not the person).

Sources for this section:
- Contribution-signal asymmetries (e.g. ships heavily but reviews
  rarely; authors many tickets but never long-form comments).
- Behavioral signals from slack — p50/p90 response latency,
  after-hours share, thread-followup rate, first-responder vs
  resolver imbalance.
- Concentration risks (single-domain expertise, only one source touched).
- Engagement-channel gaps (engages on slack but never on jira; never
  on Confluence; etc.).
- Process patterns that suggest friction (revert/re-apply churn,
  many-revision contract pages without clear scope, drop-off on own
  threads).

Examples of the shape:

  - "0 substantive PR reviews while writing 69 commits — asymmetric.
    Ships heavily but doesn't engage in peer-review depth. Worth
    discussing whether intentional focus on shipping vs gap in
    reviewer practice."

  - "p90 response latency = 4h while p50 is 12m. Median is healthy
    but tail is long — usually means a few critical asks went
    unanswered overnight. Worth checking if oncall escalation paths
    are working."

  - "after_hours_share = 38% — meaningful share of work outside
    9am-7pm IST. Could be timezone-shifted workstreams, could be
    workload pressure. Worth confirming preference vs imposition."

  - "thread_followup_rate = 23% — opens 4× more threads than they
    follow up on. Could be quick-broadcast pattern (intentional) or
    could indicate dropped balls."

  - "0 substantive jira_comments — engages on others' work via slack
    (148 replies) and confluence (39 inline comments) but not via
    jira comments. May miss async/long-form contributors who default
    to jira."

  - "first_responder_rate = 52% + resolver_rate = 8% — picks up
    threads quickly but rarely the one who closes them. Strong
    triage signal, weaker resolution signal. Could be coaching
    opportunity on driving closure."

  - "5-revision contract page over 2 weeks — could be healthy
    iteration or design churn. Worth confirming scope was clear
    upfront."

  - "Recurring share 0.5% — clean signal, but ALSO no opsgenie
    rotation participation. Could be coverage gap if person is
    expected to share oncall."

  - "100% domain concentration on withholding/deposits/cash. Strength = depth.
    Risk = single-domain expertise; cross-rotation into adjacent
    surfaces would broaden."

This section is mandatory but may be EMPTY: write
`"no notable gaps surface in this window — signals are healthy across
contribution + behavioral dimensions"` if data genuinely doesn't suggest
any. Don't fabricate growth areas to look balanced. Don't pad.

Frame every entry as "worth discussing in 1:1" / "worth confirming"
/ "may indicate" — never as a verdict. The data sees patterns, the
manager + the person know context.

## Interventions to consider (person_range only — menu, manager decides)

ONLY emit this section for the `person_range` intent. Skip entirely
for summarize / attention / etc.

For each gap surfaced in the previous section, propose intervention
options across 3 layers. Frame as a MENU, not a prescription. The
manager picks; the data tool suggests. Skip layers that don't fit
a given gap. Drop the entire section if Gaps was empty.

Layer 1 — Person-level (1:1 conversations)

  Direct discussion prompts the manager can use in 1:1. Frame as
  open questions, not interrogations.

  Example:
    Gap = "0 substantive PR reviews while writing 69 commits"
    L1  = "Ask: 'What stops you reviewing more — bandwidth, comfort,
           or you don't get tagged?' Surface whether intentional vs
           gap in habit."

Layer 2 — Team / process changes

  Norms or process gates the team can adopt. Should be cheap and
  reversible.

  Example:
    Gap = "p90 response latency = 4h, p50 = 12m — tail of unanswered
           critical asks"
    L2  = "Define explicit SLA for @here/@subteam pings (e.g. ack
           within 4h, resolve within 24h). Track weekly via slack
           validate."

Layer 3 — Tool / system changes (build to make patterns visible)

  Auto-surfacing or measurement we can build. Avoid recommending if
  manual process (L1+L2) would suffice.

  Example:
    Gap = "after_hours_share = 38% — workload health unclear"
    L3  = "Auto-alert when after_hours_share spikes >20% over rolling
           4 weeks. Build into cron-status."

Hard rules for this section:

  - Never frame as "X should do Y" — frame as "consider Y" / "worth
    trying Y" / "menu options".
  - Distinguish from Gaps section: Gaps = pattern observation.
    Interventions = what to try about it.
  - One-to-one mapping per gap is NOT required — some gaps need
    only L1, some need L2+L3, some genuinely have no clean
    intervention beyond "more context needed in 1:1".
  - **Only render layers that have content.** If a gap only needs L1,
    show JUST L1 — do NOT print "L2: Skip" or "L3: Skip" lines.
    Skip-lines are clutter; absence already conveys it.
  - If a gap is "data is silent on this" (lives in caveats, not
    Gaps), don't propose interventions — the gap is in OUR data,
    not the person's behaviour.
  - The intervention menu's job is to make the 1:1 prep cheaper,
    not to replace manager judgement.

## Detail

  Multi-paragraph narrative, themes-not-clusters, inline citation
  links woven into prose. Grounded in Signals + Confirmed-by-data
  facts. NO subjects/URLs dumped at the bottom — every link is
  in flow. Short sentences. Reader is a manager — prose, not specs.
```

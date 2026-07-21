# /ask · person output template (person_range + team_range)

Loaded by the `/ask` router for person_range / team_range. The template below is
LOCKED — section order + content rules come from `narrative-style.md`; the
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
  substantive_pr_commits          N    (commit_in_pr authored by person)
  substantive_pr_reviews          N    (review comments > 200 chars)
  substantive_jira_comments       N    (comments > 300 chars on others' tickets)
  jira_state_transitions          N    (status_change triggered by person)
  confluence_edits                N bytes / M events (page_updated by person)
  confluence_inline_comments      N    (on others' pages)
  substantive_slack_replies       N    (replies > 200 chars)
  coordination_spans              N    (own thread/ticket drawing ≥3 distinct replies)
  cited_by_others                 N    (id mentioned in others' content)
  resolver_phrases                N    ("resolved/fixed/deployed/merged by X")
  cross_surface_breadth           N/4  (sources with ≥10 substantive events)
  active_workstreams              N    (clusters touched, status=ACTIVE)
  recurring_share                 X%   (events in RECURRING clusters — deflate if > 30%)

BEHAVIORAL SIGNALS (how / when they engage — slack-derived)

  first_responder_rate            X%   (replied first in threads-they-replied-to)
  resolver_rate                   X%   (threads where they posted the resolution marker)
  p50_response_latency            Xm   (median thread_started → their first reply)
  p90_response_latency            Xm   (worst-case lag — flag if >> p50)
  after_hours_share               X%   (events outside working hours — workload health)
  weekend_share                   X%   (events on Sat/Sun)
  thread_followup_rate            X%   (own threads where they posted ≥1 reply — drop-off signal)
  question_vs_answer_ratio        Q:A  (replies ending '?' vs not)

THROUGHPUT SIGNALS (jira sprint productivity)

  Terminal-state classes: `config/tier_expectations.yaml::status_classes`.
  Current status of a ticket = latest non-null to_status event.
  Split tickets into shipped / ops_closed / cancelled / in_flight. ICs with
  high ops_closed share are judged on the OPS track, not the feature track.

FEATURE TRACK (SP-pointed work)

  story_points_committed          N    (SUM story_points, assignee=person,
                                        ever-sprinted, not cancelled)
  story_points_shipped            N    (same, status in shipped class)
  sp_completion_rate              X%   (shipped / (shipped + in_flight);
                                        EXCLUDES cancelled + ops_closed)
  tickets_shipped                 N
  tier                            X    (people.yaml: SDE1/SDE2/SDE3)
  tier_expected_range             X    (tier_expectations.yaml)
  tier_deviation                  X    (above/in/below-band — see Reliability
                                        Gates BELOW before emitting)

OPS TRACK (CMR work — for ops-heavy ICs)

  cmr_authored / cmr_assigned     N    (issue_type=CMR)
  cmrs_closed                     N    (to_status in ops_closed OR shipped)
  ops_close_rate                  X%   (closed / authored)
  rectifications_authored         N    (title LIKE "Fix%" / "Rectify%" /
                                        "Data correction%")

CANCELLATION + QUALITY DRIFT

  cancellation_rate               X%   (cancelled / total assigned — flag if
                                        > 20%: scope-churn signal)
  tickets_in_flight               N
  bugs_assigned_to_person         N    (issue_type=Bug, assignee)
  bugs_authored_by_person         N    (finding bugs vs being blamed for them)

QUALITY SIGNALS (code + ticket quality)

  pr_matterai_quality_p50         X%   (median MatterAI Code_Quality% on own
                                        pr_opened — from "Code_Quality-NN%" bot comments)
  pr_matterai_critical_flags      N    (own PRs where review body has "Critical")
  pr_revert_count                 N    (title LIKE "Revert%" by person, or
                                        linked to person's prior PR)
  bug_to_feature_ratio            X:Y  (bugs_assigned / Stories+Tasks authored)

Authorship is INTENTIONALLY low in the CONTRIBUTION list — weakest signal.
Behavioral signals are workload-health + style indicators, NOT performance —
high p90 or after-hours share flags burnout/availability, not quality.
Throughput + Quality read against tier_expectations.yaml — never absolute
targets; surface deviations as flags to discuss, not verdicts.

### Throughput verdict reliability gates (MANDATORY before emitting tier_deviation)

If ANY gate fails, REFUSE the tier verdict and print the unreliability flag
instead. The tool's job is honesty about what it can and cannot say.

Gate 1 — SP coverage (sprinted tickets only). Team convention: story_points
  are set only at sprint-load; backlog-only tickets legitimately have NULL —
  they must NOT degrade coverage.
  - SP_eligible = tickets ever in a sprint (sprint_id on issue_created OR a
    sprint_change event in window).
  - SP_coverage = (SP_eligible with story_points NOT NULL) / SP_eligible.
  - SP_coverage < 0.70 AND SP_eligible ≥ 5 → FLAG `sp_coverage_below_70pct`:
    "Tier-band verdict suppressed: only X% of sprinted tickets have
    story_points set (Y / Z). SP-pointing is being skipped at sprint-load
    time. Surface as a process gap, not a velocity gap."
  - SP_eligible < 5 → FLAG `insufficient_sprinted_tickets`: "Tier-band verdict
    suppressed: only N sprinted tickets in window — too few for meaningful
    velocity." Either flag also lands in Gaps with an L2 intervention
    (enforce SP-tagging at sprint-load).

Gate 2 — CMR / ops-heavy role. CMR_share = CMR tickets / total assigned.
  ≥ 0.30 → FLAG `cmr_heavy_role`: "Tier-band verdict needs ops parallel-track:
  X% of tickets are CMRs (not story-pointed by convention). Standard
  SP-velocity expectation does not apply cleanly." Compute the ops parallel
  track below.

Gate 3 — Window-vs-sprint-cadence. Window must span ≥ 1 sprint
  (working_days per tier_expectations.yaml); shorter → FLAG `window_too_short`
  and skip tier_deviation entirely.

All gates pass → emit `tier_deviation: in-band | above-band | below-band`
against `tier_expected_range`, framed "in this window, against SP-tracked work
only" — never as an absolute productivity verdict.

### Ops parallel track (when CMR_share ≥ 0.30)

Compute IN ADDITION to SP throughput: ops_cmrs_authored, ops_cmrs_closed_proxy
(closure-marker comment: "Reviewed" / "DAM alert was updated" / "Done"),
ops_close_rate, rectifications_authored. Compare against ops_band_expectation
if defined; render as a SEPARATE verdict from SP throughput — two parallel
tracks: feature velocity (SP) + ops velocity (CMR count).

## Confirmed by data

Per-claim mapping back to real, openable evidence — NO field names, cluster
IDs, or engine names (rule zero). Each bullet = the claim, then artefacts a
human can open + a plain-English activity description. Shape:

  - "Bob drove the year-end EOD/BOD disable" — he left long investigative
    comments on the Year-End-Job tickets, edited the Year-End-Job page three
    times, and opened the approval thread ([thread](url)). The decision
    rationale is written in the body of [EX-2479](url), which he authored —
    authorship matters here because the ticket body IS the decision.

## Data silent on

User framings the data cannot confirm or deny. Shapes: "highly celebrated
performer" (data shows volume + breadth, not peer perception); "high impact"
(event counts measure volume not influence — critical-path work, multiplier
effects, whiteboard decisions don't appear). ALWAYS surface honestly, never
pad. If a framing IS well-supported, say so: "framing 'X' is confirmed by
signals Y + Z, no silence here."

## Novel observations (subjective — reading the data, not the rubric)

Emergent patterns THIS person + window surfaced that the baseline doesn't
capture (must come from actual data, e.g. "every deploy-console deployment
ask followed the pattern: PR link first, approval second, sync within 20
minutes — no exceptions"; "of 9 ledger-balance rectifications, wrote the SQL
inline in 4, commented on someone else's in 5"). Mandatory section, shape
varies. Thin data → say plainly "no novel patterns surface in this window
beyond baseline signals". Don't pad.

## Gaps / coaching opportunities (signal-derived, NOT judgement)

Patterns that may indicate growth areas or behavioral flags. Frame as
observations to test in 1:1, never verdicts ("worth discussing" / "worth
confirming" / "may indicate"). Distinct from "Data silent on" (that's the
data's limits; this is about the person).

Sources: contribution asymmetries (ships heavily, never reviews long-form;
authors tickets, never comments); behavioral tails (p90 >> p50,
after-hours/weekend share, thread-followup drop-off, first-responder high +
resolver low); concentration risks (single domain / single source);
engagement-channel gaps (slack-only, never jira/confluence); friction
patterns (revert churn, many-revision pages, drop-off on own threads).

Example shapes:
  - "0 substantive PR reviews while writing 69 commits — asymmetric. Worth
    discussing intentional shipping focus vs reviewer-practice gap."
  - "p90 latency 4h while p50 is 12m — healthy median, long tail; a few
    critical asks likely sat overnight. Worth checking escalation paths."
  - "after_hours_share 38% — could be timezone-shifted work or workload
    pressure. Worth confirming preference vs imposition."
  - "100% domain concentration on withholding/deposits/cash — depth as
    strength, single-domain risk; cross-rotation would broaden."

Section is mandatory but may be EMPTY: write "no notable gaps surface in this
window — signals are healthy across contribution + behavioral dimensions" if
the data genuinely suggests none. Don't fabricate balance.

## Interventions to consider (person_range ONLY — menu, manager decides)

Skip entirely for other intents. For each Gap, propose options across 3
layers; frame as a MENU, not a prescription. Drop the section if Gaps was
empty.

  L1 — Person-level: open 1:1 discussion prompts, not interrogations.
    e.g. Gap "0 substantive reviews, 69 commits" → "Ask: 'what stops you
    reviewing more — bandwidth, comfort, or not getting tagged?'"
  L2 — Team/process: cheap, reversible norms.
    e.g. Gap "p90 4h tail" → "explicit SLA for @here pings: ack 4h,
    resolve 24h; track weekly."
  L3 — Tool/system: build auto-surfacing ONLY when manual L1+L2 won't do.
    e.g. Gap "after-hours 38%" → "auto-alert when share spikes >20% over
    rolling 4 weeks."

Hard rules: never "X should do Y" — "consider / worth trying Y". Gaps =
observation, Interventions = what to try. One-to-one mapping NOT required —
some gaps need only L1, some L2+L3, some just "more context in 1:1". **Only
render layers that have content** — no "L2: Skip" lines; absence conveys it.
Data-limit caveats get NO interventions (the gap is in our data, not the
person). The menu's job is cheaper 1:1 prep, not replacing manager judgement.

## Detail

  Multi-paragraph narrative, themes-not-clusters, inline citation links woven
  into prose. Grounded in Signals + Confirmed-by-data facts. NO subjects/URLs
  dumped at the bottom — every link in flow. Short sentences. Reader is a
  manager — prose, not specs.
```

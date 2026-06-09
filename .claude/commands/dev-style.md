Per-developer working-style profile derived from the `actor_behavior` view. Read-only. Owner-invoked.

Pulls metrics from `state/actor_behavior_report.json` (built by `derive/actor_behavior.py report`) and narrates a person's behavior across incident-flavored clusters: first-responder rate, resolver rate, reply latency, domain spread.

## Usage — `/dev-style [person]`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run anything.

**What it does:** Read-only per-developer working-style profile from the `actor_behavior` view — first-responder rate, resolver rate, reply latency, domain spread.

**Modes:**
- no arg → top-10 leaderboard by threads touched.
- `<person>` → full working-style profile: first-responder rate, resolver rate, reply latency (p50/p90), domain spread, across incident-flavored clusters.

**Usage:** `/dev-style [person]`
- `person` (optional) — name substring (case-insensitive) or raw slack U-id. Empty → top-10 leaderboard by threads touched.

## Arguments

`$ARGUMENTS` = the person identifier. Accepts substring match (case-insensitive) against canonical name OR raw slack U-id. Examples:

```
/dev-style frank
/dev-style bob
/dev-style eve
/dev-style U0EXAMPLE
```

If `$ARGUMENTS` is empty: print the top-10 leaderboard by `threads_touched` and stop.

## Phase 1 — Setup + report freshness

```bash
cd $HOME/work-context
```

Check `state/actor_behavior_report.json` exists. If missing OR older than the most recent `topic_brief.computed_at`, rebuild:

```bash
.venv/bin/python derive/actor_behavior.py report
```

The rebuild is cheap (a few seconds at ~30k-subject corpus scale). Always safe to rerun.

## Phase 2 — Resolve person

Run substring match via the show subcommand:

```bash
.venv/bin/python derive/actor_behavior.py show --person "$ARGUMENTS"
```

The script:
- finds all actors whose canonical name OR raw id contains `$ARGUMENTS` (case-insensitive)
- prints one profile block per match
- if no match, prints the first 20 available actor names

If multiple matches and they look like different people (e.g. `eve` matches `eve-example` AND `<raw:your-org-eve03>`): show all, then call this out — these may be the same person with unmapped github_login, candidate for `people.yaml` update.

## Phase 3 — Narrate (optional, on request)

After the raw profile prints, if the owner asks for narrative interpretation, render something like:

```
{person} is a {style} responder.

  - Touches {N} incident-flavored clusters; authors {A} of them.
  - First-responder in {fr}% of threads they didn't author (denominator: {denom}).
  - Resolution markers (resolved/fixed/merged/PR-link) appear in {rr}% of their replies.
  - Median first-reply latency: {p50}; p90: {p90}.
  - Domain focus: {top-cluster-1}, {top-cluster-2}.

  Reading: {one-line plain-English summary — e.g. "fast first-responder but
  hands off to others for the fix" / "slow to engage but high resolver rate
  when they do" / "deep focus on DB ops, light on other domains".}
```

Use these heuristics for the reading:

| first_responder_rate | resolver_rate | Reading |
|---|---|---|
| > 0.50 | > 0.50 | "jumps in AND drives the fix"  |
| > 0.50 | < 0.20 | "fast first-responder, hands off"  |
| < 0.20 | > 0.50 | "slow to engage but resolves when involved"  |
| < 0.20 | < 0.20 | "occasional participant / observer"  |
| else | else | "balanced participant"  |

For latency:

| p50 | Reading |
|---|---|
| ≤ 5 min | "always-on" |
| 5–30 min | "responsive" |
| 30 min – 4 h | "deliberate"  |
| > 4 h | "asynchronous / OOO-prone" |

## Phase 4 — Caveats

State at the bottom of every profile:

```
Caveats:
  - Scope: incident-flavored clusters only (root_cause IS NOT NULL).
    Channel-coordination, sprint-planning, and release-only work is excluded.
  - Sample size: {threads_touched} threads. Confidence is lower below 10 threads.
  - Bot/system actors (oncall bot, automation) are NOT filtered yet — flag
    if a `<raw:...>` actor with bot-shaped behavior shows up in results.
  - Resolution markers are pattern-based ("resolved", "merged", PR/commit
    URLs). Resolutions documented in Jira but not in Slack do not count.
```

## Hard constraints

- Read-only. NEVER write to `topic_brief` or `events` from this skill.
- NO LLM API calls. All numbers come from `derive/actor_behavior.py`.
- If owner asks for a narrative summary: produce it from the raw profile,
  do NOT fabricate numbers.

## Top-leaderboard mode (no $ARGUMENTS)

If `$ARGUMENTS` is empty, run:

```bash
.venv/bin/python derive/actor_behavior.py report
```

(idempotent — prints the top-10 leaderboard alongside the rebuild stats)

Or for a different ranking metric:

```bash
.venv/bin/python derive/actor_behavior.py top --metric first_responder_rate
.venv/bin/python derive/actor_behavior.py top --metric resolver_rate
.venv/bin/python derive/actor_behavior.py top --metric first_reply_latency_p50_sec -n 20
```

## After write

Owner can verify the underlying view directly:

```bash
sqlite3 index/events.db "
  SELECT cluster_id, label, status, root_cause
    FROM topic_brief
   WHERE root_cause IS NOT NULL
   ORDER BY last_activity_ts DESC"
```

Anything new in `root_cause` → re-run `derive/actor_behavior.py report` to pull the new cluster into scope.

# Shared rule — date-range grammar (IST)

Loaded by any skill that parses a natural-language time window (`/ask`, `/standup`,
`/retro`, `/pulse`, `/pr-quality`, …). Single source of truth for relative-date parsing
and the team's working-hours window. Skill defaults (e.g. standup = yesterday, pr-quality
= last 45d) stay in the skill.

## "Today" source

Today's date is provided by the cron-status SessionStart hook as `currentDate`. All
relative parsing is anchored to that, in **IST** (UTC+5:30).

## Relative-date → bounds

- **yesterday**   → since = today−1d 00:00 IST, until = today 00:00 IST
- **last N days** → since = today−N 00:00 IST, until = now
- **this week**   → since = Monday 00:00 IST of the current week, until = now
- **past month**  → since = today−30d, until = today
- **a named month** ("march") → since = `<current-year>-03-01`, until = `<current-year>-04-01`
  (first day of the month → first day of the next month)
- **a single day** (`2026-06-05`) → that calendar day, 00:00 → next-day 00:00 IST

## Emit as ISO8601

Emit bounds as ISO8601 (`YYYY-MM-DDTHH:MM:SSZ`). Engines compare against `events.ts`
(already ISO). When a skill computes its own `START_TS` / `END_TS`, they are ISO8601 UTC
strings derived from the IST bounds above.

## Working-hours window (after-hours / activity-shape metrics)

The team's working window is **12:00–20:00 IST**, NOT 09:00–19:00. Any "after-hours" /
activity-shape signal is computed against that 12–20 IST window. (This is computed in code
— `person_profile.py` — skills consume it; don't re-derive it in prose, and never render the
raw metric: translate to plain English per `.claude/ask/narrative-style.md`.)

## Weekend / holiday guard

If a default window ("yesterday") lands on a weekend/holiday with ~no activity, say so and
offer the last working day rather than rendering an empty result.

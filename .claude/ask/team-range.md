# /ask · team_range — dispatch chunk

Loaded by the `/ask` router (Phase 3). Before rendering, ALSO Read:
- `.claude/ask/person-range.md` — the per-person pipeline + field map.
- `.claude/ask/narrative-style.md` — output style + translation hard rules.
- `.claude/ask/person-template.md` — the per-person output template.

Whole-team narrative. Loop `person_range` over every entry in
`config/people.yaml::people` whose `canonical` is in the core team (Tier-0
ICs — those with a `role` field set, e.g. SDE1/SDE2/SDE3). For each person:

1. Run `derive/person_profile.py --name <canonical> --since X --until Y` and
   capture JSON.
2. Run `derive/ask_engine.py person --name <canonical> --since X --until Y`
   for cluster material.
3. Render a per-person section using the same person_range template
   (TL;DR + Signals + Confirmed + Data silent on + Novel + Gaps +
   Interventions + Detail) — but trimmed: TL;DR + Signals + 1-paragraph
   Detail.

## Parallel fan-out (speed — REQUIRED for ≥3 people)

Do NOT run the per-person loop sequentially in the main thread. Fan out one
subagent per person, 3–4 concurrently (matches the slack-backfill concurrency
lesson: ramp only if the first batch succeeds). Each subagent prompt must be
self-contained:

- Tell it to Read `.claude/ask/person-range.md`, `.claude/ask/narrative-style.md`,
  and `.claude/ask/person-template.md` first.
- Give it: canonical name, since/until ISO, `cd $HOME/context/work-context`,
  and steps 1–3 above.
- It returns ONLY the rendered per-person markdown section (trimmed shape:
  TL;DR + Signals + 1-paragraph Detail) plus a one-line JSON of
  `{sp_attributed, tickets_shipped, tier_deviation}` for the velocity table.

The main thread does NOT re-run the scripts — it assembles the returned
sections, then writes the overview + table:

Prefix the file with a **Team overview** paragraph (3-5 sentences) and a
**Team velocity table** (per-person `sp_attributed`, tickets_shipped,
tier_deviation verdict). Section order: people with strongest signals
(OWNED ≥1 domain + above-band) appear first; within tier, sort by
event volume descending.

Output: `management/narratives/team/<since>-to-<until>.md`.

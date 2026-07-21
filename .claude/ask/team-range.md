# /ask · team_range — dispatch chunk

Loaded by the `/ask` router (Phase 3). Before rendering, ALSO Read:
- `.claude/ask/person-range.md` — the per-person pipeline + field map.
- `.claude/ask/narrative-style.md` — output style + translation hard rules.
- `.claude/ask/person-template.md` — the per-person output template.

Whole-team narrative. Loop `person_range` over every entry in
`config/people.yaml::people` whose `canonical` is in the core team (Tier-0
ICs — those with a `role` field set, e.g. SDE1/SDE2/SDE3). For each person:

1. Run the person_range bundle call (the ONLY script call per person):
   `derive/person_v4_manifest.py --name <canonical> --since X --until Y
   --bundle-dir /tmp` — writes `/tmp/<canonical>_{manifest,v3,deep}.json`.
2. Follow `person-range.md`'s render flow from those on-disk files (manifest
   = selection authority; v3/deep for phrasing fields + citation quotes).
3. Render a per-person section using the same person_range template
   (TL;DR + Signals + Confirmed + Data silent on + Novel + Gaps +
   Interventions + Detail) — but trimmed: TL;DR + Signals + 1-paragraph
   Detail.

## Parallel fan-out (speed — REQUIRED for ≥3 people)

Do NOT run the per-person loop sequentially in the main thread. Fan out one
subagent per person — ALL people in ONE message (a single batch of concurrent
Agent calls), no ramp-up. The old 3–4-at-a-time cap was borrowed from the
slack-backfill lesson, but that limit protects the Slack **API**; these
subagents only read local sqlite (events.db is fine with many concurrent
readers) and run local scripts. Full-width fan-out makes team_range wall-clock
≈ one person's time. Each subagent prompt must be self-contained:

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

Concise, leave-aware 1:1 pulse for one person: how they're doing in a recent short window vs the prior equal window. Owner-invoked. Read-only.

## Usage — `/pulse <person> [weeks=N] [asof=YYYY-MM-DD]`

If invoked with `help`, `-h`, or `--help`: print this Usage block verbatim and STOP.

**What it does:** A short trend read for 1:1 prep — NOT the full audit profile that
`/ask <person>` produces. It compares a person's *recent* window against the *prior*
equal window and answers one question: **is this person trending up, down, or flat —
adjusted for leave and small-sample noise.**

The defining rule: **it is leave-aware.** A blind two-week-vs-two-week diff screams
"productivity collapsed" the moment someone takes a few days off. This skill checks
effective working days + leave mentions FIRST, and never calls a decline that is
really just fewer days at the desk.

**When to use which:**
- `/pulse <person>` → "how's X doing lately, up or down?" — 1-page, pre-1:1.
- `/ask <person> in <month>` → full evidence-pack profile for a review.

**Params:**
- `person` (required) — substring match against `canonical` in `config/people.yaml`.
- `weeks=N` (optional, default 2) — window length; recent = last N weeks, prior = the N weeks before.
- `asof=YYYY-MM-DD` (optional, default today IST) — pretend "now" is this date.

## Phase 1 — Run the engine

```bash
cd work-context
.venv/bin/python derive/pulse.py --name "<person>" [--weeks N] [--asof YYYY-MM-DD]
```

If it returns `{"error": ...}` (no person match or multiple), surface that and ask
which person. Otherwise read the JSON. Top-level keys:

- `person`, `role`, `asof`, `weeks`, `windows` (recent + prior date ranges).
- `work_mix` — recent vs prior (`feature` / `platform` / `mixed` / `ops`). A shift
  here is itself a signal — e.g. flipped to platform = more DB/infra/CMR work.
- `working_days` — `{touched, active}` per window. **`active`** (days with ≥5
  authored events) is the leave-adjusted denominator. Use it, not `touched`.
- `leave` — the person's own Slack messages in the recent window that read as
  leave/OOO/sick, with timestamps + excerpts + links.
- `metrics[]` — each `{label, recent, prior, direction, trend, higher_is_better}`.
  **`trend` is the goodness verdict (`better`/`worse`/`flat`/`n/a`) — use it.**
  Do NOT re-derive up/down from raw numbers (rank, latency, after-hours, abandons
  are all "lower is better" — `trend` already handles that inversion).
- `recent_work[]` — concrete PRs merged/opened + tickets created in the recent
  window, for citation.
- `flags[]` — pre-computed caveats (leave count, active-days ratio, small-window).

## Phase 2 — Interpret (leave-adjust BEFORE calling a trend)

This is the whole point of the skill. Apply in order:

1. **Leave gate first.** If `leave` is non-empty OR recent `active` days are
   meaningfully below prior (say `active_recent / active_prior < 0.8`), the headline
   is **NOT "productivity down"**. Volume metrics (story points, tickets, commits,
   reviews, total events) drop mechanically with fewer days. State the leave/fewer-days
   reason and judge on per-day shape + quality instead.
2. **Normalise volume per active day in your head.** ~half the working days → roughly
   half the output is expected and flat, not a decline. Only call a volume trend
   "down" if it fell *faster than* the working-days ratio.
3. **Quality + behaviour are NOT volume — read them directly.** Critical flags,
   abandoned PRs, code-quality, after-hours share, response latency don't scale with
   days off. A real regression here (e.g. abandons up, latency much worse, after-hours
   spiking) is worth surfacing even in a leave-shortened window. A real improvement
   (after-hours down, cleaner PRs) is worth crediting.
4. **Work-mix shift** — if `work_mix` flipped, say so plainly ("more platform/DB work
   than the prior fortnight") — it reframes any story-point dip (points under-score
   platform work, same rule as `/ask`).
5. **Small-sample humility.** Behavioural rates (first-responder %, latency) ride on
   few threads over N weeks — call them "directional", never a verdict.

Decide ONE headline: **up / steady / down / mixed**, leave-adjusted. That headline is
the bottom line.

## Phase 3 — Render (concise — ~1 page, this is NOT the full profile)

Plain English only. Same anti-jargon rule as `/ask`: NEVER expose engine/script names
(`pulse.py`, `person_v3`, "cluster", field paths). Cite only real artefacts a human can
open — PRs (`owner/repo#N`), tickets (PROJ-NNNN), Slack links. Sections, in order:

```markdown
# 1:1 Pulse — <Name>

**Generated:** <asof> IST
**Window:** last <N> weeks (<recent.since> → <recent.until>) vs prior <N> weeks (<prior.since> → <prior.until>)
**For:** 1:1 prep — read in 60 seconds

---

## Bottom line

**<up / steady / down / mixed — leave-adjusted, one bold sentence>.**
<2-4 short lines: the real read. If leave drove the numbers, say it here and name
the 1:1 note it implies ("welcome back / how are you feeling", not "why slow").>

## Trend (recent vs prior <N> weeks)

| Signal | Last <N> wks | Prior <N> wks | Read |
|---|---|---|---|
| Effective working days | ~<active_r> | ~<active_p> | <leave reason if any> |
| <metric label> | <recent> | <prior> | <better/worse/flat in plain words> |
| … one row per metric that moved or matters … |

*(<N>-week windows are noisy and below the reliable-rating threshold — a pulse, not a verdict.)*

## What they actually did in their working days

- <2-4 bullets from recent_work — concrete PRs/tickets, plain English, with links>

## Talking points for the 1:1

1. <most important — health/re-ramp if leave, else top trend>
2. <second>
3. <third — or "no red flags this fortnight" if genuinely clean>

## Caveats

- <pull from flags[] + work-mix shift + small-sample, in plain English>
```

Tables ARE allowed here (a metric comparison is what a table is for) — this is a
skill-defined format, so the "no tables in chat" preference does not apply.

Keep it tight. One screen. If everything is healthy, say so and don't pad.

## Phase 4 — Save (mandatory)

Write the rendered output to `management/pulse/<canonical>-<asof>.md`. Header,
never-overwrite, `Saved to:` footer, Write-tool/mkdir: per
`.claude/shared/output-save-conventions.md`.

## Hard rules

- **Read-only.** Never write to `topic_brief` / `events` / `embedding`. No LLM API calls.
- **Leave before decline.** Never headline a productivity drop without first ruling out
  fewer working days / leave. The helper hands you the leave + active-days; use them.
- **Use `trend`, not raw arrows.** The helper already inverts lower-is-better metrics.
- **Cite real artefacts only.** No engine/script/field names anywhere in the output.
- **Pulse, not verdict.** N-week windows are below the reliability threshold; never emit
  a tier/velocity verdict here — that's `/ask`'s job over a month+.
- If `recent` and `prior` are both near-empty (person inactive both windows, e.g. long
  leave), say so plainly and stop — don't manufacture a trend from noise.

## Smoke test

```bash
.venv/bin/python derive/pulse.py --name grace --asof 2026-06-11
.venv/bin/python derive/pulse.py --name alex --weeks 3
```

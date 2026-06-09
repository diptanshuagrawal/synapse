Render the PR-quality friction report (per-PR, per-dev, human-vs-agentic gap).

Consumption half of the PR-quality scorer (PRD: `prd/pr-quality-scorer.md`).
Mechanical friction + any classified comments are already in the DB; this skill
turns the data bundle into a stakeholder-facing report with coaching
actionables and bridging suggestions.

## Usage — `/pr-report [date|days] [all-authors]`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run anything.

**What it does:** Renders the PR-quality friction report — per-PR, per-dev, human-vs-agentic gap — from already-classified data, with coaching actionables.

**Part of:** the PR-quality scorer (PRD `prd/pr-quality-scorer.md`). This is the RENDER half — classify via `/pr-quality` first. Turns mechanical friction + classified comments into a stakeholder report: per-PR friction, per-dev rollup, human-vs-agentic gap, plus coaching/bridging actionables.

**Usage:** `/pr-report [date|days] [all-authors]`
- optional — start date (`2026-04-01`) or day count (`45`); empty → all merged PRs. Add `all-authors` to widen scope.

Optional arg `$ARGUMENTS`: a date (`2026-04-01`) or day count (`45`) → report
window. Empty → all merged PRs. Append `all-authors` anywhere in the args to
widen scope (see below).

**Scope: team-only by default.** Only PRs authored by a `scope: team` member
(people.yaml) are included. The bundle's `scope` field echoes `team` or
`all-authors`. Pass `--all-authors` for the org-wide view.

This is a SKILL OUTPUT — use the report format below, NOT chat-reply style.

**Always persist the rendered report as markdown** (Phase 3) under
`management/pr-quality/`, in addition to printing it to chat.

## Phase 1 — Pull the data bundle

```bash
cd $HOME/context/work-context && .venv/bin/python derive/pr_quality_report.py --since <ISO-date>
```

Convert a day-count arg to an ISO date for `--since`. Omit `--since` for all.
Add `--all-authors` if the user asked for the org-wide (non-team) view.
The script prints a JSON bundle: `scope`, `friction_bands`, `top_friction[]`,
`per_dev`, `coverage_gap`, `classification_coverage`, `category_weights`.

If you want fresher scores first, run `derive/github_metrics.py --since <date>`
(team-only; add `--all-authors` to match) to recompute `pr_friction`, then
re-run the report script.

## Phase 2 — Render the report

Read the bundle and produce these sections.

### TL;DR
2-3 sentences: how many merged PRs, what share were clean vs high-friction, and
the single biggest recurring friction theme.

### How to read the friction score
Always include this legend so the numbers aren't opaque. The score is **review
cost per unit of change** — higher = more back-and-forth to get it merged. It
combines: extra review rounds, rework commits (commits pushed after the first
human review), a changes-requested penalty, a slow-merge penalty, and the
*severity* of review comments (correctness / business-logic / security weigh
most; naming / nits weigh least; human comments count double a bot's). That raw
total is divided by √(lines-changed/100) so a big PR isn't punished for being
big, and capped at 100. Bands:
- **clean (0)** — single round, no rework, nothing flagged.
- **low (<10)** — normal review, minor comments.
- **moderate (10–25)** — a few rounds or some real findings.
- **high (25–50)** — repeated rounds / meaningful rework.
- **severe (50+)** — heavy churn; many rounds + rework + serious findings.

### Highest-friction PRs
One line each for the top items in `top_friction` (skip `clean`). **Hyperlink
the PR using its title**, and lead with a human label, not a bare number:
`[#<num> — <title>](<url>) (<author>): <score>, <dominant_category> — <the mechanical why>`
e.g. `[#591 — Charges Execute Implementation](https://github.com/example-org/service-a/pull/591) (carol): 100, correctness — 11 rounds, 24 rework commits, 5d to merge`.
Each `top_friction` entry carries `title` and `url`; use them. Keep to the ones
that actually carried friction. Anywhere else a PR is named, link it the same way.

### Per-developer actionables (coaching, NOT ranking)
For each dev in `per_dev` with signal, 1-2 lines. Lead with what's going well,
then the actionable. Ground it in their dominant `categories`:
- recurring `correctness`/edge-case → "focus on edge cases + add tests for them"
- recurring `security` → "add a security self-check before raising"
- recurring `test-gap` → "land tests with the change, not after"
- mostly clean / nits → "low friction — no action"
Never produce a leaderboard or numeric rating. Phrase as growth, not judgement.

### Human vs agentic coverage gap
Open with one plain sentence explaining the table: *for each comment category,
it counts how many PRs were flagged by humans, by MatterAI, or both — showing
where the bot already has us covered vs where humans are still doing all the
work.* Then, from `coverage_gap`, per category, three buckets:
- **bot-covered** (both flagged) → agentic review already handles this →
  candidate to reduce human review here.
- **human-only** (the gap) → still needs a human; this is what can't be
  automated yet (expect `business-logic`, `design` here).
- **bot-only** → MatterAI flagged, humans didn't → tag value-add vs noise.

### Bridging suggestions
From the gap profile, concrete moves:
- mechanical human-only gaps (`test-gap`, `naming`) → propose agentic-reviewer
  rule additions to close them.
- judgement-heavy human-only gaps (`business-logic`, `design`) → keep human,
  fix upstream (PRD clarity, earlier design review).
- high `bot-only` noise → tune the reviewer down there to protect trust.

### Coverage caveat
If `classification_coverage.prs_with_any_classification` is low relative to
`merged_prs`, state plainly that category/gap signals are partial and improve as
`/pr-quality` classifies more comments. Mechanical scores (rework, rounds,
time-to-merge) are complete regardless.

## Phase 3 — Persist the report (always)

Write the FULL rendered report (every Phase-2 section, verbatim — same markdown
you printed to chat) to a file under `management/pr-quality/`. Use the Write
tool, never Bash/echo. This runs on every invocation, not just on request.

- Directory: `$HOME/context/work-context/management/pr-quality/`
  (create it if missing).
- Filename: `pr-report-<YYYY-MM-DD>-<scope>-<window>.md` where
  - `<YYYY-MM-DD>` = today's date,
  - `<scope>` = `team` or `all-authors` (from the bundle's `scope`),
  - `<window>` = `all` when no date filter, else `since-<ISO-date>`.
  - e.g. `pr-report-2026-06-09-team-all.md`. Re-running the same window the same
    day overwrites that file (idempotent).
- Top of file: a one-line front-matter header — `# PR-Quality Friction Report`
  followed by a line `_scope: <scope> · window: <window> · generated: <YYYY-MM-DD>_`,
  then the report body.

`management/` is private/untracked work output — never commit it. After writing,
print the file path as the last line of your chat reply.

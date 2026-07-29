# Meeting-notes format — universal rules + per-category sections

## Universal format (EVERY note — DISTILLED + scannable; the reader gets the whole meeting in ~15 seconds and can stop)

- H1 = clean display title (no dates/slugs), then the `<!-- category: x -->` tag.
- **Lead with the CORE, in this exact order. Each layer has a DISTINCT job —
  NEVER restate the same fact across them (that triple-echo is the #1 thing that
  makes a note a chore):**
  1. **TL;DR** — 1-2 lines. The SITUATION + the outcome, in prose. NOT a list of
     the decisions (those are next) — don't pre-state them here.
  2. **Decisions** — the choices made, one line each; stated ONCE (not echoed in
     the TL;DR above or the actions below). Omit the section if none.
  3. **Your action items** — the OWNER's follow-ups: the concrete next step +
     `(due)`, one line each; others' under a sub-line. An action is the DOING,
     not a decision restated — if the only "action" is "do <the decision>", the
     decision already covers it; drop it. Omit if none.
  - Anti-pattern (a real miss): TL;DR "owner takes transaction & savings" +
    Decision "owner will take transaction & savings" + Action "take transaction
    & savings" = the SAME fact 3×. Right: TL;DR frames the situation; the
    Decision records the ownership once; an Action appears only for a genuinely
    separate next step.
- **NOTHING above the TL;DR.** The very first content line after the H1 is
  `**TL;DR**`. Any caveat (low-fidelity transcript, unattributed speaker, partial
  audio) is a SINGLE short italic line at the very END — never a preamble. The
  reader must hit the outcome first, not disclaimers.
- THEN `## Details` — the supporting discussion, category-specific sections
  below, SKIPPABLE. Depth lives here so the top stays scannable.
- **SAY EACH THING ONCE — no duplication.** The TL;DR is the only summary.
  Decisions / actions state specifics NOT already in the TL;DR. `## Details`
  adds ONLY what the core didn't say — a number, a nuance, who-argued-what. If a
  Details bullet restates the TL;DR or a Decision, DELETE it. A fact appearing
  twice at different zoom levels is the #1 thing that makes a note feel like a
  chore to read.
- **DISTILL, don't transcribe.** Capture the POINT, not the back-and-forth. Cut
  filler, pleasantries, tangents, restated context. If a line doesn't change a
  decision, an action, or the reader's understanding — drop it. Fewer, sharper
  bullets beat completeness.
- Keep exact: numbers, dates, ticket keys (linkified), owners. Quotes only when
  the wording itself matters (with `[mm:ss]`) — not as a default.
- One idea per line. Short sentences. Bold labels. Blank line between groups.
- The CORE stays tight even for a 2-hour meeting; let `## Details` carry length.
  If the whole note is longer than it needs to be to act on, it's too long.

## `## Details` sections by category

(TL;DR + Decisions + Your action items are the universal CORE above. The sets
below are what goes UNDER `## Details` — pick the set for the category, keep each
bullet distilled, drop any that's empty. Don't re-list Decisions/Actions here.)

**default / other**
Discussion (topic-grouped) · Open questions

**1-1** (managerial; private — candid is fine here)
How they're doing · Topics they raised · Feedback exchanged ·
Growth / career notes
(agreed follow-ups → core action items)

**prd-handover**
What's being handed over · Key flows & components walked through ·
Gotchas & tribal knowledge (the stuff not written anywhere — highest value) ·
Open items & risks · Docs / links referenced

**design-review**
Context (what's being decided) · Options discussed (+who argued what) ·
Rationale behind the decision · Risks / concerns raised
(the decision itself → core Decisions, one line)

**incident-review**
Timeline · Root cause · Impact · Prevention items
(remediation actions & owners → core action items)

**planning**
Scope agreed · Explicitly deferred · Capacity / constraints

**interview**
Signals observed · Strengths · Concerns
(core TL;DR carries the verdict lean + level)

(standup has its own template file — feeds the signals pipeline.)

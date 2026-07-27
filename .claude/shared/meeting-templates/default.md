# Meeting-notes format — universal rules + per-category sections

## Universal format (EVERY note — DISTILLED + scannable; the reader gets the whole meeting in ~15 seconds and can stop)

- H1 = clean display title (no dates/slugs), then the `<!-- category: x -->` tag.
- **Lead with the CORE, in this exact order. A reader who stops after it still
  has everything they must know or do:**
  1. **TL;DR** — 1-2 lines MAX. The outcome. Nothing else.
  2. **Decisions** — one line each. Omit the whole section if none.
  3. **Your action items** — the OWNER's to-dos first: `action (due)`, one line
     each. Then others' actions under a sub-line. Omit if none.
- THEN `## Details` — the supporting discussion, category-specific sections
  below, SKIPPABLE. Depth lives here so the top stays scannable. Never repeat a
  core point down here.
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

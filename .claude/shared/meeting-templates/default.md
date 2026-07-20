# Meeting-notes format — universal rules + per-category sections

## Universal format (EVERY note — ADHD-readable by spec, like ticketize bodies)

- H1 = clean display title (no dates/slugs), then the `<!-- category: x -->` tag.
- **TL;DR first**: ≤3 lines, the whole meeting's outcome. A reader who stops
  here should still know what happened.
- One idea per line. Short sentences. Blank line between groups.
- Bold inline labels (**Decision:**, **Owner:**) over deep heading nesting.
- LOSSLESS on substance: every ticket linkified, numbers exact, quotes
  verbatim with `[mm:ss]`. Structure the content — never omit to shorten.
- Omit any empty section entirely. Target ≤60 lines unless content demands.

## Sections by category

**default / other**
TL;DR · Discussion (topic-grouped) · Decisions (+quote offsets) ·
Action items (`owner — action (due)`) · Open questions

**1-1** (managerial; private — candid is fine here)
TL;DR · How they're doing · Topics they raised · Feedback exchanged ·
Growth / career notes · Agreed follow-ups

**prd-handover**
TL;DR · What's being handed over · Key flows & components walked through ·
Gotchas & tribal knowledge (the stuff not written anywhere — highest value) ·
Open items & risks · Docs / links referenced · Actions

**design-review**
TL;DR · Context (what's being decided) · Options discussed (+who argued what) ·
Decision & rationale · Risks / concerns raised · Actions

**incident-review**
TL;DR · Timeline · Root cause · Impact · Actions & owners · Prevention items

**planning**
TL;DR · Scope agreed · Explicitly deferred · Capacity / constraints · Actions

**interview**
TL;DR (lean + level) · Signals observed · Strengths · Concerns · Verdict lean

(standup has its own template file — feeds the signals pipeline.)

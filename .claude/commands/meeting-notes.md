---
description: >
  Process meeting recordings into structured notes. Sweeps the transcripts
  inbox (local whisper transcription + events.db ingest), then synthesizes
  template-driven notes from the transcript + optional scratchpad. Local-only:
  audio never leaves the machine; synthesis happens in this session (no API).
argument-hint: "[date-or-slug filter, e.g. 2026-07-16 or standup]"
---

# /meeting-notes — transcript → structured meeting notes

Part of the meeting-intelligence pipeline (prd/meeting-intelligence.md, P1).
Read-only on all sources except: events.db ingest (via the sweep script) and
the rendered note file. Nothing is posted anywhere.

## STEP 0 — Sweep the inbox (idempotent)

    bash __REPO__/bin/transcripts_process.sh

- `processed=N` lines tell you which meetings are newly ingested.
- Failures stay in the inbox — report them plainly, continue with what worked.
- (`__REPO__` = the repo root this file lives in.)

## STEP 1 — Pick the meeting(s)

Target = newly processed meetings from STEP 0, plus any meeting matching
`$ARGUMENTS` (date `YYYY-MM-DD` or slug substring). List candidates from:

    sqlite3 work-context/index/events.db \
      "SELECT subject, title, ts FROM events
       WHERE source='meeting' AND event_type='meeting_recorded'
       ORDER BY ts DESC LIMIT 15"

Skip meetings whose note file already exists (management/meetings/<date>-<slug>.md)
unless the owner explicitly asked to redo one.

ALSO enumerate explicit work markers — these are age-independent, and the
`LIMIT 15` query above only sees recent recordings, so an OLD meeting's request
would otherwise be missed (the bug that stranded regens/MoMs older than the last
~15 recordings):

    ls management/meetings/*.regen.request management/meetings/*.mom.request 2>/dev/null

Each marker's `<date>-<slug>` is a pending target no matter how old the meeting is.

REGENERATION: a `<date>-<slug>.regen.request` marker (or, legacy, a missing note
WITH a `<date>-<slug>.md.prev` sibling) means the owner hit ↻ in the Steno UI —
regenerate that note fresh from transcript + CURRENT scratchpad + links + `.cat`
(don't copy the .prev; it's only the owner's rollback), then DELETE the
`.regen.request` marker and report it in the run output.

## STEP 2 — Load the material (per meeting)

1. Full transcript: the archived `.txt` next to the audio in
   `work-context/transcripts/archive/YYYY-MM/<date>-<slug>.txt` (fallback: the
   `transcript_segment` bodies for the subject, ordered by ts).
2. Scratchpad (owner's during-meeting bullets), if present:
   `<date>-<slug>.notes.md` in the same archive dir. The scratchpad DRIVES the
   notes — every scratchpad bullet must be expanded with what the transcript
   actually said about it (quotes, numbers, decisions). Transcript-only content
   still gets captured, after the scratchpad-driven sections.
3. Context for attribution (standup template only): current board state /
   recent per-person activity from events.db to help match speech to people.
4. Attached context links, if present: `<date>-<slug>.links` in the same
   archive dir (one URL / Jira key per line, attached via the Steno UI).
   RESOLVE each one's actual content with the right tool — and issue ALL the
   resolutions as ONE parallel tool block (they're independent reads; never one
   link per turn):
   - Slack permalink → slack_read_thread
   - Confluence URL → getConfluencePage
   - Jira key/URL → getJiraIssue
   - anything else → WebFetch (skip on failure, note it)
   Use the resolved content as grounding: it disambiguates garbled terms,
   fills in ticket/doc specifics the audio only alluded to, and anchors
   decisions to their written source. Bare URLs pasted into the scratchpad
   get the same treatment.

## STEP 3 — Classify the meeting + select the template

Classify from title + transcript content into ONE category:
`standup | 1-1 | prd-handover | design-review | incident-review |
planning | interview | vendor | townhall | other`

Category definitions (owner-calibrated 2026-07-17 — follow these, not intuition):
- `1-1` = the MANAGERIAL conversation only: EM↔dev discussing the person —
  performance, feedback, growth, wellbeing, priorities, reviews. Two people
  talking about a technical topic is NOT a 1-1 — classify by the content
  (a 2-person debugging huddle is `other` or whatever the content says).
- `prd-handover` = ownership/knowledge transfer of a feature, module, or PRD:
  walkthroughs, "you're taking this over", handover of a flow/system to a
  person or team. (e.g. "Positive Pay Handover" → `prd-handover`.)
- `design-review` = discussing/critiquing an approach, architecture, migration
  plan, audit findings.
- When torn between two, pick by the meeting's PURPOSE, not its attendee count.

- Write it into the note as an invisible tag on the line AFTER the H1:
      <!-- category: handover -->
  The Steno UI reads this for its category filter chips — exact format matters.
- OWNER OVERRIDE: if `management/meetings/<stem>.cat` exists, its value IS the
  category — use it verbatim (comment + template choice), never re-classify.
- OWNER PARTICIPANTS: if `transcripts/archive/*/<stem>.people` exists, those
  ARE the attendees (ground truth, beats inference) — use for the Participants
  line, huddle titles ("Huddle with Alex"), and as the candidate set when
  attributing `Them:` speech. Still never attribute a specific quote to a
  specific person without transcript support.
- Template: `standup` category → standup template; everything else →
  `default.md`, which defines UNIVERSAL format rules (DISTILLED + scannable:
  a tight core — TL;DR + Decisions + your action items — then skippable
  `## Details`; distill don't transcribe) + a section set
  PER CATEGORY — use the section set matching the classified category.
- Owner can name a template explicitly in the invocation.

## STEP 4 — Synthesize (in-session; hard rules)

- ATTRIBUTION HONESTY: name a speaker only when the transcript supports it;
  otherwise `(unattributed)`. Never invent names, reporters, or owners
  (same rule as ticketize evidence-author-must-match).
- SPEAKER STREAMS: dual-stream (CALL) transcripts prefix lines `Me:` (the
  owner's mic) and `Them:` (everyone else) — ground truth, trust it over
  inference. `Me:` IS the owner. Within `Them:` infer individual names per the
  rules above. CAVEAT: if the owner was on speakers, the mic hears the remote
  side too — a `Me:` line that duplicates an adjacent `Them:` line is the echo,
  not the owner speaking; prefer the `Them:` copy.
- 1-1 DIRECTION (MANDATORY for every `1-1`; the #1 way a 1-1 note goes wrong):
  a managerial 1-1 always has a MANAGER and a REPORT. Determine which side the
  OWNER is on from the CONTENT — this is ALWAYS unambiguous, so there is no
  excuse for getting it backwards. The MANAGER is whoever sets the other's
  goals, gives competency / performance feedback, or assesses the other; the
  REPORT is whoever receives goals/feedback and raises things upward. NEVER
  assume the recording owner is the manager — the owner's OWN manager frequently
  runs the owner's review, in which case the OWNER is the report (their goals,
  their feedback, their competency being set — not the other way round). Before
  writing, state the direction to yourself ("owner is the REPORT; <other> set
  the owner's goals") and frame EVERY goal, action item, and feedback line that
  way. Getting the direction backwards inverts the whole note and is a hard fail.
  `Me:` telling you who held the mic does NOT tell you who is the manager —
  the mic-owner is just as often the one being reviewed.
- SPEAKER N (in-person, diarized): meetings captured on ONE room mic are
  diarized — lines are prefixed `Speaker 1:` / `Speaker 2:` / … These are
  DISTINCT VOICES, not names, and the numbering is arbitrary per meeting
  (Speaker 1 is just whoever spoke first). `Me:` never appears here — the owner
  is one of the Speaker N. Attribute a Speaker to a real person ONLY with
  transcript support: the `.people` participant set is the candidate pool, and
  direct address ("hey Alex") / self-introduction / work uniquely matching one
  person's events.db activity is the evidence. Never hard-map Speaker N → a name
  without support — leave `(unattributed)` (same honesty rule as above). Consistency
  helps: once Speaker 2 is confidently a person, treat all their lines as theirs.
- SPEAKER IDENTITY SIDECAR: if `transcripts/archive/*/<stem>.speakers.json` exists,
  any entry with a non-null `name` (or `handle`) is an OWNER-CONFIRMED identity —
  that `Speaker N` (the entry's `display`) IS that person, GROUND TRUTH (same
  status as Me:/Them:); use the name throughout and don't hedge it. Entries with
  only an `auto` voice-match and NO confirmed name are a hint, not truth — treat
  them like any other inference (needs transcript support). Confirmed names here
  beat both diarization numbering and your own inference.
- OFF-CALENDAR TITLES (`slack-huddle` / `teams-call` / `adhoc` slugs): infer
  the counterpart(s) from the transcript — direct address ("hey Alex"),
  self-introduction, or work uniquely matching one person's events.db
  activity — and title the note `Huddle with <Name>` (H1) plus a
  `Participants:` line. Confidence threshold applies: can't tell → keep the
  generic title; never guess a colleague's name into a title. The note FILE
  name keeps the original stem (pipeline key); only the display title improves.
- Quotes stay verbatim with their `[mm:ss]` offset.
- HINGLISH: meetings code-switch Hindi/English. Whisper silently TRANSLATES
  Hindi stretches into approximate English (observed: "Nothing has happened
  to me" ≈ "mujhe kuch nahi hua") — such lines are paraphrase, not verbatim.
  Use them for meaning, but never present a translated-sounding line as an
  exact quote, and never hang a commitment/decision on its precise wording.
- Whisper mishears names/terms: when a garbled token plausibly matches a known
  ticket key, service, or person from events.db context, note it as
  `garbled, likely X` — don't silently correct.
- Linkify every Jira key mentioned (config jira base URL). No bare keys.
- When links were attached, the note ends with a `## Linked context` section:
  each link with a one-line description of what it is and how it relates.
- These notes are PRIVATE (management/ is gitignored, never published). Do not
  redact; do capture candid content faithfully.

## STEP 4.5 — Dedup pass (MANDATORY, not a while-drafting instinct)

The #1 reason a note "feels like a task" is the same fact appearing in TL;DR AND
Decisions AND Actions AND Details. The no-repetition rule is easy to draft past,
so run this as an EXPLICIT pass before writing the file:

1. Re-read the CORE (TL;DR + Decisions + Actions). List every distinct fact it
   states.
2. Scan `## Details` line by line. Delete or merge any line that only restates
   one of those facts — a reworded version, or the same fact with one extra
   number, STILL counts as a repeat. Details earns its place only by adding a
   NEW nuance (who argued what, a constraint, a figure the core didn't carry).
3. Within the core itself: the TL;DR must not pre-list the Decisions; an Action
   that just says "do <the decision>" is already covered by the Decision — drop
   it. (Real miss to avoid: "owner takes X" in TL;DR + Decision + Action = 3×.)

A note that survives this pass says each thing exactly once.

## STEP 5 — Persist signals (EVERY meeting)

Run for **every** meeting, not just standups — the note already produces the
Action items / Asks / Untracked sections for all templates (STEP 6), and the
Steno "My Action Items" (To-do) view + the standup gather both read this store.
A meeting that skips STEP 5 is invisible to the To-do view. (Commitments +
said-vs-done are most common in standups but valid anywhere; actions/asks/
untracked apply to every meeting.)

Feed the durable signal state (Steno To-do view + said-vs-done / Your-queue /
ticketize pickups read this, the latter via `# STANDUP CALL` in the standup gather):

1. Compose `/tmp/meeting_signals_<slug>.json`:
       {"commitments": [{"person": "<canonical people.yaml handle or (unattributed)>",
                         "promise": "...", "due": "YYYY-MM-DD or omit",
                         "ticket": "KEY-123 or omit",
                         "subject": "meeting:<date>:<slug>", "offset": "[mm:ss]"}],
        "asks":        [{"person": "...", "ask": "...", "subject": "...", "offset": "..."}],
        "untracked":   [{"person": "...", "work": "...", "subject": "...", "offset": "..."}],
        "actions":     [{"assignee": "<canonical handle | owner | (unassigned)>",
                         "action": "...", "due": "YYYY-MM-DD or omit",
                         "subject": "...", "offset": "..."}],
        "suggestions": [{"suggestion": "...", "rationale": "<why, ≤1 line>",
                         "subject": "...", "offset": "..."}]}
   - `asks` = someone asked the OWNER to do/decide something.
   - `actions` = concrete action items AGREED in the meeting, each with an
     assignee: use the owner's canonical handle when it's on the owner, a
     teammate's canonical handle when it's on them, `(unassigned)` when the
     transcript names no clear owner. This is what feeds the standup Your-queue
     (owner-assigned) and route/delegate (teammate-assigned) buckets — the
     "I discussed X with someone and one of us must action it" case.
   - `suggestions` = 0-3 PROACTIVE owner to-dos you INFER that were NOT stated
     as explicit action items — the assistant layer ("you should also…"): an
     obvious prep/follow-through step, a risk to chase, a stakeholder to loop in,
     a decision the owner left dangling. Grounded in what was actually discussed
     (cite the reason in `rationale`); never invent commitments or facts. Omit
     the array if nothing genuine surfaces — do NOT pad.
   `actions`/`asks`/`untracked` are EXPLICIT items only — the same ones in the
   note's Action items / Asks / Untracked sections. person/assignee MUST be a
   canonical handle, `owner`, or `(unattributed)`/`(unassigned)` — never a guess.
2. `python3 work-context/derive/meetings/signals.py add /tmp/meeting_signals_<slug>.json`
   (idempotent — re-running a meeting dedups by content hash).
3. When the owner says a commitment/ask is handled:
   `python3 work-context/derive/meetings/signals.py resolve <id> [note]`.

## STEP 5.5 — MoM on request

A `management/meetings/<date>-<slug>.mom.request` marker means the owner hit
"MoM" in the Steno UI. Generate `<date>-<slug>.mom.md` — a SLACK-READY, shareable
recap the owner can paste straight into a channel or DM. Unlike the private note
it may go to attendees / leadership, so: professional tone, no candid asides, no
said-vs-done framing, no unattributed speculation.

FORMAT — Slack mrkdwn, NOT document markdown (this gets pasted into Slack, which
renders none of `#`/`##` headers, tables, or `[label](url)` links):
- Section labels are `*bold*` lines; bullets are `•`; owners emphasised with
  `*bold*`; links only as `<https://url|label>`.
- NO `#`/`##` headers, NO markdown tables, NO `[label](url)` links.
- It's a MESSAGE, not minutes-on-letterhead — compact, scannable in ~15 seconds.

    *<Title>*  ·  <date>
    _Attendees: <names supported by transcript/calendar; "+others" if unsure>_

    *Summary* — 1–2 lines: what the meeting was for and the outcome. Nothing else.

    *Decisions*
    • <the decision only, one line each>

    *Actions*
    • <action> — *<owner>* (<due, or "—">)

    *Discussion*
    • <ONLY context the Summary/Decisions/Actions don't already carry>

    *Open*
    • <unresolved items>

SAME quality bar as the note — a MoM is not exempt from the distillation rules:
- DISTILL, don't transcribe — capture the point, cut filler and back-and-forth.
- SAY EACH THING ONCE — the Summary frames; Decisions/Actions are the substance;
  Discussion adds ONLY what they didn't already say. If a bullet restates a
  decision/action/the summary, delete it. (Same triple-echo trap as the note.)
- NO INFORMATION LOSS on what matters — every decision, every action+owner, and
  every hard number/date/ticket/owner survives. Distil the wording, never drop a
  decision or a commitment.
- OMIT ANY EMPTY SECTION entirely (Decisions / Actions / Discussion / Open are all
  optional). A 4-line MoM for a 4-line meeting is correct — short beats complete-
  but-heavy.
- Cross-reference to another meeting → name it in prose (NO local-file links; the
  MoM leaves the machine). Only real shareable URLs, in `<url|label>` form.

Ground it in transcript + scratchpad + resolved links, same as the note.
Delete the `.mom.request` marker after writing. Report it in the run output.

## STEP 6 — Write + report

- Write the note: `management/meetings/<date>-<slug>.md` (create dir if needed).
- The H1 is a DISPLAY TITLE (the Steno sidebar shows it): a clean human
  meeting name — "Positive Pay Handover", "Huddle with Alex". NO dates, NO
  file stems/slugs, NO timestamps in the H1; the date lives in the filename.
- In chat, show: TL;DR + decisions + action items + (standup) commitments and
  owner-asks. One line per meeting processed. Do NOT paste the full note body
  into chat — it was just written to the file (linked); duplicating it doubles
  the run's output tokens for content one click away.
- If STEP 0 had failures or the transcript looks truncated/empty, say so
  plainly — never present partial notes as complete.

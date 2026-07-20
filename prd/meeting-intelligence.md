# PRD — Meeting Intelligence Pipeline (in-house "Granola")

**What this is:** a local-first meeting capture → transcript → notes → signals pipeline that
replicates every Granola feature on top of the existing context-engine stack (events.db,
embeddings, /ask, /standup, /ticketize, relay bot), then goes beyond it with signals only a
work-graph-aware system can produce.

**One-liner:** Granola hears your meetings; this hears your meetings *and* knows your Jira
board, Slack threads, PRs, leaves, and on-call rota — so the notes come with receipts.

## Problem

- Meeting content (standups, 1:1s, incident reviews, ad-hoc calls) is the largest work
  signal source NOT in events.db. Verbal commitments, blockers, and decisions vanish.
- Third-party recorders (Granola et al.) are cloud SaaS: audio of colleagues leaves the
  machine, data is hosted US-only, and org policy requires a security review + enterprise
  license before any AI tool touches internal data. Approval friction is high; the
  data-residency story may be unfixable.
- The platform mix (Teams meetings + Slack huddles + in-person) means no single built-in
  transcription covers everything: Teams transcription only covers Teams; huddles have no
  native transcript without a paid add-on.

## Product principles

1. **Local-first.** Audio and transcripts never leave the machine. Transcription is local
   Whisper. Synthesis is Claude in-session (chat-only policy — no direct LLM API calls
   from scripts; the OpenAI key stays embeddings-only).
2. **No new vendors.** Every component is either OSS running locally (whisper.cpp,
   ScreenCaptureKit), an already-approved tool (Claude, Slack MCP, calendar), or existing
   pipeline code. Nothing new for a security review except a consent heads-up.
3. **Privacy by construction.** `work-context/transcripts/` is gitignored; audio archives
   are prune candidates for housekeeping; any *shared* output passes the leak-scan +
   redaction rules before posting. Notes are private by default.
4. **Reuse before create.** Ingest uses the shared `delete_events` helper and cursor/state
   conventions; sharing uses the relay-bot pattern; chat-across-meetings is /ask over the
   existing embedding pipeline — no parallel search stack.
5. **Honest attribution.** Speaker attribution below confidence threshold is marked
   `(unattributed)`, never guessed — same rule as ticketize attribution hardening.

## Feature parity — every Granola feature, mapped

### F1. Bot-free capture (system audio + mic)
Granola records computer audio so no bot joins the call. Ours: a small Swift CLI
(`bin/meet-record`) on ScreenCaptureKit captures system audio + microphone into a single
m4a. No kernel driver; one-time Screen Recording permission. Works identically for Teams,
huddles, Meet, Zoom — anything that makes sound.
- `meet-record start [--label <slug>]` / `meet-record stop` (also a Raycast/menu-bar toggle).
- Output lands directly in `work-context/transcripts/inbox/`.
- Interim (phase 1): QuickTime manual recording into the same inbox; phone recordings via
  an iCloud-synced folder for in-person capture (Granola's mobile story).

### F2. Transcription
Local whisper.cpp (or mlx-whisper on Apple Silicon). Timestamped segments. Language
auto-detect (Granola supports multi-language; Whisper does too, including code-switched
Hindi/English common in our meetings). Phase 2+: whisperX for real diarization.

### F3. Human-in-the-loop notes (Granola's core mechanic)
During the call the owner jots rough bullets into a scratchpad
(`transcripts/inbox/<same-stem>.notes.md` — any text editor, or Obsidian). At processing
time the notes skill merges scratchpad + transcript: each rough bullet is expanded with
what was actually said (quotes, decisions, numbers), exactly Granola's "write 'pricing
concerns', get every pricing quote" behaviour. No scratchpad → full-auto notes (Granola
does the same).

### F4. Templates per meeting type
Meeting-type templates as skill sections (the `--label` slug or calendar-title match picks
one): `standup`, `1-1`, `incident-review`, `planning`, `interview`, `vendor-call`,
`default`. Each defines the note sections + which signal extractors run (e.g. standup runs
said-vs-done; 1:1 runs sentiment + growth topics; incident review runs action-item +
owner extraction). Templates are markdown — add/remove sections freely (Granola parity).

### F5. Action items + follow-up drafts
Every processed meeting emits: action items (owner, due-if-stated, source quote),
decisions, open questions. On request the skill drafts the follow-up message (Slack-ready,
standard markdown) — Granola's follow-up-email feature, retargeted to where we actually
follow up: Slack threads.

### F6. Calendar integration
Read the owner's calendar locally (macOS EventKit via a small helper, or `icalBuddy`) —
the work account is already on the Mac's Calendar app.
- **Auto-detect + remind:** a routine checks upcoming events; 1 minute before a meeting
  with 2+ attendees it fires a notification: "recording? `meet-record start`". (Granola's
  pre-call nudge.)
- **Auto-attach:** processed transcripts are matched to the calendar event by time overlap
  → the meeting note carries title, attendee list, recurrence id. Attendees are mapped to
  people.yaml identities where possible.
- **Auto-label:** recurring event title → template selection (the daily standup event maps
  to the `standup` template automatically).

### F7. Pre-meeting Briefs (Granola's best feature — ours is better)
`/meeting-brief [event]` (and an auto morning digest for the day's calendar): for each
meeting, who's attending, what we discussed last time (prior meeting notes for that
recurrence/attendee set), open action items from previous instances, PLUS what Granola
cannot do — each attendee's live work state from events.db: current tickets, recent PRs,
open threads with the owner, leave/on-call status. External attendees get the
last-interactions view instead.

### F8. Chat with meetings / across meeting history
Transcript events flow into the embedding + topic-brief pipeline like any other source →
/ask handles "what did we decide about X", "when did <person> last raise capacity",
scoped to `source:meeting` or blended with Slack/Jira context. Granola Chat parity, plus
cross-source answers it can't do.

### F9. Search, folders, organization
Meetings are subjects (`meeting:<date>:<slug>`), auto-foldered by template/recurrence.
events.db FTS + embeddings give search. "Folder rules" (Granola Business) = per-template
routing rules in config (e.g. every `standup` note auto-posts a digest block; `1-1` notes
never auto-post anywhere).

### F10. Sharing
Private by default. Explicit share = relay-bot posts the rendered note (or a section) to a
configured channel — after the leak/redaction pass. Per-template auto-post rules cover
Granola's folder→Slack automation. No public share links (out of scope — internal tool).

### F11. Integrations / MCP
Granola ships an MCP connector so Claude can read meetings. Ours *is* Claude-native:
everything queryable in-session by construction. Slack = relay bot; Jira = ticketize;
"Notion database" equivalent = events.db + rendered md. Zapier not needed.

### F12. Transparency / consent controls
Granola Enterprise offers attendee-notification controls. Ours: a standing one-time notice
to the team channel + per-meeting honesty (owner states recording for notes when asked).
The recorder writes a local session log (who/when/what file) so recording activity is
auditable. In-person capture follows the same rule: tell the room.

## Beyond Granola — features only this pipeline can have

### N1. Said-vs-done (flagship)
Standup transcript claims vs. reality in events.db: "will finish X today" with no ticket
movement in 48h → surfaces in the next standup digest Day-update as a gentle delta. Also
the inverse: work visibly done but never mentioned (silent heroes) → recognition signal.

### N2. Work-gap feed into /ticketize
Verbal work mentions with no Jira ticket are exactly the work-gaps ticketize hunts.
Transcript-sourced candidates enter the same maker-checker approve flow (evidence = the
transcript quote; reporter/assignee rules unchanged; nothing auto-created).

### N3. Owner-asks from meetings → Your queue
Asks directed at the owner in a call ("can you approve…", "need your call on…") join the
owner-asks net and land in the standup Your-queue message with a `(meeting)` source tag.

### N4. Commitment tracker
Every extracted commitment (speaker, promise, due) becomes a tracked item; delivered →
auto-closed by the said-vs-done matcher; overdue → resurfaces in the digest. Meetings stop
being where promises go to die.

### N5. Decision log
Decisions extracted across all meetings accumulate into a queryable log (subject-linked to
tickets/threads where the linker finds them). "When/why did we decide X" gets a cited
answer — /ask intent `decision_lookup`.

### N6. Per-person meeting signals → /pulse, /retro, /dev-style
Speaking share, blockers raised verbally vs. in writing, asks made/received, sentiment
trend per person — folded into the existing per-person skills as one more source (leave-
and role-aware, same guardrails: role-not-topic clustering rules apply).

### N7. Meeting hygiene analytics
Recurring-meeting scorecard: duration vs. calendar slot, decision/action-item yield per
hour, attendee count trend, "this meeting produced zero actions 4 weeks running" flags.
Dashboard lane candidate (local dashboard, /v5).

### N8. Identity-mapped speakers
Diarized/inferred speakers resolve through people.yaml (the same cross-source identity
map slack/jira/github use) — so meeting signals join each person's existing activity
graph instead of floating as "Speaker 2".

### N9. Redaction gate on anything shared
Shared notes pass the leak-scan denylist + an identity-redaction pass (external-facing
shares strip internal slugs/ids). Private notes keep everything.

### N10. Incident-review autopack
`incident-review` template cross-links the transcript to the incident's Slack thread,
opsgenie alert window, and RCA doc — the meeting note becomes the connective tissue the
RCA template asks for (detection time, decisions, action items with owners).

## Architecture

```
[meet-record CLI / QuickTime / phone→iCloud]        (capture)
        → work-context/transcripts/inbox/*.m4a (+ optional .notes.md scratchpad)
[watcher: launchd or routine step]                   (detect)
        → bin/transcribe.sh  (whisper.cpp, local)    (transcribe)
        → derive/meetings/ingest_transcript.py       (ingest: events.db source=meeting,
           subject meeting:<date>:<slug>, calendar match, people.yaml mapping,
           delete_events helper on re-ingest, PID-suffix tmp + busy_timeout per
           concurrent-writer rules)
        → audio + raw transcript → transcripts/archive/ (prune-policy candidates)
[/meeting-notes skill — in-session]                  (synthesize)
        → template render + scratchpad merge + signal extraction
        → notes to work-context/management/meetings/<date>-<slug>.md (gitignored tree)
        → signals: commitments/asks/decisions → state/*.json for standup/ticketize pickup
[existing pipeline]                                  (leverage)
        → embeddings refresh picks up meeting subjects → /ask /retro /pulse
        → standup gather reads # STANDUP CALL block + said-vs-done state
        → relay bot posts shared notes on explicit approve / template auto-rule
```

## Phasing

- **P1 — Transcribe + ingest + notes (build first):** inbox watcher, whisper.cpp setup,
  `ingest_transcript.py`, `/meeting-notes` skill with `standup` + `default` templates,
  manual QuickTime capture. Proves the loop end-to-end.
- **P2 — Recorder:** `meet-record` ScreenCaptureKit CLI (system audio + mic), Raycast
  toggle, phone→iCloud inbox for in-person.
- **P3 — Calendar + briefs:** EventKit reader, auto-attach, pre-call nudge,
  `/meeting-brief` + morning briefs digest.
- **P4 — Signals:** said-vs-done, commitment tracker, owner-asks feed, ticketize feed,
  standup-gather integration.
- **P5 — Sharing + analytics:** relay-bot share flow with redaction gate, decision log,
  meeting hygiene lane on the dashboard, whisperX diarization upgrade.

## Non-goals

- Live in-meeting notes UI (notes arrive minutes after, on processing).
- A GUI app of any kind; mobile app (phone capture = record + iCloud folder, that's it).
- Public share links / external collaboration.
- Perfect diarization in v1 — `(unattributed)` is an acceptable answer.
- Recording other people's meetings where the owner isn't a participant.

## Risks & mitigations

- **Consent:** recording colleagues silently is the real risk, not the tech. Mitigate:
  one-time team notice before first use; session log; local-only processing as the
  defensible posture. (Policy note: local OSS + approved tools ≠ new AI-tool onboarding,
  but flag the pattern to the security channel once, for the inventory.)
- **Diarization quality:** Whisper alone gives one stream. Mitigate: standup round-robin
  structure + name mentions + events.db cross-reference for attribution; confidence
  threshold; whisperX in P5.
- **TCC/sandbox friction:** recorder needs Screen Recording permission; calendar needs
  Calendar access; watcher runs as user launchd agent (established pattern). Sandboxed
  app containers are avoided entirely — capture writes straight to the inbox.
- **Disk growth:** audio archives are large. Housekeeping-review card + a retention rule
  (audio 30d, transcripts keep) from day one.
- **Double-processing / races:** inbox watcher uses the run-once marker + in-progress
  lock pattern; ingest follows the concurrent-writer rules (PID tmp, busy_timeout).
- **Scope creep:** P1 ships before any calendar/recorder work starts.

## Success metrics

- ≥80% of the owner's note-worthy meetings captured (self-reported, 4-week check).
- Standup digest cites ≥1 transcript-sourced signal (said-vs-done / verbal blocker /
  meeting ask) on ≥50% of days once P4 lands.
- ≥1 ticketize card per week sourced from meeting transcripts (P4).
- /ask answers meeting-history questions with citations (spot-check set).
- Zero transcript/audio bytes in any published commit (leak-scan green streak).

## Files (planned)

- `bin/meet-record` (Swift CLI, P2) + `bin/transcribe.sh` (whisper wrapper, P1)
- `derive/meetings/ingest_transcript.py` (P1), `derive/meetings/calendar_match.py` (P3)
- `.claude/commands/meeting-notes.md` (P1), `.claude/commands/meeting-brief.md` (P3)
- `.claude/shared/meeting-templates/*.md` (P1+)
- `work-context/transcripts/{inbox,archive}/` (created, gitignored)
- `state/meeting_commitments.json`, `state/meeting_asks.json` (P4)

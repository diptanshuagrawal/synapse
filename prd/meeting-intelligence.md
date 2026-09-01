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
Local whisper.cpp (or mlx-whisper on Apple Silicon). Timestamped segments.
- **Language (revised 2026-07-27):** explicit control, not auto-detect. Whisper picks ONE
  language and auto-detect is unreliable on code-switched Hindi/English (it decodes Hindi
  as confident-but-garbled English — measured on a real huddle). Default stays `auto`
  (fine for English meetings); Hinglish meetings set `TRANSCRIBE_LANG=hi` explicitly —
  the Steno UI's re-transcribe control passes it.
- **VAD (2026-08-05):** silero VAD is DISABLED by default — the current whisper-cpp
  1.9.1 / ggml-0.16.0 + silero-v5.1.2 combo detects 0 speech on all audio (total
  transcription outage; upstream regression, not fixed at root). `STENO_VAD=1` opts back
  in for re-testing.
- Diarization is a pyannote overlay, not a whisperX swap — see the P5 decision note.

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
  meeting hygiene lane on the dashboard, diarization upgrade.
  - **Diarization decision (2026-07-21):** implemented as a **pyannote-audio overlay**,
    NOT a whisperX swap. whisper.cpp keeps doing the transcription (all its tuning:
    silero VAD, vocab prompt, `-mc 0` loop recovery, silence gate); `diarize.py` runs
    pyannote `speaker-diarization-3.1` on the mic wav for who-spoke-when, and
    `merge_streams.py --single --diarize` maps each whisper segment onto the dominant
    turn → `Speaker 1/2/…`. Rationale: whisperX would replace the tuned ASR and its
    wav2vec2 alignment is English-only (bad for our Hinglish); pyannote is acoustic-only
    (language-agnostic) and reuses whisper.cpp untouched. Only fires when the system-audio
    ('them') stream is silent (= in-person, one room mic); CALLS keep ground-truth
    Me:/Them:. Isolated torch venv (`~/.steno-diarize`), gated models side-loaded past the
    Zscaler HF-CDN block (`bin/steno-diarize-setup.sh`), soft-dependency fallback. v1 =
    anonymous Speaker N; name resolution stays synthesis-time (`.people` + direct address).
  - **Update (2026-08-15):** shipped, and extended past the note above:
    - **Far side of CALLS is now diarized too** (2026-07-27): on the dual-stream call
      path the them-stream wav runs through the diarizer → `Them · Speaker N` when
      multiple remote people share one stream (e.g. a room on one Teams mic). Soft
      overlay — diarizer failure degrades to flat `Them:`. The "only fires when the
      them-stream is silent / calls keep ground-truth Me:/Them:" restriction no longer
      holds; Me:/Them: stream separation itself is unchanged.
    - **Over-split cluster merging**: acoustically-same diarization clusters are merged
      (one voice → one speaker) before labeling; clusters also match against a local
      voiceprint gallery (`speakers.json`).
    - **"v1 = anonymous Speaker N" is superseded**: the Steno UI supports custom speaker
      names and inline speaker tagging on the transcript, and a re-transcribe
      auto-regenerates the note.
    - The "silero VAD" item in the tuning list above is disabled by default since
      2026-08-05 (upstream regression — see F2); the rest of the tuning stands.

## Status (2026-08-15)

- **P1 live** since 2026-07-16: inbox → whisper.cpp → events.db `source=meeting` +
  `/meeting-notes` (ingest at `work-context/derive/meetings/ingest_transcript.py`).
- **P2 shipped** — and grew a UI: `meet-record` ScreenCaptureKit capture plus the Steno
  local web UI (`bin/meet_ui.py`) + thin native macOS wrapper. Library views, editable
  meeting titles / one-click rename (mislabel fix), distinct same-day huddles split into
  separate meetings (merge only true sub-5-min reconnect fragments), live capture-health
  warnings (mic-only + silent-mic), re-transcribe with explicit language control,
  per-stream me/them audio retained (~64k AAC) for re-runs, in-session transcript
  correction (STEP 2.5), inline speaker tagging, auto-regenerated note after a
  re-transcribe.
- **P3 shipped**: local EventKit calendar reader (`bin/steno-agenda`, 2026-08-05 —
  replaced the blocked published-ICS feed), calendar auto-match, `/meeting-brief`.
- **P4 not started**: said-vs-done, commitment tracker, owner-asks + ticketize feeds
  still planned (`state/meeting_commitments.json` / `meeting_asks.json` don't exist yet).
- **P5 partial**: diarization shipped and extended (see the decision-note update);
  `/meeting-share` redacted export shipped. Decision log + meeting-hygiene dashboard
  lane still open.
- **Latency**: notes land ~5 min after a meeting (was ~15) — priority-queue sweep
  (starred first, shortest first) + turbo tiering; power policy in
  `prd/meeting-transcription-power-policy.md`.
- The "no GUI app" non-goal below is superseded by the Steno UI; the non-goals text is
  kept as the original intent record.

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
  threshold; pyannote diarization overlay in P5 (single-mic in-person case).
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

# Meeting-notes template: standup

For daily-standup recordings. Extraction focus: per-person status, commitments,
blockers, asks. These notes feed the standup digest and ticketize pipelines.

Sections to render:

## TL;DR
2-3 sentences: overall team state, anything unusual (blockers, escalations).

## Per-person
One block per speaker segment. Attribute a segment to a person ONLY when the
transcript supports it (they are addressed by name, introduce themselves, or
describe work uniquely matching one person's current tickets in events.db).
Otherwise label the block `(unattributed)` — never guess.

Per person:
- **Did**: what they reported done (link tickets if named).
- **Will do**: commitments, verbatim-ish ("will finish X today" style claims
  matter later — keep the promise wording + `[mm:ss]`).
- **Blocked**: blockers raised, incl. who/what blocks them.

## Commitments (feeds said-vs-done)
Flat list: `person — commitment — stated due — [mm:ss]`. Only explicit
promises, not vague intentions.

## Blockers & escalations
Every blocker mentioned verbally, whether or not it exists in Slack/Jira yet.
Flag any that have NO matching Slack thread or Jira ticket — these are the
invisible ones the digest can't see.

## Asks directed at the owner
Anything a speaker asked the owner to do/decide/review — `person — ask — [mm:ss]`.

## Untracked work mentions (feeds ticketize)
Work described in the call that has no Jira ticket reference in the transcript
and no obvious match in current board state — candidates for /ticketize.

# Shared rule — roster & identity resolution

Loaded by any skill that scopes work to the owner's team or attributes events to people
(`/standup`, `/ticketize`, `/retro`, `/pulse`, `/dev-style`, `/leaves`, `/ask` person/team
intents, …). This is the single source of truth for WHO is on the roster and how an event
or a name resolves to a person. Skill-specific scoping (e.g. which channels, which window)
stays in the skill.

## 1. Roster = `config/people.yaml`, `scope: team`

The roster is the set of `config/people.yaml` entries with **`scope: team`** — NOT
`team.md` prose, NOT the `role` field (the org has many people with a role). That set =
the owner (manager) + their direct reports.

## 2. Identity set per member

Build each member's identity set from their `people.yaml` entry — match ANY of:
- `github` + `github_aliases` / `git_names`
- `jira_id` + email
- `slack_id` + `slack_handle`
- `canonical` (the canonical handle — the identity key used everywhere downstream)

## 3. Event attribution — actor OR assignee

**Keep an event only if its `actor` OR `assignee` matches a roster identity.** Non-roster
actors (anyone outside the `scope: team` set) are dropped — a non-roster name in the output
is a bug. Credit work by the matched person (see the credit rules in the skill /
`derive/jira_metrics.py`), never by who merely transitioned a ticket.

## 4. Manager / owner exclusion (team-scoped digests)

For **team-scoped** output (e.g. `/standup team`, `/ticketize`), EXCLUDE the manager (the
owner) — they're the audience, not a reportee. Team output renders only the reports
(`scope: team` minus the owner). The owner stays reachable via `me` / `<owner>` for their
own section. Owner identity = the single `scope: team` entry flagged as manager/owner
(`standup_gather.py` exposes it as `owner_handle()`).

## 5. Person-name resolution (single-person intents)

When a skill takes a person argument (`/pulse <person>`, `/dev-style <person>`, `/ask what
did <person> do`): match the argument **case-insensitive substring against `canonical`** in
`config/people.yaml` (also accept a raw slack U-id where the skill supports it). If multiple
distinct people match, list them and ask which — never guess. Unmapped aliases that collide
(same person, different `github_login`/id) are a `people.yaml` grooming signal — call it out.

## 6. accountId / write-time resolution

When a skill writes to Jira (assign, report, comment), resolve the Jira accountId from the
matching `canonical`'s `jira_id` in `people.yaml` **at write time**. If `jira_id` is missing,
create unassigned and flag the gap — never invent an accountId.

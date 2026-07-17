Team-structure changes as one-shot flows: onboard a dev, onboard a repo. Everything
that must change across configs/ingest/graph happens in ONE invocation, ends with a
verification pass + a short residual checklist of the things only the owner can do.

## Usage — `/team <subcommand> <arg>`

- `/team add-dev <name or email>` — onboard a new team member across every pipeline.
- `/team remove-dev <name or email>` — offboard a member who left the team/org.
- `/team add-repo <repo> [--graph-only]` — onboard a repo across GitHub ingest +
  code-graph + service-brief. `--graph-only` = graph/ingest but never brief it.

If invoked with `help`, `-h`, `--help`, or an unknown subcommand: print this Usage
block verbatim and STOP.

**Source-of-truth model (consolidated 2026-07-16):**
- Team MEMBERSHIP = `config/people.yaml` entries with `scope: team`. Nothing else.
  (`management/context/team.md` is the manager's 1:1-notes doc — a stub is appended
  there for notes, but it does not drive membership anywhere.)
- Repo surface = `config/sources.yaml` `github.repos` (ingest), `github.codegraph_repos`
  (graph + service-brief), `github.service_brief_exclude` (graph-only carve-out).

---

## `/team add-dev <name or email>`

### STEP 1 — resolve identities (never guess; confirm ambiguity with the owner)
1. **Slack**: `slack_search_users` with the name/email. If MORE THAN ONE plausible
   candidate, list them (name, title, email) and ASK the owner which one — validated
   miss risk: a common first name can match 5 people across departments. Read the
   chosen profile (`slack_read_user_profile`) for email + title.
2. **Jira**: `lookupJiraAccountId` (Atlassian MCP; cloudId = `atlassian.host` from
   `config/sources.yaml`) with the email. Expect exactly 1 hit.
3. **GitHub** (best-effort): try the org handle convention (`github.handle_prefixes`)
   via `gh api "search/users?q=<name>"` and look for a prefixed login. If nothing
   confident: SKIP — leave github unset and say so. The ingest-autofix routine parks
   their first PR's unmapped login for owner review, so nothing is lost.
4. **Role**: from the Slack title if present; otherwise ask the owner.

### STEP 2 — dedup BEFORE writing (validated 2026-07-16)
Grep `config/people.yaml` for the email, slack_id, AND jira_id. A new teammate often
already exists as a `scope: org` cross-team collaborator — sometimes MORE THAN ONCE.
Duplicate entries SHADOW the team entry in the roster loaders (the person resolves to
a raw slack-id instead of their canonical). If found: MERGE — upgrade one entry to the
full team shape below and DELETE the other duplicates. Never leave two entries with
the same email/slack_id/jira_id.

### STEP 3 — write the people.yaml entry
Append (or upgrade the merged entry) with:

```yaml
- email: <email>
  name: <Full Name>
  role: <SDE1|SDE2|SDE3|...>
  github: <login>              # only if confidently resolved
  jira_id: '<accountId>'       # QUOTE ids containing a colon
  slack_id: <U...>
  slack_handle: <handle>
  canonical: <kebab-full-name>
  scope: team
  git_names:                   # only if github resolved
  - <login>
```

### STEP 4 — secondary configs
- `config/teams.yaml`: add the github login to the home team's `contributors_github`
  (skip if unresolved; add later with the handle).
- `management/context/team.md`: append the standard notes stub (`## <Name> — <email>`
  + Role/Owns/Strengths/Recent work/Open threads/Last 1:1 lines) — notes only.

### STEP 5 — verify (all three must pass)
```bash
cd $HOME/context/work-context && .venv/bin/python - <<'PY'
import sys; sys.path.insert(0, '.')
import yaml
d = yaml.safe_load(open('config/people.yaml'))          # 1. yaml parses
from derive.slack_team import load_team_slack_ids
ids = load_team_slack_ids()                             # 2. slack filter sees them
assert '<U...>' in ids and ids['<U...>'] == '<canonical>', ids.get('<U...>')
print("slack filter OK:", ids['<U...>'])
PY
$HOME/context/bin/standup_gather.py <yesterday> team | head -3   # 3. roster line includes canonical
```

### STEP 6 — leave-plan catch-up (only when a current ask is live)
The `leave-plan-reminder` routine posts `Please update leave plan for <M1> and <M2>`
to the leave-plan channel (`slack.leave_plan_channel`) on the 1st of even months,
pinging the team user-group. A dev onboarded mid-cycle missed that ping. Search the
leave-plan channel for the most recent reminder post; if its two-month window is
still CURRENT, post a threaded reply under it tagging the new dev (`<@U...>`) asking
them to add their plan for the remainder of the window. If no live reminder exists,
skip — the next even-month fire covers them via the group ping (once they're in the
user-group).

### STEP 7 — residual checklist (print for the owner; these need admin/human action)
- Add them to the team Slack user-group(s) (`config/team_subteams.yaml` ids) — admin
  UI. This also makes future leave-plan reminders + group pings reach them.
- Invite them to the team channels (standup-updates + team-internal at minimum) —
  @-mentions only notify channel MEMBERS.
- Add them to the on-call rota when ready (forecast picks it up automatically).
- If github was unresolved: send the handle later → fill `github` + `git_names` +
  `contributors_github`.
- Coverage is FORWARD-ONLY: their pre-existing Confluence docs / PRs won't backfill
  until touched again.

---

## `/team remove-dev <name or email>`

Offboarding is a SCOPE FLIP, not a delete — events.db history references the
person's canonical/ids forever, so past attribution must keep resolving.

### STEP 1 — flip membership off, keep identity
In `config/people.yaml`, change their entry's `scope: team` → `scope: org` and add
an inline comment `# LEFT ORG <date>` (or `# MOVED TEAMS <date>`). Do NOT delete
the entry or any of its ids — deleting breaks historical attribution in standup
lookbacks, /retro, /ask person queries and PR-quality reports.

### STEP 2 — docs
- `management/context/team.md`: annotate their section header
  `_(LEFT ORG <date> — notes kept for reference)_`. Keep the notes.
- `config/teams.yaml` `contributors_github`: KEEP their login — it attributes
  their historical team PRs. (Remove only if they moved to a SIBLING team whose
  new work would now mis-attribute to this team.)

### STEP 3 — verify the roster dropped them
```bash
cd $HOME/context/work-context && .venv/bin/python - <<'PY'
import sys; sys.path.insert(0, '.')
from derive.slack_team import load_team_slack_ids
ids = load_team_slack_ids()
assert '<their U...>' not in ids, "still on the roster"
print("roster size:", len(ids))
PY
```
Standup / leaves / capacity / pulse all read the same scope — one flip covers all.

### STEP 4 — residual checklist (owner/admin actions; print them)
- Remove them from the team Slack user-group(s) — otherwise group pings keep
  "reaching" a dead account and the subteam-size signals drift.
- Remove/replace their future on-call rota slots (external scheduler) — the
  forecast reads the rota as-is and will keep showing them until it's edited.
  Flag any leave×on-call collision the swap creates.
- Reassign their open Jira tickets + in-review PRs (surface them: board query
  assignee=<them> status not Done; open PRs by their login).
- If they were a DRI on any cross-team track, name the replacement there.

---

## `/team add-repo <repo> [--graph-only]`

### STEP 1 — resolve the full `org/name`
If given a short name, find the org: check `events.db` bodies for `github.com/<org>/<name>`
references, or `gh api` the org candidates from `github.repos`. Repos may live in a
DIFFERENT org than `github.org` (validated: sibling-org repos are normal). Confirm
with the owner if ambiguous.

### STEP 2 — access check BEFORE config (hard gate)
```bash
TOKEN=$(cat ~/.secrets/github_pat)
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://api.github.com/repos/<org>/<name>/pulls?per_page=1&state=all" | head -c 200
```
A JSON array = OK. `Not Found` = the ingest PAT lacks access — STOP and tell the
owner; adding the repo to config would make every ingest fire fail.

### STEP 3 — GitHub ingest
Add `"<org>/<name>"` to `github.repos` in `config/sources.yaml`.
Validate with a dry run:
```bash
cd $HOME/context/work-context && GITHUB_TOKEN=$(cat ~/.secrets/github_pat) \
  .venv/bin/python ingest/github.py --dry-run --repo <org>/<name> | tail -3
```
NOTE: the ingest cursor is GLOBAL — coverage starts from the current cursor, no
history backfill. Do NOT run a repo-only real ingest (it advances the shared cursor
and blinds the other repos for the gap). Mention this to the owner.

### STEP 4 — code-graph mirror
```bash
git clone git@github.com:<org>/<name>.git ~/.code-review-graph/repos/<name>
```
Append to `~/.code-review-graph/registry.json`: `{"path": "<abs mirror path>", "alias": "<name>"}`.
Add `"<name>"` to `github.codegraph_repos` in `config/sources.yaml` — the daily graph
rebuild and the service-brief loop both read this list.

### STEP 5 — service-brief
- `--graph-only`: add `"<name>"` to `github.service_brief_exclude` and stop here.
- Otherwise (Go services only): run
  `bash derive/service_derive/refresh-skeletons.sh` — the new service must appear in
  the `CHANGED:` line (a brand-new skeleton always flags). Then follow
  `.claude/commands/service-brief.md` STEPS 3–6 to write + ingest the BASELINE brief
  now — the daily routine only briefs services whose skeleton CHANGED in its own run,
  so a new service that already has a fresh skeleton would otherwise never get its
  first brief.

### STEP 6 — verify + residual
- `refresh-skeletons` log line lists the service (or it's in the exclude list).
- Dry-run ingest returned cleanly (Step 3).
- Registry + mirror exist.
- Residual for the owner: if the repo introduces a new work domain, add keywords to
  `config/projects.yaml` so rollup classification maps its activity; embeddings pick
  up the brief on the next weekly refresh.

Run full manual rollup: keyword pass → classify pending → apply verdicts.

## Usage — `/rollup [days]`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run any phase.

**What it does:** Runs the full manual activity rollup over a lookback window — keyword pass, classifying pending subjects into project slugs + team ownership, then applying verdicts and rebuilding the derived rollups.

**Phases:**
- Phase 1 — keyword pass + dump pending subjects.
- Phase 1.5 — auto-create slugs for unmapped Jira epics (delegates to `/slug-epics`), only if dump flags them.
- Phase 2 — chat-classify each subject: domains + team ownership.
- Phase 3 — apply verdicts → deterministic ownership corrections → per-cluster ownership rollup → derived rebuild.

**Usage:** `/rollup [days]`
- `days` (optional) — lookback window in days. Default 240.

Examples: `/rollup` (240d) · `/rollup 30` (last 30 days).

Optional arg: number of days to roll up. User input: `$ARGUMENTS`.
If empty, use 240. Otherwise use the user-provided value.

## Phase 1 — Keyword pass + dump

Substitute DAYS_VAL with 240 if the arg is empty, else the arg value:

```bash
cd $HOME/context/work-context && DAYS=DAYS_VAL derive/manual-rollup.sh dump
```

If output contains "✓ no pending classifications", rollup is complete. Stop here.

## Phase 1.5 — Epic-slug creation (only if triggered)

If `dump` output mentions "epic(s) need slug creation" instead of pending subjects, run `/slug-epics` to synthesise human-readable slugs for unmapped Jira epics. The skill:

1. Reads `state/pending_slug_creation.json` (epic + child context).
2. Writes `state/verdicts.epic_slugs.json`.
3. Applies via `derive/manual-rollup.sh apply-slugs`.

After `/slug-epics` finishes, re-run Phase 1 (`derive/manual-rollup.sh dump`) and proceed.

## Phase 2 — Classify

Follows the shared dump→classify→apply harness
(`.claude/shared/classify-apply-harness.md`): maker-checker, rules-first, schema-strictness,
confidence gate 0.7, verdict-file format. Rollup adds the team-ownership fields below.

Read the classification rules FIRST, before subjects:
`$HOME/context/work-context/state/pending_classification.json.rules.md`

Read all pending subjects:
`$HOME/context/work-context/state/pending_classification.json`

For each subject, triage signal strength first:

- Rich signal (decent title+body OR matterai_summary OR epic_body OR confluence_body): classify directly.
- Thin GitHub subject (empty body AND no matterai_summary, source=github): FETCH the PR diff using `gh`:
  ```bash
  gh pr diff <pr-num> --repo <owner/repo>
  ```
  Subject format `<owner/repo>#<num>` (e.g. `gh pr diff 587 --repo example-org/service-a`). Read enough of the diff to identify touched modules/paths, then classify.
- Sync/conflict-resolve PRs (title literally "sync" / "merge main" / similar): do NOT fetch diff. Use `domains: [], confidence: 0.80` per rules.md.

After triage, produce one verdict per subject:
```json
{
  "subject": "<echo unchanged>",
  "content_hash": "<echo unchanged>",
  "domains": ["slug1"],
  "summary": "Action-first present tense ≤200 chars",
  "risk_flags": [],
  "confidence": 0.85,
  "owned_by_primary": "home-team",
  "co_owners": ["deposits-team"],
  "owned_by_confidence": 0.85,
  "ownership_reasoning": "≤300 chars — why this team owns it"
}
```

Hard constraints (from rules.md):
- `domains`: only slugs from the project slug enum in rules.md. Empty OK.
- `summary`: ≤ 200 chars, action-first, present tense. No filler.
- `risk_flags`: only `{security, data-loss, panic, race, migration, breaking-api}`. Empty when none.
- `confidence`: calibrated 0–1. After diff fetch, expect ≥ 0.70 for almost all. Below 0.7 → stays pending next run (use only if diff itself is empty/unreadable).
- `epic_domain` non-empty → that slug MUST be in `domains`.
- DO NOT set `needs_diff: true` — flag is dead. Fetch the diff inline instead.

Ownership constraints (from rules.md — see team-id enum + author→team table there):
- `owned_by_primary`: exactly ONE team id from the teams.yaml enum. The team that OWNS the work (does the fix / drives the workstream), NOT necessarily the channel/repo/reporter. Use `external` for org-wide noise, `unknown` when no signal.
- `co_owners`: zero+ team ids that collaborate. Drop unknown ids.
- `owned_by_confidence`: 0–1. Below 0.6 → `apply_verdicts` nulls the ownership fields (domain classification still kept).
- `ownership_reasoning`: ≤300 chars, cites the signal (author identity, subteam ping, epic owner, thread context).
- Deterministic overrides (author-on-team, channel-join→external, co-owner-team author attribution, etc.) re-apply automatically in apply via `ownership_corrections.py` — don't hand-tune those; focus ownership judgement on the ambiguous cross-team cases.

Write the complete JSON array to:
`$HOME/context/work-context/state/verdicts.json`

Print: total classified / stored / deferred (low-conf).

## Phase 3 — Apply + final rollup

```bash
cd $HOME/context/work-context && derive/manual-rollup.sh apply
```

`apply` runs, in order: `apply_verdicts.py` (verdicts → subject_summary incl.
ownership cols) → `ownership_corrections.py` (deterministic ownership post-pass,
idempotent) → `cluster_ownership_rollup.py` (per-cluster `owner_distribution_json`)
→ `run_rollup` (derived/ rebuild). No manual ownership steps needed.

Print the final output from that command.

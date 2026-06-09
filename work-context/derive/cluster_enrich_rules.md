# Cluster enrichment rules (maintained source)

The chat reads a per-dump copy of this file at
`state/pending_cluster_enrichments.json.rules.md` (written by
`derive/enrich_clusters.py dump`). Edit THIS file to change rules;
the dump command picks it up on next run.

## Goal

For each labeled cluster, extract the operational metadata that turns
the cluster from a "table of contents entry" into a queryable fact:

- **status**         — where the work stands now
- **decisions**      — choices the team has made, with evidence
- **blockers**       — open issues stopping forward progress, with evidence
- **root_cause**     — for incident-flavored clusters only: one-line cause
- **participant_roles** — per actor: role in this cluster (AUTHOR/RESPONDER/RESOLVER/REVIEWER/DECIDER)

`first_ts`, `last_activity_ts`, and per-actor reply counts are computed by
the script from `events` — do not extract them.

## Rules per field

### status (REQUIRED, enum: ACTIVE | RESOLVED | STALE | RECURRING)

- **RECURRING**: cluster label starts with `Recurring ` — bot/ceremony/template
  pattern. Status is fixed regardless of timeline.
- **RESOLVED**: every member shows a resolution marker —
  - Jira tickets closed/marked Done
  - Slack threads explicitly closed ("fixed", "resolved", "deployed", "merged")
  - PRs merged
  - Confluence pages with "[Outdated]" / "[ARCHIVED]" in title
- **STALE**: `last_activity_ts` more than 30 days before today AND not RESOLVED
  AND not RECURRING. Use the computed timestamp the dump includes.
- **ACTIVE**: everything else — work still in motion or recently touched.

### decisions (array of {text, evidence_subject})

Capture explicit choices the team made. Look for:
- "we decided to ...", "approach: ...", "going with ...", "will do ..."
- Confluence pages titled `*TRD*` / `*Design*` represent decisions; cite the page
- Jira tickets with explicit resolution approach in description
- Slack threads where someone confirms a path

Each entry:
- `text`: paraphrased decision in ≤ 25 words, present tense.
- `evidence_subject`: the subject id where the decision appears.

If no decisions: empty array `[]`. Do NOT fabricate.

### blockers (array of {text, evidence_subject})

Capture open issues blocking forward progress. Look for:
- "blocked on ...", "waiting for ...", "we need ...", "pending ..."
- Jira tickets in BLOCKED or WAITING status
- Slack threads with explicit unresolved asks

Each entry:
- `text`: paraphrased blocker in ≤ 25 words, present tense.
- `evidence_subject`: the subject id.

If no blockers: empty array `[]`. Closed/resolved blockers do NOT count.

### root_cause (string OR null)

ONLY for clusters whose work is incident triage, performance debugging,
production alerts, or post-incident fixes. Otherwise null.

Format: one-line causal phrase, ≤ 30 words. Not a description of the
incident — the **cause** of the incidents.

Examples:
- "DB lock contention on account_balance during defrag window"
- "Grafana threshold tuned too tight on payment fraud decline rate"
- "VendorX inward transfer settlement-account mismatch with service-c status"
- "DC→DR image-pullback errors from stale registry credentials"

If cluster is not incident-flavored, OR cause is genuinely unknown, return `null`.

### participant_roles (object: {person_canonical: role})

For each person who appears in the cluster's events, assign one role:
- **AUTHOR**     — initiated the work (created the ticket, raised the thread, opened the PR)
- **RESPONDER**  — replied early but didn't drive resolution
- **RESOLVER**   — drove the fix / posted the resolution / merged the PR
- **REVIEWER**   — reviewed/approved without authoring or resolving
- **DECIDER**    — made or confirmed the key decision in the cluster

A person may have only one role per cluster — pick the one that best
describes their highest-leverage contribution. Use the canonical name
from `participants_observed` in the dump (already resolved by script).

Skip bots (actor_id starting with `B`).

If the cluster has no human participants worth naming (recurring bot
templates), return `{}`.

## Output shape

One entry per cluster. Wrap all in a single JSON array.

```json
{
  "cluster_id": <int>,
  "status": "ACTIVE | RESOLVED | STALE | RECURRING",
  "decisions": [{"text": "...", "evidence_subject": "..."}, ...],
  "blockers":  [{"text": "...", "evidence_subject": "..."}, ...],
  "root_cause": "..." | null,
  "participant_roles": {"person_canonical": "ROLE", ...}
}
```

Save to: `state/verdicts.cluster_enrichments.json`

Then run: `.venv/bin/python derive/enrich_clusters.py apply`

## Notes

- Apply is idempotent (INSERT OR REPLACE on cluster_id). Partial batches OK.
- The script does NOT mutate cluster_id, label, summary — those come from Phase B.
- If you change your mind on a status/decision after running apply, edit the
  verdicts file and re-run apply.

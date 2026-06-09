# Cluster finalize rules — combined label + enrichment

The chat reads a per-dump copy at `state/pending_cluster_finalize.json.rules.md`
(written by `derive/finalize_refresh.py dump`). Edit THIS file to change
the rules; the next dump picks up the change.

## Goal

For each cluster that needs new chat work (new + relabel from `cluster_diff`),
produce ONE combined verdict that covers BOTH naming and operational metadata.
This replaces the older two-phase label + enrich loop.

Each cluster's verdict has 11 fields. Required fields are starred (*).

## Field reference (apply in order while reading members)

### label* (≤ 15 words, single string)

The shared **work** across the cluster's members. Not topic. Not keyword.
Verb-ish noun phrase.

- Bridge clusters (≥ 2 sources): `<work A> + <work B>: <src-A> + <src-B>`
- Template/recurring clusters: lead with `Recurring `
- Single-source workstreams: name the workstream (`payments-main subscription rollout`)
- Composite/mixed: name the bucket, elaborate in `what_work`

### what_work* (≤ 25 words, single sentence)

One-line elaboration. Becomes `topic_brief.summary`.

### confidence* (0.0 – 1.0)

| 0.90+      | All members fit cleanly — tight workstream OR clean template |
| 0.75–0.89  | One looser member but core theme clear |
| 0.50–0.74  | Reasonable bucket but ≥ 2 forced members |
| < 0.50     | No coherent frame — flag for re-clustering |

### status* (enum: ACTIVE | RESOLVED | STALE | RECURRING)

- **RECURRING**: label starts with `Recurring ` — bot/ceremony/template pattern.
- **RESOLVED**: every member shows a resolution marker (jira closed, PR merged,
  slack thread explicitly closed, "[Outdated]" / "[ARCHIVED]" page title).
- **STALE**: `last_activity_ts` > 30 days before today AND not RESOLVED AND
  not RECURRING. Use computed timestamp in the dump.
- **ACTIVE**: everything else.

### decisions (array of {text, evidence_subject})

Explicit choices the team made. Look for "we decided to ...", "approach: ...",
"going with ...", "will do ...". TRD / Design pages count as decisions —
cite the page subject id.

`text`: paraphrased decision ≤ 25 words, present tense.
Empty `[]` when none. Do NOT fabricate.

### blockers (array of {text, evidence_subject})

Open issues blocking forward progress, observable AT last_activity_ts.
Closed/resolved blockers do NOT count.

`text`: paraphrased blocker ≤ 25 words, present tense.

### outcomes (array of {text, evidence_subject}) — NEW

What ACTUALLY shipped or got deferred — distinct from decisions (intent).

Look for:
- "rolled out", "live", "merged", "shipped", "deployed N%", "deferred to ..."
- PR merged with stated effect
- Slack threads with "live now" / "moved to Q3" / "scrapped"

Cite the evidence_subject. Empty `[]` if work is mid-flight with no observed
outcome yet.

### followups (array of {text, evidence_subject}) — NEW

Open TODOs at last activity. Lighter than blockers (not stopping progress
but explicitly listed). Look for:
- "Action items:" / "todo:" lists in threads
- "Need to ..." statements not yet picked up
- "post-rollout checks pending" / "monitor for X"

Empty `[]` if none observed.

### risk_areas (array of {text, evidence_subject}) — NEW

Known unknowns or things that broke during the work. Complements blockers
(blocker = stopped; risk = "watch this"). Look for:
- "race condition observed under load"
- "edge case with concurrent ..."
- "memory leak when ..."
- Post-incident reports linked to this work

Empty `[]` if none.

### root_cause (string OR null)

ONLY for incident-flavoured clusters (triage, perf debug, alerts, hotfix).
One-line causal phrase ≤ 30 words. Not a description — the **cause**.

Examples:
- "DB lock contention on account_balance during defrag window"
- "Grafana threshold tuned too tight on IMPS fraud decline"
- "VendorX inward NEFT settlement-account mismatch with service-c NICR"

`null` for non-incident clusters.

### stakeholders (array of {name, role}) — NEW

People OUTSIDE the contributor list who decide / are affected. Mention them
when they appear in slack threads as approvers, sign-off-givers, or impacted
external owners. Format:

```json
{"name": "example-dev4 Example", "role": "platform-owner-decider"}
```

Roles: `approver`, `decider`, `compliance`, `pm`, `affected-team`, `customer-rep`.

Empty `[]` when none.

### artifacts (array of {type, url, label}) — NEW

Links to non-event resources referenced by the cluster — TRD pages, demo
recordings, dashboards, runbooks.

```json
{"type": "trd",       "url": "...", "label": "instant-pay TRD Phase-1"}
{"type": "dashboard", "url": "...", "label": "Grafana service-c-Txn"}
{"type": "demo",      "url": "...", "label": "payments demo recording"}
```

Types: `trd`, `design`, `dashboard`, `runbook`, `demo`, `report`, `other`.

Extract from member content where the URL is verbatim. Empty `[]` if none.

### participant_roles (object: {person_canonical: role})

For each human participant assigned ONE role:
- AUTHOR — initiated the work (created ticket, raised thread, opened PR)
- RESPONDER — replied early, didn't drive resolution
- RESOLVER — drove the fix / posted resolution / merged the PR
- REVIEWER — reviewed / approved without authoring or resolving
- DECIDER — made or confirmed the key decision

One role per person — pick highest-leverage. Skip bots. Use canonical from
`participants_observed` in the dump (already resolved by script).

`{}` for pure recurring bot templates.

## Output shape

One entry per cluster. Single JSON array. Save to:

```
state/verdicts.cluster_finalize.json
```

```json
[
  {
    "cluster_id": 53,
    "label": "service-a instant-pay recon + account-status enhancement + txn retry handling",
    "what_work": "Build instant-pay recon for service-a transactions and harden account-status enhancement under release-24mar branch.",
    "confidence": 0.85,
    "status": "ACTIVE",
    "decisions": [
      {"text": "release-24mar consolidates all deposits + instant-pay recon changes into one cut",
       "evidence_subject": "example-org/service-a#392"}
    ],
    "blockers": [],
    "outcomes": [
      {"text": "instant-pay recon merged into release-24mar branch",
       "evidence_subject": "example-org/service-a#374"}
    ],
    "followups": [
      {"text": "Post-rollout: monitor txn-ref retry latency for 1 week",
       "evidence_subject": "slack:C0EXAMPLE:1778475408.846569"}
    ],
    "risk_areas": [
      {"text": "Concurrent reversal handling on deposits accounts in stress test",
       "evidence_subject": "EX-2511"}
    ],
    "root_cause": null,
    "stakeholders": [
      {"name": "Henry", "role": "approver"}
    ],
    "artifacts": [
      {"type": "trd", "url": "https://your-org.atlassian.net/wiki/pages/EXAMPLE_PAGE_ID",
       "label": "IFT Flow Revamp TRD"}
    ],
    "participant_roles": {
      "frank-example": "AUTHOR",
      "dan": "RESOLVER",
      "eve-example": "RESPONDER",
      "carol-agrawal": "RESPONDER"
    }
  }
]
```

Then run:

```bash
.venv/bin/python derive/finalize_refresh.py apply
```

## Notes

- Apply is idempotent (UPDATE by cluster_id). Partial batches OK.
- `auto_recurring.py` runs automatically inside apply — clusters with
  label starting "Recurring " get status + empty arrays auto-stubbed if not
  explicitly provided. You don't have to enrich every Recurring cluster
  by hand.
- The script does NOT mutate `cluster_id`, `member_count`, `source_breakdown_json`,
  `first_ts`, `last_activity_ts` — those come from cluster_diff apply.
- Empty arrays `[]` are valid for any optional field. NULL is treated as "not yet
  filled" (topic_brief_validate flags non-RECURRING clusters with NULL
  decisions_json as WARN).

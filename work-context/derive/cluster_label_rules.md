# Cluster labelling rules (maintained source)

The chat reads a per-dump copy of this file at
`state/pending_cluster_labels.json.rules.md` (written by
`derive/label_clusters.py dump`). Edit THIS file to change the rules; the
dump command will pick up the change on next run.

## Goal

Label each embedding cluster by the **SHARED WORK** across its members.
Not the topic. Not the keywords. Not the source.

Items come from one or more sources:
- `slack:CH:ts` — Slack thread parent
- `EX-NNNN` — Jira ticket
- `page:NNNN` — Confluence page
- `owner/repo#N` — GitHub PR

## Rules (apply in order)

### Rule 1 — Bridge naming (cross-source clusters)

When the cluster spans ≥2 sources, the label MUST name the bridge.
Format: `<work A> + <work B>: <source-A> + <source-B>`.

Examples:
- "Withholding compliance + UTR generation: TRD work across Jira tickets and Slack design discussion"
- "DR drill UAT coordination: scheduling threads + readiness page"
- "DB ops: vacuum tuning + range partitioning across Jira CMRs and Confluence guides"
- "Payout V2 refactor: schema redesign across Confluence designs + GitHub PR"

### Rule 2 — Template callout (recurring bot / ceremony)

If members share verbatim boilerplate (same bot, same header, same alert
template, same retrospective template), lead the label with
`Recurring <thing>:`. Do NOT pretend it's a workstream — it is a pattern.

Examples:
- "Recurring Grafana FIRING alerts: IMPS fraud + autoscale triggers"
- "Recurring Opsgenie closed-alert template: API error firings auto-closed"
- "Recurring Slack channel join/leave events"
- "Recurring RO sprint retrospective template pages"
- "Recurring PG oncall weekly summary pages"

### Rule 3 — Work-frame for single-source coherent

Name the *workstream*, not the keyword. Verb-ish noun phrase, ≤15 words.

Bad: "Redis", "Things from service-c-Transactions", "Bugs", "Slack threads about deployments"
Good: "DB ops: vacuum tuning + range-partition indexes",
      "Production data fixes: transaction-status terminal-state corrections",
      "Payments SDK design docs: TPAP + instant-pay SDK + Mandate state machine"

### Rule 4 — Composite work for mixed-shape clusters

When the cluster lumps related-but-distinct work (e.g. graceful shutdown +
CI gating + AI tooling under one Epic), the label captures the **bucket**
("service-a platform hardening") rather than forcing one theme. `what_work`
elaborates the bucket members.

### Rule 5 — Confidence calibration

| Confidence | When |
|---|---|
| 0.90+      | All members fit cleanly — tight workstream OR clean template |
| 0.75–0.89  | One looser member but core theme is clear |
| 0.50–0.74  | Reasonable bucket but ≥2 members feel forced; LLM-split candidate later |
| < 0.50     | No coherent frame; flag for re-clustering |

### Rule 6 — Source-field is metadata only

Never name a source in the label except as part of a bridge expression
("...across Jira and Slack"). Never use "slack threads about X" framing.
`sources_breakdown` is for the verdict's bookkeeping only.

## Output shape

For every cluster you label, emit one entry. Wrap all entries in a single
JSON array. It is OK to label a subset of clusters in a batch — `apply` is
idempotent and additive.

```json
{
  "cluster_id": <int>,
  "label": "<≤15 word label>",
  "what_work": "<one sentence ≤25 words>",
  "confidence": <0.0 - 1.0>
}
```

Save to: `state/verdicts.cluster_labels.json`

Then run: `.venv/bin/python derive/label_clusters.py apply`

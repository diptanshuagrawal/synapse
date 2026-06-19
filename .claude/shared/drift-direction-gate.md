# Shared rule — doc-drift direction gate

Loaded by the doc-drift skills (`/doc-sync`, `/doc-sync-sweep`). Classify EVERY
doc-vs-code/decision mismatch before emitting it — only one direction is worth reporting.
Skill-specific scope (which docs, inline-comment vs change-list, dry-run) stays in the skill.

## Classify every finding

- **FORWARD (doc ahead of code)** — doc describes something the code doesn't have YET.
  Confirm via: Status ∈ {Draft, In Progress, In Review} OR open/in-flight jira.
  → This is "not built yet," **NOT drift. Suppress by default** — at most list under a short
  "Doc ahead of code (planned)" section with the tracking ticket. Do NOT emit "fix the doc"
  edits for forward findings. (Validated: the Lien/Un-lien v2 TRD is Draft + unbuilt —
  `liens`/`lien_activities`/new order-types/routes all absent; correct output = "planned,"
  not a drift punch-list.)

- **BACKWARD (code ahead of / diverged from doc)** — feature is built (Status ∈
  {Approved, Done} OR code clearly present) and the code differs from the doc.
  → This is **REAL drift. Emit a suggested doc edit.** (Validated: instant-pay-ATM Charges is
  built; schema matched cleanly, but the `OnTransactionFailed` hook-table row overstated what
  the code does — a real backward-drift edit.)

- **AMBIGUOUS** — Status unknown and code partially present. State the uncertainty; propose
  verification, not an edit.

## Clean passes count

When code MATCHES the doc, record it as a clean pass — do NOT invent an edit. A low
false-positive rate is the point (instant-pay-ATM's 4 schema tables all matched; saying so
builds trust). Never pad: a doc with no backward-drift is reported clean, not forced to yield
a finding.

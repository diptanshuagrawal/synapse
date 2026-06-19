Classify pending subjects using the project classification rules, then write verdicts to `state/verdicts.json`.

## Usage — `/classify`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run any steps.

**What it does:** Reads pending subjects, classifies each into project slugs + team ownership using the authoritative rules file, and writes verdicts to `state/verdicts.json` for `manual-rollup.sh apply` to consume.

**Usage:** `/classify` — takes no arguments.

Prereq: run `/rollup` (or `manual-rollup.sh dump`) first to generate pending subjects.

## Steps

**Follows the shared dump→classify→apply harness — `.claude/shared/classify-apply-harness.md`**
(maker-checker, rules-first, schema-strictness, confidence gate 0.7, verdict-file format).
Below is classify's specific schema + triage; the invariants live in the shared chunk.

**1. Check pending file exists**

```bash
ls $HOME/context/work-context/state/pending_classification.json 2>/dev/null || echo "NOT_FOUND"
```

If NOT_FOUND: tell the user to run `manual-rollup.sh` first to generate pending subjects. Stop.

**2. Read classification rules — FIRST, before subjects** (shared "Rules first")

Read the full rules file:
`$HOME/context/work-context/state/pending_classification.json.rules.md`

Authoritative source for slug enum, SYSTEM_PROMPT, schema, and confidence threshold.

**3. Read all pending subjects**

Read:
`$HOME/context/work-context/state/pending_classification.json`

**4. Classify every subject**

For each subject, first triage signal strength:

**Triage rules:**
- Rich signal (decent title + body OR matterai_summary OR epic_body OR confluence_body): classify directly.
- Thin GitHub subject (empty/near-empty body AND no matterai_summary): FETCH the PR diff first using `gh`:
  ```bash
  gh pr diff <pr-num> --repo <owner/repo>
  ```
  Subject format is `<owner/repo>#<num>`. Read up to ~150 lines of diff to identify the touched paths/modules, then classify.
- Sync/conflict-resolve PRs (title is just "sync" / "merge main" / similar): do NOT fetch diff. Use `domains: [], confidence: 0.80` per rules.md.

After triage and (if needed) diff fetch, produce one verdict per subject:

```json
{
  "subject": "<echo unchanged>",
  "content_hash": "<echo unchanged>",
  "domains": ["slug1", "slug2"],
  "summary": "Action-first present tense ≤200 chars",
  "risk_flags": [],
  "confidence": 0.85
}
```

Rules (from rules.md):
- `domains`: only slugs from the project slug enum in rules.md. Empty list OK.
- `summary`: ≤ 200 chars, action-first, present tense. No filler.
- `risk_flags`: only `{security, data-loss, panic, race, migration, breaking-api}`. Empty when none.
- `confidence`: calibrated 0–1. After diff fetch you should reach ≥ 0.70 for almost all cases. Only emit < 0.7 if the diff itself is empty/unreadable.
- `epic_domain` non-empty → that slug MUST appear in `domains`.
- DO NOT set `needs_diff: true` — that flag is dead. Fetch the diff inline instead.

**5. Write verdicts**

Write the complete JSON array to:
`$HOME/context/work-context/state/verdicts.json`

**6. Print classification summary**

Show:
- Total subjects classified
- Count stored (confidence ≥ 0.7 and needs_diff not set)
- Count deferred: confidence < 0.7
- Count deferred: needs_diff: true
- Any subjects where epic_domain was set (confirm it appears in domains)

**7. Remind user**

```
✓ verdicts written → state/verdicts.json

Next: run manual-rollup.sh apply
```

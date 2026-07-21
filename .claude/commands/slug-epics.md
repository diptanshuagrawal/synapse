Create human-readable slugs for unmapped Jira epics referenced by recent subjects.

## Usage — `/slug-epics`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run anything.

**What it does:** Creates human-readable slugs for unmapped Jira epics referenced by recent subjects. Normally auto-triggered by `/rollup` when dump detects unmapped epics.

**Usage:** `/slug-epics` — takes no arguments.

Triggered by `/rollup` when `derive/manual-rollup.sh dump` detects unmapped epics. Reads:

- `$HOME/context/work-context/state/pending_slug_creation.json` (epic + child context)
- `$HOME/context/work-context/state/pending_slug_creation.json.rules.md` (schema + constraints)

## Procedure

1. Read the rules.md AND the pending_slug_creation.json in ONE parallel Read
   block (independent files — never two turns). rules.md confirms the verdict
   schema + slug constraints; each pending entry has `epic_key`, `epic_title`,
   `epic_body_snippet`, and a `children` list of recent child tickets.
3. For each epic, synthesise:
   - `slug`: kebab-case, 2-5 tokens, drawn from the **dominant child-ticket theme** (not the epic title alone). Never `epic-<key>`.
   - `name`: ~60 char human-readable label.
   - `keywords`: 3-6 **bigrams** that future PRs/Jira are likely to contain.
   - `merge_into`: when an existing slug in `config/projects.yaml` already covers this domain (e.g. a generic Bug-fixes epic that maps to a known program), set this to the existing slug; otherwise omit/null.
4. Generic name guard: if the epic title is generic ("Bug fixes", "Onboarding", "Misc") AND children don't sharpen the theme, prefix the slug with the jira prefix (e.g. `EX-onboarding`, `EX-misc-bug-fixes`).

## Output

Write the complete verdict array to:
`$HOME/context/work-context/state/verdicts.epic_slugs.json`

```json
[
  {
    "epic_key": "EX-XXXX",
    "slug": "human-readable-kebab",
    "name": "Short title",
    "keywords": ["multi-word bigram", "another bigram"],
    "merge_into": null
  }
]
```

## Apply + resume (ONE chained call)

```bash
cd $HOME/context/work-context && derive/manual-rollup.sh apply-slugs && derive/manual-rollup.sh dump
```

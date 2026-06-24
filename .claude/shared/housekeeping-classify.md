# Shared — housekeeping classification (scan facts → verdicts → render)

Loaded by `/housekeeping-review` (on-demand) and the `housekeeping-review` routine.
Owns the COMMON judgement layer that turns the deterministic scan into Approve/Reject
suggestions. Scripts produce facts; THIS session produces the judgement; Relay gates
the write. No script here calls an LLM.

## Inputs / outputs
- IN  — `work-context/state/housekeeping_candidates.json` (from `bin/housekeeping.sh --scan`).
- IN  — `work-context/state/housekeeping_rejected.json` (ledger; never re-propose these keys).
- OUT — a verdicts file you write (small): `{"verdicts": {"<key>": {recommendation, risk, reason}}}`.
- OUT — rendered deterministically by `derive/housekeeping_render.py` into:
  - `work-context/state/housekeeping_suggestions_<run-id>.json` (Relay payload, ACTIONABLE only)
  - `work-context/derived/housekeeping-suggestions.md` (human report, everything).

## STEP A — read the candidates
Read `state/housekeeping_candidates.json`. Each candidate has: `key, category, path,
abs_path, size_bytes, size_h, age_days, git, detail`. If `summary.n == 0` → nothing to
classify; write an empty verdicts file `{"verdicts":{}}`, render, and post the "nothing
further to clean ✅" card. Do NOT fabricate candidates.

## STEP B — assign a verdict per candidate
For EVERY candidate, decide exactly one `recommendation`:
`delete` · `truncate` · `worktree_remove` · `keep` · `investigate`.
Plus a `risk` (`low|medium|high`) and a one-sentence human `reason` (why safe + worth it,
or why hold). Only `delete|truncate|worktree_remove` get carded; `keep|investigate` are
report-only.

HARD RULES — the renderer re-enforces these, but classify correctly up front:
- **NEVER** recommend deleting `git: tracked`. Tracked source is out of scope → `investigate`.
- **NEVER** anything under `.git`, the live `events.db`, or the repo root.

Per-category guidance (be conservative — when unsure, `investigate`, not `delete`):
- **db_backup** — KEEP the newest 1–2 backups as a safety net; `delete` the older ones.
  Mention age in the reason. Low risk (regenerable; events.db is the live copy).
- **log** — `truncate` (never delete — keeps the file handle the writer holds). Low risk.
- **pycache** / **cache** — `delete` (bytecode / `.diff_cache` / `.*_cache` dirs, regenerated on next run). Low risk. Surfaced as one dir-level candidate each.
- **preview_bloat** — `delete` (dashboard regenerates it). Low risk.
- **derived_stale** — `delete` if clearly pipeline-regenerable (most of `derived/`).
  For curated-looking files (e.g. `derived/people/*.md`) lean `keep`/`investigate`. Low–med.
- **state_orphan** — default `investigate`: state can carry meaningful run history.
  `delete` only if it's plainly a one-off dump AND old. Medium risk.
- **untracked_large** — default `investigate` (could be work-in-progress); do NOT auto-delete.
- **worktree** — `investigate` unless you can confirm the branch is merged/abandoned; then
  `worktree_remove` (medium risk). Never guess.
- **large_file** — `investigate` (surface it; let the owner decide).

## STEP C — write verdicts + render
Write the verdicts file (e.g. `state/housekeeping_verdicts.json`):
```json
{"verdicts": {"<key>": {"recommendation": "delete", "risk": "low",
                        "reason": "Old migration backup; events.db is healthy."}}}
```
Then render deterministically (cwd = `work-context`):
```bash
.venv/bin/python -m derive.housekeeping_render --verdicts state/housekeeping_verdicts.json
```
It prints the actionable count + the `run_id`, writes the payload + the `.md` report, drops
any rejected-ledger keys, and downgrades any tracked path that slipped through.

## STEP D — post Approve/Reject cards to #rollup
```bash
cd "$HOME/context" && bin/relay_bot.py --post-housekeeping <run-id>
```
(`<run-id>` is the one the renderer printed.) The Relay LaunchAgent (already live,
`RELAY_APPLY_MODE=live`) handles the click: **Approve** → `bin/housekeeping_apply.py`
git-safe delete/truncate; **Reject** → recorded so it never re-appears. If the bot errors
(not in #rollup / token), surface the stderr — do NOT silently succeed.
@relaybot must be a member of #rollup for the post to land.

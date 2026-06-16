Automated, recurring doc-drift sweep over every doc the team owns. Loops the
inventory, runs the `/doc-sync` drift check on each, posts inline comments for NEW
findings (owner tagged, owner-of-the-skill cc'd), records them, and publishes ONE
Slack summary. Dedup-safe: never re-posts a finding already raised. Owner-invoked or
cron-invoked.

## Usage — `/doc-sync-sweep [options]`

If invoked with `help`, `-h`, or `--help`: print this Usage block verbatim and STOP.

**What it does:** Runs `/doc-sync` across the whole team-doc inventory in one pass,
comments on new drift, and posts a single per-owner Slack roundup. Propose-only on the
docs (inline comments, never edits the page body). Safe to run repeatedly — the dedup
gate guarantees a finding is commented at most once.

**Options:**
- `--target dm|channel` — where the Slack summary goes. Default `dm` (test). `channel`
  posts to the team room (see `config/doc_sync.yaml`).
- `--dry-run` — do everything EXCEPT post comments / Slack. Prints what it *would* post.
- `--only <pageId>` — sweep a single doc (debug / re-check one).
- `--run-id <YYYY-MM>` — label for this sweep. Default = current year-month.
- `--allow-resolved-reflag` — re-raise a finding whose only prior comment was Resolved
  (drift came back after a dev closed it). Off by default — we don't nag.

New to this? Run `/doc-sync-sweep --dry-run` first. It posts nothing; it just shows the
inventory it scanned and the findings it would raise.

## What a developer does with a comment (tell new joiners this)

Each finding is a Confluence **inline comment** anchored to the relevant text, tagging
the doc owner. Two actions only:
- **Reply** in the thread — to discuss or push back. The thread stays **open**.
- **Resolve** — when handled (doc fixed, OR decided it's not real drift). This is the
  done signal: it drops the item off the Mon/Wed/Fri pending digest.

One line for newcomers: *"Reply to discuss, Resolve when handled."*

## Phase 0 — Load config + inventory

```bash
cd $HOME/context/work-context
```

- Inventory: `config/doc_sync_inventory.yaml` → use the **`monitor`** list only. Never
  touch `needs_confirm` or `excluded`. Each entry: `{id, title, owner, repo, kind}`.
- Owner map: `config/people.yaml` — resolve `owner` (canonical) → `jira_id` (Confluence
  mention) + `slack_id` (Slack mention). The cc target (skill owner) is `org.owner_*` /
  the configured `owner` entry.
- Slack target: `--target` → `dm` uses the owner's Slack user id; `channel` uses the
  team channel id. Keep both in `config/doc_sync.yaml` (create if absent:
  `slack: {dm_user_id: <owner dm user id>, channel_id: <team channel id>, cc: <cc_account_id>}`).
- State + dedup + render helpers: `derive/doc_sync_state.py` (network-free).
- cloudId `YOUR_CONFLUENCE_CLOUD_ID`.

`doc_sync_state.py init` (idempotent) before anything.

## Phase 0.5 — Discover new team docs (self-maintaining inventory)

Before checking, refresh the doc universe so newly-created docs get caught. Discovery
NEVER auto-monitors — new docs land in `needs_confirm` for the owner to promote.

1. Run CQL over the team's spaces (inventory `meta.spaces_scanned`) using the
   service/domain keywords in inventory `meta.discovery_terms` (the same terms that
   seeded the inventory): `searchConfluenceUsingCql`
   `(title ~ <term> OR …) AND lastmodified >= <~120d>`. Read both lists from
   `config/doc_sync_inventory.yaml` `meta` — never hardcode space keys or service names here.
2. Apply the FULL inclusion filter to each hit (all three — same as the inventory):
   (a) about an owned service (inventory `meta.owned_services`); (b) author ∈
   `config/people.yaml` `scope: team`; (c) NOT ops/RCA/oncall/incident/report/perf/
   setup/tracking. Drop anything failing any leg.
3. Write the surviving candidates to `state/doc_sync_discovered.json`
   (`{"candidates":[{id, title, author, repo}]}`).
4. Merge — append only the genuinely-NEW ids to `needs_confirm` (dedup vs every bucket):
   ```bash
   .venv/bin/python derive/doc_sync_state.py discover-merge \
       --inventory config/doc_sync_inventory.yaml \
       --candidates state/doc_sync_discovered.json --write
   ```
5. If it reports `new > 0`, include a "🔎 N newly-discovered docs added to needs_confirm —
   review + promote to monitor" block in the Slack summary (with titles + owners). These
   are NOT swept this run — only the existing `monitor` list is checked below.

This step runs even on `--dry-run` (inventory maintenance is safe — it posts no comments).
It works headless/cron (CQL is available without a browser).

## Phase 1 — Per-doc drift check (reuse `/doc-sync`)

For each `monitor` doc, run the **exact `/doc-sync` mechanics** — do not reinvent them.
Read `.claude/commands/doc-sync.md` Phases 2–4:
1. Fetch the page (`getConfluencePage`, markdown) → status, last-updated, repo.
2. Gather code truth for `repo` (code graph + migrations + source) and recent jira/slack.
3. Run the five drift checks + the **DIRECTION GATE**. Keep only **BACKWARD-drift**
   findings (code built, doc diverged). Suppress forward/planned — they are NOT drift.
4. For each backward finding, produce a candidate:
   `{page_id, page_title, page_url, owner_account, severity (major|medium|minor),
     check_type (schema|behavior|decision|dependency|lld|sequence), finding_title (one
     plain line), anchor (exact rendered page text to attach to), suggested_edit}`.

Notes:
- `repo: casa-orch` or any unregistered repo → skip the doc, log "not checkable here".
- An empty/placeholder page → skip, log it.
- **Diagrams (sequence drift):** only check a diagram when its SOURCE is machine-readable
  — i.e. inline Mermaid text (```mermaid fences) in the page body/ADF. The team's docs
  mostly use the **ZenUML "Diagram as Code Lite" macro** (source lives in app custom-content,
  NOT in the page API) or **image/PNG blobs** — for those the steps are NOT extractable.
  When a doc's flow lives in a non-readable diagram, do NOT infer steps from the title or
  guess. Emit at most ONE low-severity note ("sequence diagram is ZenUML/image — source not
  machine-readable, verify manually") and move on. Never fabricate a sequence-drift finding
  from an unreadable diagram. (Fetch ADF to detect: a `zenuml-sequence-macro-lite` extension
  or `mediaSingle`/blob = not readable; a `codeBlock` with language mermaid = readable.)
- Keep findings tight and high-confidence — this posts to a shared doc. When unsure,
  drop it. A clean doc producing zero findings is a good outcome.

Pool all candidates → write `state/doc_sync_candidates.json` (`{"candidates":[...]}`).

## Phase 2 — Dedup gate (MANDATORY — never skip)

```bash
.venv/bin/python derive/doc_sync_state.py filter-new \
    --file state/doc_sync_candidates.json --out state/doc_sync_new.json \
    [--allow-resolved-reflag]
```

Only the `new` array is eligible to post. The `skipped` array is already-raised
findings — leave them alone. This is what guarantees a finding is never commented twice.

## Phase 3 — Post inline comments (skip if `--dry-run`)

For each finding in `new`:
- `createConfluenceInlineComment` on `page_id`, anchored via `inlineCommentProperties`
  (textSelection = `anchor`; fetch the page to get the exact match count + index).
  Body, plain-led (per `/doc-sync` voice):
  `@<owner> — <finding_title>. <one-line why + suggested edit>. Reply to discuss, Resolve when handled. cc @<skill-owner>`
  Mentions = `<span data-type="mention" data-user-id="ACCOUNT_ID">@Name</span>`
  (owner = people.yaml `jira_id`; cc = configured cc id).
- If the anchor is inside a code block or fails to match, fall back to a footer comment
  (`createConfluenceFooterComment`) and note it.
- Capture `comment_id` + build `comment_url` = `<page_url>?focusedCommentId=<comment_id>`.

Collect into a record batch with all state columns + `sweep_run_id` + `resolution_status: open`.

## Phase 4 — Record + publish

```bash
.venv/bin/python derive/doc_sync_state.py record --file state/doc_sync_record.json
.venv/bin/python derive/doc_sync_state.py render-sweep --run-id <run-id> \
    --date "<DD Mon YYYY>" --cc <cc_account_id>
```

Post the rendered message to the Slack target (`--target`). Use `slack_send_message`
(markdown + `<@slack_id>` mentions). Skip the Slack post on `--dry-run` (print instead).

If `new` is empty: post a one-line "Monthly sweep — all <N> docs clean, no new drift."
(or just log it on `--dry-run`). A clean sweep is a valid, valuable result.

## Phase 5 — Chat reply

End with: docs scanned · new findings posted · deduped (already-open) · clean · skipped
(not-checkable/empty), and the Slack message link. Save nothing else — state.db is the
record.

## Hard constraints

- Inline comments only; NEVER edit a page body or call `updateConfluencePage`.
- ALWAYS run `filter-new` before posting (Phase 2). No exceptions.
- Only the `monitor` list. Never comment on `needs_confirm` / `excluded` docs.
- Honour the direction gate — forward/planned findings are not drift, do not post them.
- `repo` not registered → "not checkable here", skip. Never fabricate a finding.
- Owner + cc come from config — never hardcode names/ids in the skill.
- Default `--target dm` until the owner flips it to `channel`.

## Anti-patterns (refuse)

- Posting a finding that `filter-new` put in `skipped`.
- Commenting on a doc outside `monitor`.
- Editing the Confluence page directly.
- Padding the sweep — a doc with no backward-drift is reported clean, not forced to yield a finding.
- Low-confidence findings on shared docs — when unsure, drop it.

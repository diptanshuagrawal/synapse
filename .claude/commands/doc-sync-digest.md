Recurring pending-review digest for the doc-drift sweep. Refreshes the resolution
status of every comment the sweep has posted, then publishes ONE Slack message
grouping the still-OPEN review threads per developer, with links. Resolved threads
drop off. Owner-invoked or cron-invoked (Mon/Wed/Fri).

## Usage — `/doc-sync-digest [options]`

If invoked with `help`, `-h`, or `--help`: print this Usage block verbatim and STOP.

**What it does:** Polls Confluence for the live resolution status of every tracked
doc-sync comment, updates state, and posts a per-developer "here's what's still open"
roundup to Slack. It posts NOTHING to Confluence — read-only there.

**Options:**
- `--target dm|channel` — Slack destination. Default `dm` (test); `channel` = team room.
- `--dry-run` — refresh status + print the digest, but don't post to Slack.

## Phase 0 — Load config

```bash
cd $HOME/context/work-context
```

- `config/doc_sync.yaml` — Slack target + cc.
- `config/people.yaml` — owner canonical → slack_id (the renderer does this).
- `derive/doc_sync_state.py` — state + renderer. cloudId `YOUR_CONFLUENCE_CLOUD_ID`.

## Phase 1 — Refresh resolution status (the important step)

Read the tracked comments still marked open:

```bash
.venv/bin/python derive/doc_sync_state.py list --open
```

These are grouped by `page_id`. For each page with open tracked comments, fetch the
live comment status:

```
getConfluencePageInlineComments(pageId, resolutionStatus="resolved")
getConfluencePageInlineComments(pageId, resolutionStatus="open")
```

For each tracked comment_id, determine its CURRENT status from the API result
(resolved / open / reopened). Build a statuses batch and write to
`state/doc_sync_statuses.json` (`{"statuses":[{comment_id, resolution_status, last_checked_ts}]}`),
then:

```bash
.venv/bin/python derive/doc_sync_state.py set-status --file state/doc_sync_statuses.json
```

Only fetch pages that currently have open tracked comments — don't re-poll pages whose
items are all already resolved. This keeps the digest cheap.

## Phase 2 — Render + publish

```bash
.venv/bin/python derive/doc_sync_state.py render-digest \
    --date "<Ddd DD Mon YYYY>" --cc <cc_account_id>
```

The renderer groups the remaining OPEN comments per developer (most-open first) with
comment links. If nothing is open, it emits a clean "no pending reviews 🎉" line.

Post the rendered message to the Slack target (`slack_send_message`, `<@slack_id>`
mentions). Skip the post on `--dry-run` (print instead).

## Phase 3 — Chat reply

End with: open threads remaining · newly-resolved-since-last-run · per-dev counts, and
the Slack message link.

## Hard constraints

- READ-ONLY on Confluence — never post/edit/resolve a comment. Only the dev resolves.
- Track only OUR comment_ids (the state table). NEVER count arbitrary page comments —
  pages carry unrelated review threads that must not pollute the per-dev count.
- Owner/slack ids from config — never hardcode.
- Default `--target dm` until the owner flips to `channel`.

## Anti-patterns (refuse)

- Counting inline comments not in the state table.
- Resolving / replying to threads on the dev's behalf.
- Posting to the channel before the owner has flipped the target.

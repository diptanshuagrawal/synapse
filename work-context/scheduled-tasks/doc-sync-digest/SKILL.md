---
name: doc-sync-digest
description: Mon/Wed/Fri 13:00 IST — refreshes resolution status of tracked doc-sync comments and DMs the owner the per-developer list of still-open review threads. Read-only on Confluence.
---

Run the doc-sync pending-review digest. READ-ONLY on Confluence — never post, edit, or
resolve a comment. Posts the digest to the owner's DM (test target).

Working dir: __REPO__

STEP 0 — Resolve a yaml-capable python:
  PY=$(for p in /opt/homebrew/bin/python3 python3 /usr/local/bin/python3; do "$p" -c 'import yaml' 2>/dev/null && { echo "$p"; break; }; done)

STEP 1 — Run the digest skill EXACTLY as defined in `.claude/commands/doc-sync-digest.md`,
with option:  `--target dm`.
- `$PY derive/doc_sync_state.py list --open` → tracked comments still open.
- For each page with open tracked comments, poll `getConfluencePageInlineComments`
  (resolutionStatus open + resolved) and update only OUR tracked comment_ids via
  `$PY derive/doc_sync_state.py set-status --file …`. NEVER count untracked page comments.
- `$PY derive/doc_sync_state.py render-digest --date "<Ddd DD Mon YYYY>" --cc <cc id>` and
  post the result to the owner's DM (config `doc_sync.yaml` slack.dm_user_id) via
  slack_send_message. If nothing is open, send the clean "no pending reviews" line.

STEP 2 — Output: open threads remaining · newly-resolved-since-last-run · per-dev counts.

HARD RULES: read-only on Confluence; track only our own comment_ids; DM target until the
owner flips to the team channel. The dev's own Resolve action is the only thing that
clears an item.

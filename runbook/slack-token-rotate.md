# Slack User Token (xoxp-) Rotation Runbook

## When to rotate

- Owner offboards
- Token leak suspected (committed to git, posted in chat, etc.)
- Slack admin revokes scope on policy change
- Routine: every 6 months

## Generate

1. Go to https://api.slack.com/apps → either:
   - Use existing **example context-ingest** app (preferred), or
   - Create new app "Context Ingest" in example workspace
2. **OAuth & Permissions** → **User Token Scopes** → add:
   - `channels:history`
   - `groups:history`
   - `im:history`
   - `mpim:history`
   - `users:read`
   - `users:read.email`
   - `channels:read`
   - `groups:read`
3. **Install to Workspace** → approve in browser as owner
4. Copy **User OAuth Token** (starts `xoxp-...`)

## Install

```bash
# ~/context/.env (gitignored; do NOT commit)
SLACK_USER_TOKEN=xoxp-<paste-here>
```

Verify gitignored:

```bash
cd ~/context && git check-ignore .env
# expected: .env (matched)
```

## Test

```bash
cd ~/context/work-context
.venv/bin/python -m ingest.slack_api_client
```

Expected output:

```json
{
  "ok": true,
  "user": "<your-slack-handle>",
  "team": "example",
  ...
}
```

Failure modes:
- `SLACK_USER_TOKEN missing` → `.env` not loaded or empty
- `must start with 'xoxp-'` → using bot token by mistake
- `invalid_auth` → token revoked or wrong workspace
- `ANTHROPIC_API_KEY is present` → unset LLM keys before running ingest scripts (chat-only-classification policy)

## Revoke old

After verifying the new token works:

1. https://api.slack.com/apps → OAuth & Permissions → **Revoke Token**
2. Confirm in Slack admin audit log
3. Update this runbook's "last rotated" line below

---

**Last rotated:** _(none — initial setup pending)_
**Token holder:** owner@example.com

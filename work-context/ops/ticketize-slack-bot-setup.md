# Ticketize Slack bot — setup (Socket Mode, owner-only)

One-time owner setup. After this, paste the two tokens into `~/.secrets/ticketize_slack.env`
and tell Claude to build `bin/ticketize_slack_app.py` + the LaunchAgent.

## 1. Create the app from manifest
api.slack.com/apps → **Create New App** → **From a manifest** → pick the workspace →
paste this YAML:

```yaml
display_information:
  name: Ticketize Bot
  description: Approve/Reject ticketize candidates in #track-work
features:
  bot_user:
    display_name: ticketize-bot
    always_online: true
settings:
  socket_mode_enabled: true          # native, no public endpoint
  interactivity:
    is_enabled: true                 # buttons; Socket Mode delivers actions over the socket
  org_deploy_enabled: false
  token_rotation_enabled: false
oauth_config:
  scopes:
    bot:
      - chat:write                   # post candidates + edit/confirm
      - groups:history               # read the private #track-work (private = "group")
      - groups:read
      - users:read                   # resolve/verify the clicking user
```
(No event subscriptions / request URL needed — Socket Mode.)

## 2. Generate the two tokens
- **App-level token** (for Socket Mode): Basic Information → **App-Level Tokens** →
  Generate → name `socket`, scope **`connections:write`** → copy the **`xapp-…`**.
- **Bot token**: Install App → Install to workspace → copy the **Bot User OAuth Token `xoxb-…`**.

## 3. Add the bot to the channel
In Slack: `/invite @ticketize-bot` in **#track-work** (`<channel id>`).

## 4. Store the tokens (gitignored)
Create `~/.secrets/ticketize_slack.env`:
```
SLACK_BOT_TOKEN=xoxb-…
SLACK_APP_TOKEN=xapp-…
```
(`chmod 600`. Confirm `~/.secrets/` is not in any repo.)

## 5. Python dep
`/opt/homebrew/bin/python3 -m pip install slack_bolt`

## Then → Claude builds
- `bin/ticketize_slack_app.py` — Bolt Socket Mode app:
  - `--post <date>` entrypoint: reads `management/standup/<date>/ticket-candidates.md`,
    posts one Block Kit section per open candidate to #track-work with **Approve / Reject**
    buttons (+ bulk **Approve all**); `action_id` carries `{date, fingerprint}`.
  - long-running listener: on click → verify `user.id == owner` → record decision →
    trigger the **existing gated apply** (headless `/ticketize apply` or local apply runner;
    bot NEVER calls Jira directly) → edit message / reply with created `EX-NNNN`, disable buttons.
- `com.example.ticketize-bot.plist` — LaunchAgent keeping the listener alive (loads the env).
- DETECT routine updated to call `--post <date>` after writing the candidate md.

Owner-only, idempotent, fail-loud — same invariants as the rest of the pipeline.

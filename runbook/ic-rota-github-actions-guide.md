# Running an IC-Rota Bot on GitHub Actions — Portable Implementation Guide

A team-agnostic guide to running an **Incident-Commander rota bot** (next-IC reminders +
on-call channel topic sync) on **GitHub Actions**, driven by Opsgenie + Slack. No org identity
in this doc — every team-specific value lives in secrets/config you supply.

Reference implementation: `bin/ic_rota_bot.py` (pure stdlib + PyYAML; no framework).

---

## 1. What the bot does

Runs on a schedule. Each run does two idempotent things:

- **Remind**: if the NEXT IC stint starts within `remind_days_before`, post a heads-up in the IC
  channel tagging that person (confirm availability / arrange swap). Posted once per (stint, person).
- **Sync topic**: if the on-call channel topic ≠ the CURRENT IC, set the topic and post an
  old→new handover note.

**Mode safety**: `mode: test` (default) routes every post to a private test channel and never
touches the real topic. `mode: live` posts for real. There is no CLI flag to force live — you flip
the config deliberately.

**Rota source**: Opsgenie `GET /v2/schedules/{id}/timeline` (approved overrides already folded in),
or a static yaml list as a stopgap.

---

## 2. Why GitHub Actions is a good host for this

- **Runs the Python unchanged** — no logic port, so the subtle Opsgenie/timezone/override handling
  stays exactly as written and tested. This is the single biggest reason to prefer it over a
  low-code platform that would force a rewrite.
- **Native secrets** — Opsgenie key, Slack token, config all go in Actions secrets (safe even in a
  public repo; secrets are never exposed to forks or logs).
- **Schedule in one cron line** — replaces a laptop launchd/cron. Runner timezone is UTC, but the
  bot computes "now" in its own timezone internally, so UTC runners are fine.
- **Free** — GitHub-hosted runner minutes are free for public repos; trivial usage for private.
- **Cron lag is irrelevant** — scheduled Actions can be delayed/skipped under load, but the job is
  hourly and idempotent, so the next run self-heals.

### The one thing Actions does NOT give you: durable state

Runners are ephemeral. The bot's dedup/topic state (`ic_rota_state.json`) must live outside the run.
Two supported backends:

- **Option A — Secret Gist** (use when the Action lives in a repo you can't commit identity to,
  e.g. a public repo). State is one file in a private gist, read at start / written at end.
- **Option B — Dedicated private repo** (recommended for a real team deployment). The Action lives
  in a team-owned private repo and commits the state file back to itself. No gist, better governance.

The state file contains real emails + Slack IDs → **never commit it to a public repo.**

---

> **Starting from scratch?** If your team has no Slack app or Opsgenie API key yet, create them
> first — see **Appendix A**. The secrets below assume both already exist.

## 3. Secrets to configure (repo → Settings → Secrets and variables → Actions)

| Secret | What it is |
|---|---|
| `OPSGENIE_IC_API_KEY` | Opsgenie API key with read access to the IC schedule |
| `RELAY_SLACK_BOT_TOKEN` | Slack bot token (`xoxb-…`) |
| `IC_ROTA_CONFIG_YAML` | The full `ic_rota.yaml` contents (see §5) — carries identity, so it's a secret |
| `STATE_GIST_ID` | (Option A) id of the private gist holding state |
| `STATE_GIST_TOKEN` | (Option A) a PAT with **gist** scope, to read/write that gist |

**Required Slack bot scopes**: `chat:write`, `channels:read` (+ `groups:read` for private channels),
`channels:manage` (setTopic), and — only if you resolve IDs via Slack instead of config —
`users:read.email`. The bot must be a **member** of both the on-call and IC channels.

---

## 4. The workflow file (`.github/workflows/ic-rota.yml`)

Option A (gist state) shown. For Option B, replace the pull/push steps with a `git commit` of the
state file back to the private repo.

```yaml
name: ic-rota-bot

on:
  schedule:
    # Example: hourly 09:10–23:10 IST == 03:40–17:40 UTC (IST = UTC+5:30).
    # Adjust the minute/hour window to your timezone. Runner is UTC.
    - cron: '40 3-17 * * *'
  workflow_dispatch: {}        # manual trigger for testing

permissions:
  contents: read

concurrency:
  group: ic-rota               # never overlap runs — protects state integrity
  cancel-in-progress: false

jobs:
  rota:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install deps
        run: pip install pyyaml

      - name: Hydrate secrets → ~/.secrets
        env:
          OPSGENIE_IC_API_KEY: ${{ secrets.OPSGENIE_IC_API_KEY }}
          RELAY_SLACK_BOT_TOKEN: ${{ secrets.RELAY_SLACK_BOT_TOKEN }}
        run: |
          mkdir -p "$HOME/.secrets"
          printf '%s' "$OPSGENIE_IC_API_KEY"   > "$HOME/.secrets/opsgenie_ic_api_key"
          printf '%s' "$RELAY_SLACK_BOT_TOKEN" > "$HOME/.secrets/relay_slack_bot_token"
          chmod 600 "$HOME"/.secrets/*

      - name: Hydrate config
        env:
          IC_ROTA_CONFIG_YAML: ${{ secrets.IC_ROTA_CONFIG_YAML }}
        run: |
          mkdir -p work-context/config work-context/state
          printf '%s' "$IC_ROTA_CONFIG_YAML" > work-context/config/ic_rota.yaml

      - name: Pull state from gist (seed fresh on first run)
        env:
          GH_TOKEN: ${{ secrets.STATE_GIST_TOKEN }}
          GIST_ID: ${{ secrets.STATE_GIST_ID }}
        run: |
          if gh api "gists/$GIST_ID" --jq '.files["ic_rota_state.json"].content' \
               > work-context/state/ic_rota_state.json 2>/dev/null \
             && [ -s work-context/state/ic_rota_state.json ]; then
            echo "state: pulled from gist"
          else
            echo '{"reminded":{},"last_topic":null}' > work-context/state/ic_rota_state.json
            echo "state: seeded fresh"
          fi

      - name: Run IC-rota bot
        run: python bin/ic_rota_bot.py --remind --sync-topic

      - name: Push state back to gist
        if: always()           # persist dedup even if a later step fails
        env:
          GH_TOKEN: ${{ secrets.STATE_GIST_TOKEN }}
          GIST_ID: ${{ secrets.STATE_GIST_ID }}
        run: |
          jq -n --arg c "$(cat work-context/state/ic_rota_state.json)" \
            '{files:{"ic_rota_state.json":{content:$c}}}' \
          | gh api -X PATCH "gists/$GIST_ID" --input - --silent
```

Why it works with zero code changes: the bot reads secrets from `~/.secrets/<name>`, config from
`<repo>/work-context/config/ic_rota.yaml`, and state from `<repo>/work-context/state/ic_rota_state.json`.
The hydrate steps populate exactly those paths before the run; the push step persists state after.

`gh` and `jq` are preinstalled on `ubuntu-latest`.

---

## 5. Config secret (`IC_ROTA_CONFIG_YAML`)

The full `ic_rota.yaml`, pasted as the secret value. Template (fill the blanks; no identity in git):

```yaml
mode: test                       # test | live
test_channel: "C…"               # private owner-only channel for test posts
rota_source: opsgenie            # opsgenie | static
slack:
  oncall_channel: "C…"           # channel whose topic names the current IC
  ic_channel: "C…"               # reminders land here
topic_template: "Incident Commander: {mention}"
remind_days_before: 2
opsgenie:
  schedule_id: "…"
  schedule_url: "https://…app.opsgenie.com/settings/schedule/detail/…"
  interval_weeks: 4
  rotation_filter: "Primary Incident Commander"   # if the schedule mixes rotations
# IMPORTANT on GitHub Actions: people.yaml is NOT present (it's gitignored / local-only),
# so every rota member's Slack ID MUST be provided here:
slack_ids:
  person@example.com: "U…"
```

> The reference `email_to_slack()` checks `slack_ids` first, then a local `people.yaml`. On a
> runner there is no `people.yaml`, so `slack_ids` must cover everyone in the rota, or the run fails
> loudly with "no Slack ID for …". Alternative: extend the bot to call Slack `users.lookupByEmail`
> (needs `users:read.email`) and drop the map.

---

## 6. Option B — private repo with git-committed state

For a team-owned deployment, prefer a dedicated **private** repo. Replace the gist steps:

- Pull: the state file is just committed in the repo — nothing to pull, it's in the checkout.
- Push: after the run, commit it back.

```yaml
      - name: Persist state
        if: always()
        run: |
          git config user.name  "ic-rota-bot"
          git config user.email "ic-rota-bot@users.noreply.github.com"
          git add work-context/state/ic_rota_state.json
          git diff --cached --quiet || git commit -m "ic-rota: update state [skip ci]"
          git push
```

with `permissions: contents: write` on the job. Cleaner governance (no personal gist/PAT), and
state is versioned + auditable. Private repo means committing identity is fine.

---

## 7. Rollout (test → live) with zero double-ping

1. Keep `mode: test`. Trigger manually (`workflow_dispatch`) → confirm posts land in the test channel.
2. **Seed state before going live**: copy the existing `ic_rota_state.json` (from the old
   cron/laptop) into the gist / private repo. Skipping this re-reminds everyone currently in-window
   on the first live run. Pay attention to override fragments (a person can legitimately have two
   reminded keys for one month).
3. Run test-mode on the schedule in parallel with the old job for ≥1 full rota cycle. Diff the
   test-channel output against what the old job posts. Confirm one override cycle is handled cleanly.
4. Flip the `IC_ROTA_CONFIG_YAML` secret to `mode: live`. Same day, disable the old job.
5. Keep the old job (disabled) one more cycle as rollback, then retire it.

---

## 8. Operating notes

- **Logs**: the Actions run log replaces the old logfile. Each run prints what it did
  (`remind: posted for …`, `sync-topic: set → …`).
- **Failure alerts**: add a final `if: failure()` step that posts to a Slack ops channel, or enable
  GitHub's "Actions failure" email/notification.
- **Manual status**: `python bin/ic_rota_bot.py --status` prints the rota + current/next IC and posts
  nothing — keep it as the oracle to sanity-check the schedule.
- **Concurrency**: the `concurrency` block prevents two runs clobbering state. Keep it.
- **Secret hygiene**: never echo secret files; `--silent` on the gist PATCH avoids leaking the token
  in logs. Actions masks registered secrets automatically, but don't `cat` the secret files.

---

## 9. Checklist to stand this up

- [ ] Fork/adapt `bin/ic_rota_bot.py` into the repo that will host the Action.
- [ ] Decide state backend: gist (Option A) or private repo (Option B).
- [ ] Create the Slack app / confirm scopes + channel membership.
- [ ] Add the 3 (Option B) or 5 (Option A) secrets.
- [ ] Add `.github/workflows/ic-rota.yml`.
- [ ] Seed state from the old job.
- [ ] Test-mode parallel run for one cycle.
- [ ] Flip to live; disable old job; keep rollback one cycle.

---

## Appendix A — Create the Slack app (bot) and Opsgenie key from scratch

Do this once per team, before the secrets in §3. You can reuse an existing team bot instead of
making a new one — it just needs the scopes and channel membership below.

### A.1 Create the Slack app

1. Go to **https://api.slack.com/apps** → **Create New App** → **From scratch**.
2. Name it (e.g. `ic-rota-bot`) and pick your workspace.
3. Left nav → **OAuth & Permissions** → **Scopes** → **Bot Token Scopes**, add:
   - `chat:write` — post reminders + handover notes
   - `channels:read` — read a public channel's topic
   - `groups:read` — read a private channel's topic (only if your channels are private)
   - `channels:manage` — set a public channel's topic
   - `groups:write` — set a private channel's topic (only if private)
   - `users:read.email` — optional, only if you resolve Slack IDs via email lookup instead of the
     `slack_ids` config map
4. **Install to Workspace** (top of the same page) → authorize.
5. Copy the **Bot User OAuth Token** (`xoxb-…`). This becomes the `RELAY_SLACK_BOT_TOKEN` secret.
6. **Invite the bot to both channels** — in Slack, in each of the on-call channel and the IC
   channel: `/invite @ic-rota-bot`. Setting a topic and posting both require membership.

> Reusing an existing bot? Verify its granted scopes with a quick check:
> `curl -s -D - -o /dev/null -H "Authorization: Bearer xoxb-…" https://slack.com/api/auth.test | grep -i x-oauth-scopes`
> — the response header lists exactly what the token already has. Add any missing scope in the app's
> OAuth page, then **reinstall** the app for it to take effect.

### A.2 Get the channel IDs

For `test_channel`, `oncall_channel`, `ic_channel` in the config you need channel **IDs**
(`C…`), not names. In Slack: open the channel → channel name → **About** → copy the Channel ID at
the bottom. These go into `IC_ROTA_CONFIG_YAML`.

### A.3 Create the Opsgenie API key

1. Opsgenie → **Settings** → **API key management** (or **App Settings → API key management**).
2. **Add new API key** → name it (e.g. `ic-rota-read`) → grant **Read** access.
3. The key must have read access to the **Incident-Commanders schedule** specifically. A
   team-scoped key often returns `40301 (not authorized)` on an org-wide schedule — if so, use an
   account/org-level key or one explicitly granted on that schedule.
4. Copy the key → becomes the `OPSGENIE_IC_API_KEY` secret.
5. Note the **schedule ID**: open the schedule in Opsgenie; the ID is in the URL
   (`…/schedule/detail/<id>`). Put it + the URL in the config `opsgenie:` block.

### A.4 (Option A only) Create the state Gist + token

1. **https://gist.github.com** → new **secret** gist, filename `ic_rota_state.json`, content
   `{"reminded":{},"last_topic":null}` → **Create secret gist**. The Gist ID is in its URL.
2. **https://github.com/settings/tokens** → generate a token (classic) with the **`gist`** scope
   only → becomes `STATE_GIST_TOKEN`; the Gist ID becomes `STATE_GIST_ID`.

With A.1–A.4 done, return to §3 and add the secrets.

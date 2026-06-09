Steady-state Slack ingest via direct Slack Web API.

## Usage — `/slack-ingest [channel]`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run anything.

**What it does:** Steady-state Slack ingest via the direct Slack Web API — new messages per channel since last cursor, stale-thread reconcile, writes to `events.db`.

**Pipeline per run:** new messages since cursor → Phase 2.5 stale-thread reconcile → Phase 2.7 trailing-window (24h) edit/delete reconcile → refresh `thread_summary` → advance cursors → write `last_slack_success.date`.

**Modes:**
- empty → all channels.
- `<channel-name|id>` → just one channel.

**Usage:** `/slack-ingest [channel]`
- `channel` (optional) — empty → all channels; a name/id ingests just one.

Reads new messages from every channel in `config/slack_channels.yaml` since
their last cursor, writes to `events.db`, runs Phase 2.5 stale-thread reconcile,
runs Phase 2.7 trailing-window (24h) edit/delete reconcile, refreshes
`thread_summary`, advances cursors, writes `state/last_slack_success.date`,
refreshes `state/last_slack_validate.json` cache.

Fired by `launchctl` agent `com.example.slack-ingest` every :00/:30 IST hours
12–22. Owner can also invoke manually with `$ARGUMENTS = <channel-name|id>` for
one channel.

## What this skill does

Invoke the python script. That's it. No per-step MCP orchestration anymore.

```bash
cd $HOME/context/work-context
.venv/bin/python ingest/slack_ingest_app.py $ARGUMENTS
```

If `$ARGUMENTS` empty → all channels. Else single channel.

## Optional flags

```bash
.venv/bin/python ingest/slack_ingest_app.py --dry-run         # parse + validate, no DB write
.venv/bin/python ingest/slack_ingest_app.py <channel> --dry-run
```

## Exit codes

```
0  ≥1 channel ingested OR all up-to-date
1  env/auth/config error
2  no channel succeeded (all errored)
```

## Side-effects

- `state/slack_cursors.json` — per-channel cursor advanced to newest_ts
- `state/last_slack_success.date` — YYYY-MM-DD on any-channel success
- `state/last_slack_validate.json` — refreshed by `run-slack.sh` post-fire (consumed by `bin/cron-status.sh`)
- `events.db` rows inserted/updated (top-level + replies + edits + tombstones)
- Idempotent — re-run safe (upsert dedups by id; cursor never goes backwards)

## Implementation reference

| Component | File |
|---|---|
| Main app | `ingest/slack_ingest_app.py` |
| Slack API client | `ingest/slack_api_client.py` |
| Per-channel orchestrator | `ingest_channel()` in app |
| Phase 2.5 stale-thread reconcile | `fetch_threads_capped()` |
| Phase 2.7 edit/delete reconcile | `reconcile_window_capped()` (24h window) |
| Upsert + tombstone | `derive/slack_upsert.py::upsert_event` / `reconcile_window` |
| Cron wrapper | `ingest/run-slack.sh` |
| Launchagent plist | `launchagents/com.example.slack-ingest.plist` |
| Validator | `derive/slack_validate.py` |

## Caps + limits

- `PAGE_CAP = 10` pages/channel/fire (~2000 msgs) — spillover handled next fire
- `STALE_CAP = 50` parents/channel/fire for stale-thread reconcile
- `RECONCILE_LOOKBACK_HOURS = 24` for edit/delete window
- `RECONCILE_PAGE_CAP = 10` for that window
- Tier-3 rate limit: ~45 req/min (SlackClient throttles internally)
- Users-cache: disk-persisted at `state/slack_users_cache.json` with 24h TTL (cold 24s → warm 0s)

## `ingest_mode: team_involved` channels

For yaml-flagged `team_involved` channels (org-wide oncall, opsgenie,
cross-team announcements), the steady-state path applies the same
team-thread filter as backfill — see `.claude/commands/slack-backfill.md`
for the rules. Key points:

- Filter loaded from `team.md` (UIDs) + `config/team_subteams.yaml`
  (subteam handle ids).
- Reply walk capped at `TEAM_REPLY_CHECK_CAP` per fire to bound API
  cost; spillover continues next fire.
- Bot-authored roots (PagerDuty, OpsGenie, "Alert Incident Commander")
  are retained when team participates in replies — they are the
  incident header.

If a channel is freshly flipped from `full` to `team_involved`, run
`python -m derive.slack_team_filter_cleanup --apply` to purge historical
non-team rows before re-rollup. Cleanup mirrors the ingest filter
(UID + subteam + whole-thread retention).

## DM / MPIM handling

- `is_im=true` → hard-skip always (1:1 DMs)
- `is_mpim=true` → skip unless yaml row has `allow_mpim: true` (owner consent gate)
- For one-shot MPIM ingest without cron tracking: `ingest/slack_mpim_oneshot.py`

## MCP-era fallback

If the API path breaks, the old MCP-skill is preserved at
`.claude/commands/slack-ingest-mcp.md`. Invoke via `/slack-ingest-mcp`.

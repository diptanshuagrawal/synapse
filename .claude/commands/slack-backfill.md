One-time Slack channel backfill via direct Slack Web API. Owner-invoked.

## Usage — `/slack-backfill <channel> [flags]`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run anything.

**What it does:** One-time Slack channel backfill via the direct Slack Web API — reads history from `oldest_ts` forward, writes top-level + replies, advances cursor.

**Notes:** bounded by a `--days` window (default 365), NOT the last cursor; writes top-level + replies; runs `build_thread_summary` post-fire; advances the cursor so a later `/slack-ingest` resumes from there.

**Usage:** `/slack-backfill <channel> [flags]`
- `channel` (required) — first positional; optional flags after (e.g. `--days`).

Reads channel history from `oldest_ts` forward, writes top-level + replies to
`events.db`, advances cursor (so subsequent `/slack-ingest` resumes from there),
runs `build_thread_summary` post-fire.

Different from `/slack-ingest`:
- Bounded by `--days` window (default 365), not last cursor
- No 10-page cap — fully drains the window
- One channel at a time (no all-channels-at-once mode by design)
- Resume-safe — re-running on partial state continues from where it stopped

## What this skill does

```bash
cd $HOME/context/work-context
.venv/bin/python ingest/slack_backfill_app.py $ARGUMENTS
```

`$ARGUMENTS` — first positional is `channel`, optional flags after.

| Form | Meaning |
|---|---|
| `<channel-name>` | one channel, default 365-day window |
| `<channel-id>` | same, by C-id |
| `<channel> --days 30` | 30-day window |
| `<channel> --days all` | full history since channel.created |
| `<channel> --dry-run` | parse + count, no DB write |
| `<channel> --no-threads` | skip Phase 2 reply fetch (top-level only) |
| `<channel> --cursor-mode resume|fresh|force` | resume = continue (default); fresh = refuse if cursor exists; force = overwrite |

`compaction_policy: never` channels: pass `--days all` for full history (yaml flag doesn't auto-override default).

## Output (per channel)

```
{"channel": "<name>", "id": "C…", "days": "365", "oldest_ts": "<float>",
 "cursor_mode": "resume", "existing_cursor": "<float or null>",
 "dry_run": false, "keep_bot_messages": false}

[users] cached N users in Ns
[subteams] cached N in Ns

[history] paginating from oldest=<ts>...
  page 1: 200 msgs (cursor=<next>)
  ...

[threads] N parents-with-replies → fetching...
  threads: 50/N
  ...

[summary] thread_summary refreshed for channel

Final: top=M repl=R inserted=I cursor_advanced_to=<ts>
```

## Exit codes

```
0  success
1  env/auth/config error / refused (cursor already set without --cursor-mode override)
2  channel resolve / DM-skip / API error
```

## `ingest_mode: team_involved` channels

For channels yaml-flagged `ingest_mode: team_involved` (org-wide oncall,
opsgenie alerts, cross-team announcements), the backfill keeps only threads
where the team participates. A thread is team-involved if ANY of:

1. Root or reply author is on `management/context/team.md`
2. Root or reply body @-mentions a team UID (`<@U…>`)
3. Root or reply body pings a team subteam handle (`<!subteam^S…>`)
4. Root is bot-authored (PagerDuty / OpsGenie / Dweep "Alert Incident
   Commander") AND any reply satisfies 1–3 above

The bot-root retention is critical: incident-alert templates are written
by workflow bots but the team triages in replies, so the alert header IS
the incident and must be kept alongside the team replies.

**Subteam ids** are loaded from `config/team_subteams.yaml` (e.g.
`S0EXAMPLE` for `service-c-transaction-team-devs` / `EX-team`).
Add new subteam handles there as the team is invited into new pinging
patterns. The bot token typically lacks `usergroups:read` scope so
manual yaml entries are expected.

To re-backfill a channel after flipping it to `team_involved` (or after
extending the subteam list), pass `--cursor-mode force` to re-fetch the
window. Then optionally run `python -m derive.slack_team_filter_cleanup
--apply` to purge any historical rows that no longer satisfy the filter.

## DM / MPIM handling

- `is_im=true` → refused
- `is_mpim=true` → refused unless yaml row has `allow_mpim: true`
- For ad-hoc MPIM not in yaml: use `ingest/slack_mpim_oneshot.py` (script-only, no skill)

## Cost guidance

Per channel: ~5–15 min wall (API path far faster than MCP-era 10–30 min).
Tier-3 rate limit (~45 req/min) gates large channels. Owner pays the cost.

## After backfill

Steady-state `/slack-ingest` (cron-fired every :00/:30 IST 12–22) picks up
from advanced cursor automatically. Verify:

```bash
.venv/bin/python derive/slack_validate.py --channel <id>
```

## Implementation reference

| Component | File |
|---|---|
| Main app | `ingest/slack_backfill_app.py` |
| Slack API client | `ingest/slack_api_client.py` |
| Cursor helpers | `read_cursor`, `write_cursor` in app |
| Stale-thread derivation | `derive/slack_backfill_helper.py` |
| Upsert | `derive/slack_upsert.py` |

## MCP-era fallback

If API path breaks, old MCP-skill preserved at
`.claude/commands/slack-backfill-mcp.md`. Invoke via `/slack-backfill-mcp`.

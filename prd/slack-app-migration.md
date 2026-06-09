# Slack App-Token Migration (Hybrid Fetch + Reason)

**Status:** Shipped
**Designed:** 2026-05-15
**Shipped:** 2026-05-20
**Owner:** owner@example.com
**Driver:** MCP-based ingest hit two hard quirks (page-1 token cap, 1000-reply
thread cap) and burned Claude-turn time on mechanical fetches. Admin approval
for Slack app token in hand → built direct-API fetch path; retained MCP for
exploration only.

## Outcome (what shipped)

Two-layer architecture, fully operational:

| Layer | Driver | Stack | Purpose |
|---|---|---|---|
| **Fetch** | launchd cron + python scripts | Slack Web API via `xoxp-` user token | Bulk paginate, upsert into `events.db`. No LLM. |
| **Reason** | Chat / Claude turn | Slack MCP | Exploration, classification, compaction. With LLM. |

Reuses the chat-only-classification policy (`project_chat_only_classification.md`)
as the architectural seam.

## Non-goals (held)

- MCP not removed. Retained for exploration + as `/slack-{ingest,backfill}-mcp`
  fallback skills.
- `events.db` schema mostly preserved. Additive migrations only:
  `files_json` column (in `ingest/common.py`).
- Zero LLM calls in fetch scripts. Scripts fail-loud on accidental
  `ANTHROPIC_API_KEY` presence (mirrors rollup-auth-strip rule).

## Quirks the migration solved

1. **Page-1 oversize:** MCP capped responses at ~58k chars. API path has no
   equivalent cap — `conversations.history` returns full pages cleanly with
   `limit=200`. **Resolved.**
2. **>1000-reply thread cap:** MCP `slack_read_thread` returned ≤1000 replies
   and offered no cursor. `conversations.replies` returns
   `response_metadata.next_cursor` — full pagination trivial.
   example-dr-drill long thread spot-checked at 100% parity. **Resolved.**
3. **Per-turn batch-8 ceiling:** MCP ran only during a live Claude turn, so a
   5000-thread backfill cost ~7hr wall-clock + $X LLM tokens. API script
   runs headless via launchd in ~5min/fire at $0 LLM cost.
4. **Blocks user:** during MCP backfill, owner's session was owned by the firing
   loop. Cron-fired API path runs in background — user free.

## Token + auth (as deployed)

- **Type:** User OAuth Token (`xoxp-...`). User-token chosen over bot-token to
  inherit owner's existing channel membership — no per-channel `/invite`,
  private channels (`tmp-service-c-dr-drill`, MPIMs) accessible by default.
  Trade: token tied to a human identity; rotate if owner offboards
  (`runbook/slack-token-rotate.md`).
- **Scopes:** `channels:history`, `groups:history`, `im:history`,
  `mpim:history`, `users:read`, `users:read.email`, `channels:read`,
  `groups:read`. `usergroups:read` optional (subteams cache returns empty if
  absent).
- **Storage:** `~/context/.env` (gitignored) under `SLACK_USER_TOKEN=...`.
  Loaded via `_load_env()` in `ingest/slack_api_client.py`, never imported
  by skills.
- **Fail-loud guard:** every script asserts `SLACK_USER_TOKEN` present and
  starts with `xoxp-`; refuses to run if `ANTHROPIC_API_KEY` is present in
  same env.

## Final architecture (file inventory)

```
work-context/
├── ingest/
│   ├── slack_api_client.py        # SlackClient + ParsedMessage adapter,
│   │                                tier-3 rate-limit, cursor pagination,
│   │                                users/subteams cache (disk-persisted, 24h TTL).
│   ├── slack_backfill_app.py      # One-time channel backfill, --days window
│   ├── slack_ingest_app.py        # Steady-state cursor-bound ingest +
│   │                                Phase 2.5 stale-thread reconcile +
│   │                                Phase 2.7 trailing-window (24h) edit/delete +
│   │                                Phase 2.7b reply-edit reconcile.
│   ├── slack_mpim_oneshot.py      # Explicit-consent MPIM ingest (bypass
│   │                                yaml + is_mpim hard-skip via --confirm-mpim).
│   ├── run-slack.sh               # Launchd wrapper. No daily-success gate
│   │                                (slack volume justifies every-fire).
│   │                                Refreshes state/last_slack_validate.json.
│   └── common.py                  # Schema migrations (files_json column added)
├── derive/
│   ├── slack_upsert.py            # Upsert + reconcile_window helper.
│   │                                _url() now emits browser-clickable
│   │                                https://example.slack.com/... permalinks
│   │                                (reply-form appends ?thread_ts=&cid=).
│   │                                files_json column write on insert+update.
│   ├── slack_validate.py          # Layered sanity checks per channel
│   │                                (counts, drift, cursor lag, orphans,
│   │                                raw mentions, bot leaks, dup ts).
│   ├── slack_backfill_helper.py   # drain-channel / drain-threads / status
│   │                                helpers. _c<hash> filename parser bug fixed.
│   ├── slack_expand_mentions.py   # One-shot retro-fill of legacy <@U…>
│   │                                bodies to <@U…|Name> form.
│   ├── slack_backfill_files.py    # One-shot retro-fill of files_json column.
│   ├── slack_team.py              # team.md → slack_id resolver +
│   │                                is_team_involved(actor_id, body, ids)
│   │                                shared by ingest_app, backfill_app, cleanup.
│   ├── slack_discover_channels.py # Scans users.conversations, scores by team
│   │                                activity, decides ingest_mode per channel.
│   │                                Universal activity floor. --auto-mode +
│   │                                --apply + --json-out for cron-status.
│   ├── slack_prune_stale_mpims.py # Removes yaml + cursor rows for MPIMs
│   │                                with last event >30d old. Dry-run default.
│   │                                Preserves events.db rows.
│   └── slack_team_filter_cleanup.py # One-shot retro purge of pre-filter rows
│                                    in team_involved channels (~16k removed).
├── launchagents/
│   └── com.example.slack-ingest.plist  # Every :00/:30 IST hours 12-22.
├── bin/
│   └── cron-status.sh             # Slack flows through main per-source loop;
│                                    extras (channels w/ laggiest age, DM-skip,
│                                    validate findings + cache age) below.
├── config/
│   └── slack_channels.yaml        # 78 channels (manual + auto-discovered);
│                                    rows carry optional `ingest_mode: team_involved`
│                                    + `allow_mpim: true` for MPIM rows.
├── state/
│   ├── slack_cursors.json         # Per-channel cursor (Slack-epoch float string).
│   ├── slack_users_cache.json     # ~6700 users, 212KB, 24h TTL → 24s cold / 0s warm.
│   ├── last_slack_success.date    # YYYY-MM-DD written on any-channel success.
│   └── last_slack_validate.json   # Refreshed each fire; consumed by cron-status.
└── .claude/commands/
    ├── slack-ingest.md            # Thin wrapper (79 lines) → ingest_app.py
    ├── slack-backfill.md          # Thin wrapper (98 lines) → backfill_app.py
    ├── slack-ingest-mcp.md        # Verbatim MCP-era copy (190 lines) — fallback
    └── slack-backfill-mcp.md      # Verbatim MCP-era copy (272 lines) — fallback
```

## Phase breakdown (all complete)

| Phase | What | Status |
|---|---|---|
| 1 | Token + verify membership across 11 channels | ✅ |
| 2 | `slack_api_client.py` + parity-test against MCP on `tmp-service-c-dr-drill` | ✅ |
| 3 | `slack_backfill_app.py` + backfill 11 channels (one-shot retro) | ✅ |
| 4 | Long-thread pagination proof (example-dr-drill long thread, 100% parity) | ✅ |
| 5a | `slack_ingest_app.py` + launchd cron + wrapper | ✅ |
| 5b | Skill thin-wrappers, MCP-era preserved as `*-mcp.md` fallback | ✅ |
| Tier 2 | Validator + cron-status integration | ✅ |
| Tier 2 | Phase 2.7 trailing-window edit/delete reconcile (top-level) | ✅ |
| Tier 2 | Phase 2.7b reply-edit reconcile (closes top-level-only gap) | ✅ |
| Tier 2 | `files_json` column (schema + retro-fill ~170 rows) | ✅ |
| Tier 2 | Permalink workspace prefix (~43k rows + reply query string) | ✅ |
| Tier 2 | MPIM explicit-consent gate (`allow_mpim: true` flag) | ✅ |
| Tier 2 | Users-cache disk persist (24s cold → 0s warm) | ✅ |
| Tier 2 | Retro-fill mention expansion (~950 rows) | ✅ |
| Tier 3 | `ingest_mode` per channel (`full` vs `team_involved`) + shared `is_team_involved` helper | ✅ |
| Tier 3 | Retro cleanup of pre-filter rows (~16k deleted from opsgenie+on-call) | ✅ |
| Tier 3 | Auto-bootstrap on null cursor (seed from now − 365d, multi-fire catch-up via PAGE_CAP) | ✅ |
| Tier 3 | `slack_discover_channels.py` — scans `users.conversations`, decides ingest_mode per channel, universal activity floor, `--auto-mode` + `--apply` + `--json-out` | ✅ |
| Tier 3 | Subteam-aware discover scoring — team-activity count includes subteam-handle pings (`is_team_involved` + `team_subteams.yaml`); surfaced 34 oncall/alert/MPIM channels stuck in needs_review under author-only scoring | ✅ |
| Tier 3 | Alert-channel discovery branch — bot-authored team-domain alert firehoses (`service-a-alerts`, `example-tracker`, …) auto-add as `full` bypassing the activity floor; token-aware domain match avoids `gl`-in-`breakglass` mis-fires | ✅ |
| Tier 3 | `slack_prune_stale_mpims.py` — drops yaml row + cursor for MPIMs quiet >30d (events.db preserved) | ✅ |
| Tier 3 | `bin/run-slack-discover.sh` + `com.example.slack-discover` LaunchAgent — runs discover with `--apply` (Wed+Fri 13:00 IST) + pre-apply yaml snapshot for rollback | ✅ |
| Tier 3 | cron-status DISCOVERY block — `N ready (full+team_involved) · K needs_review` + schedule + apply hint | ✅ |
| Tier 3 | Pruner cron hookup — housekeeping step 7 (`bin/housekeeping.sh`) runs `slack_prune_stale_mpims.py --apply` weekly (Mon 03:00 IST) | ✅ |
| Tier 4 | Team-leaves pipeline (regex prefilter → chat-classify → apply → render markdown) | ✅ |
| Tier 4 | `derive/leaves_dump.py` + `apply_leaves.py` + `render_leaves.py` + `/leaves` slash skill | ✅ |
| Tier 4 | `derive/run-leaves.sh` + `com.example.leaves` LaunchAgent — daily 04:00 IST (Phase 1: regex dump + render; Phase 2 chat via `/leaves`) | ✅ |
| Tier 4 | cron-status LEAVES block — `N pending /leaves · K active/upcoming-30d (T total)` | ✅ |

## Phase caps + limits (production)

| Const | Value | Purpose |
|---|---|---|
| `PAGE_CAP` | 10 | Pages/channel/fire for top-level history (~2000 msgs); spillover next fire |
| `STALE_CAP` | 50 | Stale-thread parents/channel/fire (Phase 2.5) |
| `LIMIT` | 200 | API page size |
| `RECONCILE_LOOKBACK_HOURS` | 24 | Trailing-window edit/delete reconcile (Phase 2.7) |
| `RECONCILE_PAGE_CAP` | 10 | Pages for Phase 2.7 history sweep |
| `RECONCILE_THREADS_CAP` | 25 | Most-recent parents for Phase 2.7b reply-edit reconcile |
| `RateLimit.max_per_min` | 45 | SlackClient internal throttle (tier-3 has 50/min limit) |
| `_USERS_CACHE_TTL_S` | 86400 | Disk cache validity for users.list |
| `BOOTSTRAP_LOOKBACK_DAYS` | 365 | Seed window when ingest hits null cursor (auto-bootstrap) |
| `MPIM_TEAM_THRESHOLD` | 3 | Min team handles in MPIM name to auto-add (discover) |
| `TEAM_RATIO_FULL_THRESHOLD` | 0.5 | `team_msgs/total_msgs` ≥ 0.5 → `auto_full`, else `auto_team_involved` |
| `min_team_msgs` (floor) | 5 | Activity floor for non-MPIM channels over 90d window (discover) |
| `min_mpim_msgs` (floor) | 1 | Activity floor for MPIMs over 90d window (discover) |
| `DEFAULT_QUIET_DAYS` | 30 | MPIM prune threshold (`slack_prune_stale_mpims.py`) |

## DM / MPIM gate

- `is_im=true` (1:1 DM) → hard-skip always in `ingest_channel` and
  `slack_backfill_app`. No override.
- `is_mpim=true` → hard-skip UNLESS yaml row has `allow_mpim: true`.
- One-shot MPIM ingest without cron tracking: `ingest/slack_mpim_oneshot.py`
  with `--confirm-mpim`. Optional `--persist-cursor` writes cursor for
  recurring (pair with yaml row + `allow_mpim: true`).

Currently 14 MPIMs in yaml (`allow_mpim: true`) — original working-group(s)
plus auto-discovered team DMs surfaced by `slack_discover_channels.py`. MPIM
hygiene maintained by
`slack_prune_stale_mpims.py` (drops rows quiet >30d; events.db rows preserved
so re-discovery cleanly re-adds).

## Ingest-mode + discovery (Tier 3)

`ingest_mode` is an optional yaml field per channel:

- `full` (default if omitted) — every message stored.
- `team_involved` — only messages where author ∈ team OR body @-mentions team
  member are upserted. Threads kept whole: any team-involved reply pulls in
  the full thread including non-team replies.

Shared logic: `derive/slack_team.py::is_team_involved(actor_id, body, ids)`.
Consumers: `slack_ingest_app.py`, `slack_backfill_app.py`,
`slack_team_filter_cleanup.py`.

**Discovery** (`slack_discover_channels.py`) automates yaml population:

1. Walks `users.conversations` for owner's membership.
2. Skips channels already in yaml + bot-noise prefixes (opsgenie-, alert-, …).
3. For each candidate, fetches last 90d of messages, counts **team-involved**
   messages via `is_team_involved` — author ∈ team OR body @-mentions a team
   member OR body pings a team subteam handle (e.g. `@service-c-txn-oncall`,
   `@service-c-incident-comms` from `config/team_subteams.yaml`). Subteam-ping
   coverage surfaces oncall/incident/alert channels where the team is paged
   via user-group handle and authors almost nothing.
4. Decision tree per candidate (top-down):
   - **team-owned alert channel** (alert-named/bot-dominated AND name carries a
     team-domain keyword) → `auto_full`, **bypasses the activity floor** —
     captures bot-authored alert streams for team systems (accounting,
     ledger-balance, txn, service-c, service-a, recon, account-freeze; deposits/withholding excluded)
   - `team_msgs < floor` (`min_mpim_msgs`/`min_team_msgs`) → `needs_review`
   - MPIM with `≥ MPIM_TEAM_THRESHOLD` team handles in name → `auto_full`
   - other MPIM → `needs_review`
   - announcement-name pattern (`general`, `tech`, …) → `auto_team_involved`
   - non-MPIM with `team_ratio ≥ TEAM_RATIO_FULL_THRESHOLD` → `auto_full`
   - else → `auto_team_involved`
5. `--apply` appends `auto_*` rows to yaml; `--json-out` writes proposals for
   cron-status DISCOVERY block. Wrapper `bin/run-slack-discover.sh` invokes
   both flags by default — auto-applying is the steady state (Wed+Fri 13:00
   IST cron). Pre-apply yaml snapshot is retained at
   `state/slack_channels.yaml.bak.<ts>` (last 4 kept) for rollback.

**Auto-bootstrap:** new yaml rows with null cursor get seeded from
`now − BOOTSTRAP_LOOKBACK_DAYS` on first ingest fire. Multi-fire catch-up
absorbs the 365d window via PAGE_CAP=10 per channel per fire. No manual
`/slack-backfill` required for typical auto-added channels.

**Pruner** (`slack_prune_stale_mpims.py`): walks MPIM yaml rows, queries
`MAX(events.ts) per channel`, removes yaml row + cursor when quiet >
`DEFAULT_QUIET_DAYS`. `events.db` rows preserved — re-discovery cleanly
re-adds with fresh bootstrap. Wired into `bin/housekeeping.sh` step 7
(Mon 03:00 IST weekly fire). GRACE-skip protects MPIMs that just got
added and haven't yet been backfilled.

## Team leaves (Tier 4)

Track direct reports' leave plans (OOO, WFH, sick, vacation, travel)
extracted from Slack mentions. Mirrors `subject_summary` chat-classify
pattern; no LLM in cron path.

**Pipeline:**

1. **Regex prefilter** (`derive/leaves_dump.py`) — pulls slack events
   from last 60d authored by direct reports (owner email excluded),
   matches against a leave-keyword regex, writes `state/pending_leaves.json`
   + `.rules.md`. Idempotent — events already in `team_leaves_processed`
   are skipped.

2. **Chat classify** (`/leaves` slash skill) — reads pending JSON +
   rules, emits per-event verdicts: `{event_id, is_leave, confidence,
   leaves: [{actor, date_start, date_end, reason}, ...]}`. One event
   can yield multiple leave rows (e.g. a "5-6 May leave, 7-8
   WFH, 11-15 WFH" plan).

3. **Apply** (`derive/apply_leaves.py`) — validates verdicts
   (`confidence ≥ 0.7`, actor ∈ team_canonical, date format,
   `date_end ≥ date_start`), upserts into `team_leaves` table, marks
   `team_leaves_processed` regardless of `is_leave` (so false
   positives don't re-emerge in next dump). Verdicts file archived as
   `verdicts.leaves.<ts>.json`.

4. **Render** (`derive/render_leaves.py`) — SQL → `derived/team-leaves.md`
   with sections Active today / Upcoming (next 30d) / Recent past (last
   14d) / Ambiguous (date TBD). Dedupes via window function on
   `(actor, date_start, date_end, reason)` so top-level + thread-context
   duplicates collapse to one visible row.

**Schema:**

```sql
CREATE TABLE team_leaves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL, actor TEXT NOT NULL,
  mentioned_at TEXT NOT NULL,
  date_start TEXT, date_end TEXT,    -- nullable for ambiguous
  reason TEXT,                       -- wfh|vacation|sick|holiday|ooo|travel|other
  channel_id TEXT, channel_name TEXT,
  body_excerpt TEXT, url TEXT,
  confidence REAL, extracted_by TEXT, classified_at TEXT
);
CREATE TABLE team_leaves_processed (
  event_id TEXT PRIMARY KEY,
  processed_at TEXT NOT NULL,
  is_leave INTEGER NOT NULL,         -- 0 = false positive, 1 = real
  confidence REAL
);
```

**Cron:** `com.example.leaves` LaunchAgent fires daily 04:00 IST.
Runs Phase 1 + Phase 4 only (regex dump + render of already-classified
rows). Phase 2 chat-classify is owner-invoked via `/leaves` in a
Claude session — preserves the chat-only-classification policy
(`ANTHROPIC_API_KEY` stripped from cron env). Pending count surfaces
in cron-status LEAVES block as `N pending /leaves` — owner sees the
backlog and fires the skill when it grows.

**Team set:** owner's direct reports (parsed from
`management/context/team.md`, resolved to slack_ids via people.yaml).
Owner email **excluded** — owner knows their own leaves; dashboard
tracks the team they manage. This differs from
`slack_team.load_team_emails()` (owner included for `is_team_involved`
ingest filter); leaves uses its own private helper to enforce the
reports-only invariant.

## Validator

`derive/slack_validate.py` — exit 0 clean, 1 findings, 2 env error.

Checks: counts, reply_drift, cursor_lag, orphan_replies (PK-based — earlier
strftime-based query had a SQLite rounding bug on high-fraction-second ts),
raw_mentions (regex-strict), bot_leaks, dup_ts (refined to same
`(ts, event_type)` — thread_broadcast legitimately creates same-ts pair),
summary_lag, success_marker. Cached per cron fire to `state/last_slack_validate.json`.

## Cost vs estimate

| Item | Estimated | Actual |
|---|---|---|
| Token + bot invites | 1h | 0.5h (user-token, no invites needed) |
| `slack_api_client.py` | 4h | 3h |
| `slack_backfill_app.py` | 4h | 4h |
| Migration of 1 fresh channel | 1h | 0.5h |
| `slack_ingest_app.py` + cron | 3h | 3h |
| Skill rewrites | 1h | 1h |
| Hook cleanup | 0.5h | — (deferred; MCP-era skills still present) |
| Tier-2 (validator + files + permalinks + MPIM gate + users-cache + Phase 2.7b) | — | ~6h |
| **Total** | **~14h** | **~18h** |

## Deferred (no urgency until drift seen)

- Weekly deep reconcile (Sun 02:00 plist, 30d window) for edits >24h old
- Reply tombstone detection in Phase 2.7b (currently edit-only)
- Per-channel cursor-lag trend dashboard
- Reactions full extraction (currently summarised in `reactions_json`)
- PII redaction (PRD v2)
- MCP-era skill removal (`*-mcp.md` files; remove after API path proves
  stable for 2-4 weeks of cron fires)
- Leaves Phase 2 autonomous-session firing — currently owner-invoked via
  `/leaves`. Optional `scheduled-tasks` MCP could fire it autonomously
  ~daily, but owner-gated is acceptable until backlog becomes a problem.

## Risk + rollback

- **Token leak:** `.env` gitignored. Rotate via Slack admin per `runbook/slack-token-rotate.md`.
- **Schema mismatch:** API path uses same `slack_upsert.py`. Verified via
  parity test on `tmp-service-c-dr-drill` before broader rollout.
- **Rate-limit:** tier-3 = 50/min; SlackClient throttles to 45/min.
  Observed: never hit 429 across 12-channel fires.
- **Private channel auth gaps:** `conversations.info` checked at fire start;
  refuses ingest on `not_in_channel`.
- **Owner offboard:** documented in `runbook/slack-token-rotate.md`.
- **Rollback:** every active skill has a `*-mcp.md` fallback. Invoke
  `/slack-ingest-mcp` or `/slack-backfill-mcp` if API path breaks.

## Decisions (resolved)

1. **Token type:** User Token (`xoxp-...`). Inherits owner's channel
   membership.
2. **MCP hook (`slack-mcp-persist.sh`):** Kept. Exploration MCP calls
   (`slack_search_*`, ad-hoc `slack_read_*`) still use it.
3. **Tenancy:** Single-tenant (example workspace). Multi-workspace ingest
   out of scope.
4. **Edit/delete coverage:** trailing 24h window via Phase 2.7+2.7b. Weekly
   deep sweep deferred until drift seen.
5. **MPIM:** opt-in per channel via `allow_mpim: true` yaml flag. Default
   skip preserves DM privacy invariant.

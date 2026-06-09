# Slack ingest — detailed PRD (v1, MCP path)

> ⚠️ **SUPERSEDED.** This is the original v1 MCP-only design. Slack ingest
> shipped on the **direct Slack Web-API path** instead — see
> [`slack-app-migration.md`](slack-app-migration.md) (designed 2026-05-15,
> shipped 2026-05-20). Current entry points: `ingest/slack_ingest_app.py` +
> `ingest/slack_backfill_app.py` (the MCP `*-mcp` commands are retained as
> legacy fallback). For run instructions see `work-context/README.md` →
> "Slack workspace + channels". Kept for design history.

**Owner:** owner · **Status:** ~~Drafted, not built~~ → Superseded by direct-API path (shipped 2026-05-20) · **Last revised:** 2026-05-13
**Parent:** [`PRD.md`](PRD.md) · **Source code (target):** `.claude/commands/slack-*.md` slash commands + `~/context/work-context/derive/slack_upsert.py` helper (to be written)

> **Auth model: Slack MCP only in v1.** Direct HTTP token paths (bot `xoxb`, user `xoxp`, browser `xoxc`) are deferred to v3 as a cost / reliability optimisation. Decision recorded 2026-05-13 — see §16.

> Companion: [`USER-JOURNEYS.md`](USER-JOURNEYS.md) — every journey marked 🔵 BLOCKED-ON-SLACK depends on this landing. Includes DR-drill visibility, on-call rotation context, incident-thread RCA reconstruction.

---

## 1. Problem

`events.db` today captures GitHub, Jira, Confluence. That misses where roughly 30-40% of high-signal engineering work actually happens at a bank running a core-banking-system rewrite:

- **Incident war-rooms** live in Slack threads. RCA Confluence docs are written days later, summarising-only — losing the in-thread decision trail.
- **DR drill coordination** is Slack-native. Handoff §5 (Alice + Bob narrative caveats) confirmed: DR drill referenced by user but invisible to `events.db` because no Slack ingest exists.
- **On-call rotation handoffs** (who picked up what at 2am) are Slack-only.
- **Year-end CMR coordination** (Bob's 17 ops tickets, Alice's withholding rollout, Eve's reversal-debug threads) all anchor on Slack threads that fan out to Jira tickets only after the fact.

Result: `/narrative` undercounts ops work, `/retro` misses incident attribution, future skills (`/quarterly-retro`, `/boss-update`) inherit the same blind spot.

This PRD specifies a polling-based Slack ingest that lands a curated set of channels into `events.db` with the same shape as existing sources, so all downstream skills consume it without bespoke logic.

---

## 2. Goals

- **G1.** Slack messages from a curated channel set appear as rows in `events.db` with `source='slack'`, fitting the existing `events` schema.
- **G2.** Ingest runs on schedule (LaunchAgent) without manual intervention. Catches up automatically after laptop sleep.
- **G3.** Edits and deletions within a 7-day reconciliation window are reflected — `events.db` doesn't drift from current Slack state for recent messages.
- **G4.** Module integration: `derive/jira_metrics.py::detect_ops_tickets` extended to scan Slack thread parents in addition to Jira titles. `/narrative` ops sections gain Slack-thread evidence automatically.
- **G5.** Storage bounded: 1-year hot window in `events`, older threads compacted to one-line summaries in `subject_summary`. `#incident-*` channels exempt from compaction (preserved full).

## 3. Non-goals (v1)

- Not real-time. 30-min polling is acceptable; Socket Mode / Events API explicitly deferred.
- Not full-workspace mirror. Only curated channels.
- Not DMs / group DMs. Privacy red line.
- Not PII redaction. Deferred to v2 (compliance flag accepted by owner; mitigated by laptop disk encryption + `.gitignore` of `*.db` backups).
- Not writes to Slack. Read-only.
- Not search. The `search.messages` user-token endpoint deferred.

---

## 4. User story + acceptance tests

```
As an EM whose team runs incidents in Slack,
I want every message in the curated channels available in events.db
within ~30 min,
so /narrative can attribute DR drill / incident / RCA work that
currently looks like "_None observed in window._".
```

**Acceptance tests:**

1. After install, `bin/cron-status.sh` shows a `SLACK` block alongside GITHUB / JIRA / CONFLUENCE — runs at :15 + :45 past hour, last success ts visible, +N new count.
2. `select count(*) from events where source='slack'` is non-zero after first run.
3. A test message posted to a curated channel appears in `events.db` within 30 min.
4. A message edited within the test channel within 24h appears with updated `body` + `edited_ts` set after the nightly reconcile pass.
5. A message deleted in Slack within 24h has `deleted_ts` set on its row after reconcile.
6. `detect_ops_tickets` on a window containing a known DR-drill Slack thread returns the thread as an OpsTicket.
7. Running `/narrative` for an EM during a known incident window produces an "Ops & incident response" section that cites Slack threads, not just Jira tickets.
8. Threads older than 365 days (non-`#incident-*` channels) appear in `subject_summary` with one-line digest, and corresponding `events` rows are deleted.

---

## 5. Auth — Slack MCP

**Decision: Slack MCP server (already provisioned in this session).**

Auth flow is owned by the MCP host (Anthropic). No tokens stored locally. No `~/.secrets/slack_*` file in our scope. MCP server presents tools to Claude sessions; tools internally hit Slack Web API with whatever credential the MCP owner provisioned.

### Available MCP tools

| MCP tool | What it replaces (vs direct API) |
|---|---|
| `mcp__<slack>__slack_search_channels` | `conversations.list` — discover channel IDs |
| `mcp__<slack>__slack_read_channel` | `conversations.history` — read channel messages |
| `mcp__<slack>__slack_read_thread` | `conversations.replies` — read thread replies |
| `mcp__<slack>__slack_read_user_profile` | `users.info` — resolve user → email/handle |
| `mcp__<slack>__slack_search_users` | `users.lookupByEmail` — reverse lookup |
| `mcp__<slack>__slack_search_public_and_private` | `search.messages` — keyword search across messages |

Write tools (`slack_send_message`, `slack_schedule_message`, canvas family) are **not used** — ingest is read-only.

### Scope implications

MCP user-scope token sees **everything the authed user can see**, including:
- Public channels (full workspace)
- Private channels you are a member of
- DMs and group DMs you participate in

**This widens scope vs the bot-invite gate.** DM hard-skip becomes load-bearing — see §12 hard constraints.

### Pros / cons vs HTTP token (deferred)

| Dimension | MCP (v1) | HTTP token (v3 backlog) |
|---|---|---|
| Setup cost | zero — MCP already provisioned | Slack app create + scopes + install + token + secrets path |
| Cron-friendly | no — must run inside Claude session | yes — pure shell + LaunchAgent |
| Per-fire $$ | yes — costs Claude turns | near-zero (just API calls) |
| Scheduled execution | via scheduled-tasks MCP / routines | LaunchAgent timer |
| Reliability | depends on MCP availability + scheduled-task fire | self-contained |
| Failure visibility | inside session transcript | cron logs + cron-status.sh |
| Offline handling | misses fires when laptop / Anthropic side offline | LaunchAgent retries on wake |

### 5.1 MCP availability + scheduled routine setup runbook

Owner-executed sequence. Vastly simpler than bot path — one-time MCP sanity check + scheduled routine creation. No tokens, no per-channel invites.

1. **Verify Slack MCP is connected**. In a fresh Claude session, run:
   ```
   /mcp
   ```
   or check the session start tool listing for `mcp__*__slack_read_channel` etc. If absent, the MCP is not connected — escalate to MCP provisioning (out of scope for this PRD).

2. **Smoke-test channel discovery**. In a session, ask Claude to invoke:
   ```
   mcp__<slack>__slack_search_channels(query="service-c")
   ```
   Should return channel objects with `id` (`C0...`) + `name` + `is_im` + `is_private` flags. Confirms read access.

3. **Run `/slack-discover` skill** (to be created in v1 build). It:
   - Calls `slack_search_channels` for each name in `config/slack_channels.yaml`
   - Filters out `is_im=true` and `is_mpim=true` defensively
   - Writes channel IDs back into the yaml, preserving class + compaction_policy
   - Refuses to write if MCP returns a DM/group-DM channel under a curated name

4. **Create scheduled routine** to fire `/slack-ingest` every 30 min during work hours. Use either:
   - `mcp__scheduled-tasks__create_scheduled_task` directly, or
   - The `anthropic-skills:schedule` skill which wraps the MCP tool

   Suggested cadence:
   ```
   cron: "*/30 6-16 * * *"  # 06:00–16:00 UTC = 11:30 IST–21:30 IST, every 30 min
   command: /slack-ingest
   ```

5. **Add nightly reconcile routine** firing at 02:00 IST:
   ```
   cron: "30 20 * * *"     # 20:30 UTC = 02:00 IST
   command: /slack-reconcile
   ```

6. **Compaction stays as shell+cron**, since it's local-only (no Slack API). LaunchAgent at `~/Library/LaunchAgents/com.example.slack-compact.plist` fires `bin/slack-compact.sh` weekly Sunday 03:00 IST.

7. **Verify cron-status** picks up the routine. `bin/cron-status.sh` extends to query the scheduled-tasks MCP for the slack routine's `last_run_ts` + `next_fire_ts`. SLACK block in cron-status shows green when the routine fired successfully today.

**Failure modes:**

| Symptom | Cause | Fix |
|---|---|---|
| `mcp__<slack>__slack_*` tools absent from session | Slack MCP not provisioned for this session | Re-check MCP install at session config level |
| `slack_read_channel` returns 0 messages but channel has activity | Channel ID typo or `is_im` channel filtered out | Verify ID via `/slack-discover` re-run |
| `/slack-ingest` routine doesn't fire | Scheduled task expired / cron string wrong | Re-create via `mcp__scheduled-tasks__list_scheduled_tasks` audit |
| MCP request rate limit hit | Bulk backfill running too fast | Backfill chunks ≤ 100 messages per call; pause 2s between channels |
| Channel disappeared from `slack_search_channels` | User left channel / archived | Surface in cron-status; remove from yaml |

---

## 6. Transport — MCP via scheduled routine

**Decision: MCP tools called from inside a Claude session, fired by scheduled-tasks routine.**

Reasoning recap: zero token-setup cost; uses MCP already provisioned. Trade-off accepted: per-fire Claude-turn cost + dependency on scheduled-task reliability.

### Execution model

```
┌─────────────────────────┐
│ scheduled-tasks MCP     │  fires every 30 min during work hours
│ (cron-equivalent)       │──┐
└─────────────────────────┘  │
                             ▼
                ┌───────────────────────────────┐
                │ /slack-ingest skill runs in   │
                │ a fresh Claude session        │
                └─────────────┬─────────────────┘
                              │ for each channel in config:
                              │   - mcp__<slack>__slack_read_channel(oldest=last_ts)
                              │   - for each thread parent w/ replies:
                              │       mcp__<slack>__slack_read_thread(thread_ts)
                              │   - filter is_im/is_mpim out (defensive)
                              │   - write UPSERT to events.db via Python helper
                              ▼
                ┌───────────────────────────────┐
                │ derive/slack_upsert.py        │
                │ (local Python; events.db SQL) │
                └───────────────────────────────┘
```

### Tool-call budget per fire

Steady-state estimate (8 channels × ~5 new messages avg per 30-min window):
- 1 `slack_read_channel` call per channel = 8 calls
- ~2 `slack_read_thread` calls per active channel = ~16 calls
- ~50 messages × `slack_read_user_profile` (cached) = 0–50 calls (cache hit rate >90% steady state)
- Total per fire: 25–75 tool calls. ~30–90s wall-clock at MCP latency.

### Slack-side rate-limit tiers (apply to MCP user-scoped token)

| Slack tier | Methods | Underlying MCP tool | Rate |
|---|---|---|---|
| Tier 3 | `conversations.history`, `conversations.replies` | `slack_read_channel`, `slack_read_thread` | 50 req/min |
| Tier 2 | `conversations.list`, `users.lookupByEmail` | `slack_search_channels`, `slack_search_users` | 20 req/min |
| Tier 1 | `search.messages` | `slack_search_public(_and_private)` | ~20 req/min |

**MCP-layer behaviour on rate-hit is unknown until first trip.** Two likely modes:
- Pass-through: Slack 429 surfaces as MCP error string → skill catches + skips channel
- Internal throttle: MCP queues/delays → skill sees slow response

Either way, §11 design holds: no in-skill retry, next routine fire IS the retry. Track first observed behaviour in §18 (operational learnings) once hit.

Backfill estimate (one-time, 1 year × 8 channels):
- Page through history at ≤ 100 messages per `slack_read_channel` call
- 1 year × ~30k msgs/channel ÷ 100 = 300 pages per channel
- 8 channels × 300 pages = 2400 calls
- + per-thread reads: ~500 threads/channel × 8 = 4000 calls
- Total: ~6400 tool calls. Spread across 4–6 chunked `/slack-backfill` runs (each chunk = N channels or N days). Owner-driven, run overnight.

### Cursor + pagination

`mcp__<slack>__slack_read_channel(channel_id, oldest=<float-secs>, latest=<float-secs>, cursor=<token>, limit=100)`

**Cursor support confirmed via live probe 2026-05-13.** Schema + response both expose cursor:

| Tool | Cursor | `limit` default | `limit` max |
|---|---|---|---|
| `slack_read_channel` | yes | 100 | 100 |
| `slack_read_thread` | yes | 100 | 1000 |
| `slack_search_channels` | yes | 20 | 20 |
| `slack_search_users` | yes | 20 | 20 |
| `slack_search_public` | yes | 20 | 20 |

`pagination_info` field in response carries `cursor: <base64-token>` for next page (or "End of results" sentinel). Standard Slack-API pattern, MCP is thin pass-through.

### Response format is text, not JSON

**Important parsing concern.** MCP returns human-readable formatted text:

```
=== Message from Ivan Example (U0EXAMPLE) at 2026-05-13 15:42:30 IST ===
Message TS: 1778667150.756969
<@U0EXAMPLE|Frank> please review...
```

`derive/slack_upsert.py` must parse this with regex. Stable fields per message block:
- `=== Message from <Name> \((U[A-Z0-9]+)\) at <human-ts> ===` → actor_id
- `Message TS: (\d+\.\d+)` → raw Slack ts (cursor + UPSERT key)
- Mention syntax `<@U…|Name>` → resolve to canonical via `state/slack_users_cache.json`
- Body = lines between Message-TS line and next `===` delimiter (or end)

Use `response_format="detailed"` always — `concise` strips the `Message TS:` line which we need.

Edit / delete signals (likely format, confirm on first occurrence):
- Edited messages: format may include `(edited)` suffix or `Edited at: ts` line — to verify on live probe
- Bot messages: actor may show as `Bot Name (B0…)` instead of `User (U0…)` — different ID prefix to detect

### Channel-types default skews to public

`slack_search_channels` defaults `channel_types=public_channel`. All 8 owner-curated channels probed are **private** — must pass `channel_types=public_channel,private_channel` on every discovery call. `/slack-discover` skill bakes this in. Same for any user-facing search the skill exposes.

### Skill cursor-advance flow

1. Read `last_success_ts` for this channel from `state/slack_cursors.json`.
2. Call `slack_read_channel(channel_id, oldest=last_success_ts)` with no cursor.
3. UPSERT each message into events.db.
4. If response has `pagination_info` with `cursor: ...`, call again with that cursor.
5. After exhausting pagination, advance `state/slack_cursors.json[channel_id] = max(msg.ts for msg in fetched)`.
6. Move to next channel.

Cursor advance commit: only after the page-set writes succeed. Partial-failure leaves cursor unchanged → next fire re-pulls same window. UPSERT semantics absorb the duplication.

### Backoff

MCP server handles Slack-side rate limits internally (returns wrapped error to caller). Slash command logic:

- On MCP tool error containing `rate_limit` / `retry_after`: skip remaining channels in this fire, advance cursors for completed channels only, report partial in cron-status.
- Routine fires every 30 min — next fire picks up where we stopped.
- No in-skill sleep / retry loop. The routine cadence IS the retry mechanism.

---

## 7. Schema delta

### `events` table extensions

Existing columns retained. Add:

```sql
ALTER TABLE events ADD COLUMN edited_ts TEXT;       -- ISO ts of latest edit; NULL if never edited
ALTER TABLE events ADD COLUMN deleted_ts TEXT;      -- ISO ts when we noticed deletion (tombstone)
ALTER TABLE events ADD COLUMN thread_ts TEXT;       -- thread parent ts (NULL if top-level message)
ALTER TABLE events ADD COLUMN reactions_json TEXT;  -- {":+1:": 5, ":eyes:": 2} or NULL
```

### Slack-specific event mapping

| Slack concept | `events` row |
|---|---|
| Top-level message | `event_type='thread_started'`, `thread_ts=NULL`, `subject='slack:<channel_id>:<ts>'` |
| Reply in a thread | `event_type='thread_reply'`, `thread_ts=<parent_ts>`, `subject='slack:<channel_id>:<parent_ts>'` (same subject as parent → thread coheres) |
| Reaction added | NOT a separate row. Captured in `reactions_json` of parent message; updated on reconcile pass. |
| Edit | UPSERT existing row by `(source, subject, event_type, ts)`; overwrite `body` + set `edited_ts` |
| Delete | UPDATE existing row to set `deleted_ts = now()`. Body preserved. |

Subject format: `slack:<channel_id>:<thread_parent_ts>` — guarantees one subject per thread. Matches the existing convention where one subject = one logical work item.

### `actor` resolution

Slack returns `user: "U01ABC"`. Look up `users.info` to get `profile.email`. Map email → canonical via `config/people.yaml::slack_id` or `email`. If no match → store raw email or `slack:U01ABC` as actor; surfaces in cross-team contributor appendix.

Cache `users.info` results in `state/slack_users_cache.json` (refreshed weekly). 50 users × 1 call/week ≈ 7 calls/week, well within `users:read` Tier-2 budget.

### Reactions capture

On every poll fire, message includes `reactions: [{"name": "+1", "count": 5, "users": [...]}]`. Serialise to `reactions_json` as `{":+1:": 5, ":eyes:": 2}`. Updated on every fetch — most recent state wins. Useful for "was this thread important based on reactions" signals (used by `detect_ops_tickets` priority scoring in v2).

### 7.1 Story-graph commitment — events linkable across sources

Every event participates in a property graph via the `event_refs` join table.
A "story" = a connected component reachable from any starting subject by walking
ref edges across sources: Jira ticket → Confluence TRD → PR → Slack threads.

**Canonical ref vocabulary** (added in Phase C1, used by `derive/slack_upsert.py`
+ `ingest/common.py::enrich_refs`):

| ref_type | ref_value canonical form | Source(s) that emit |
|---|---|---|
| `person` | canonical from `config/people.yaml` | all |
| `project` | slug from `config/projects.yaml` | all |
| `ticket` | `EX-2629` (uppercase prefix + dash + number) | all (extracted from text) |
| `page` | `EXAMPLE_PAGE_ID` (numeric ID, no prefix) | all (URL or numeric body match) |
| `pull_request` | `example-org/service-a#629` (org/repo#N) | all (NEW) |
| `slack_thread` | `slack:<channel_id>:<ts>` (matches subject form) | all (NEW) |

Subject-form mismatch: `events.subject` for confluence rows is `page:N`; the
`ref_value` for a page ref is `N` (no prefix). Story-graph walks handle this
via `derive/story_graph.py::_REF_TO_SUBJECT_EXPR` mapping table — no schema
migration needed.

### 7.2 thread_summary materialised view

Per-thread fast-lookup row built post-ingest by `derive/build_thread_summary.py`
(called from `/slack-ingest`, `/slack-backfill`, `/slack-reconcile` after their
write phase). Stored in `thread_summary` table — see migration
`derive/migrations/005_thread_summary.sql`.

Columns:
- `subject` (PK = `slack:<channel>:<parent_ts>`)
- `channel_id`, `channel_name`, `channel_class` (denormed from yaml + meta cache)
- `started_by_canonical` (parent message author resolved via people.yaml)
- `participants_json` (every distinct actor resolved → canonical; unresolved raw U-ids dropped silently, picked up on `--rebuild-all` after late-add)
- `first_ts`, `last_ts`, `msg_count`, `reply_count`
- `referenced_tickets`, `referenced_pages`, `referenced_prs`, `referenced_threads` (denorm json arrays from event_refs)
- `ops_pattern_match` (`incident`|`drill`|`rca`|`year_end`|`rollback`|NULL — title regex)
- `digest` (1-line LLM summary; NULL pre-compaction, populated at year+ compaction)
- `computed_at`

Powers "what happened on this thread" as a 1-row lookup. `/narrative` ops
section + future `/story` skill consume `thread_summary` directly.

### 7.3 Raw JSONL mirror

Matching the existing github/jira/confluence convention,
`work-context/raw/slack/YYYY/MM/DD.jsonl` stores one line per ingested message
(in addition to the SQL row). Written by `ingest/common.py::append_raw`
invoked from `derive/slack_ingest_runner.py`.

Why mirror raw:
- Disaster recovery: rebuild events.db from JSONL if migration goes wrong
- Reprocess: re-parse if `slack_upsert.py` regex evolves
- Audit: who said what at what ts, immutable
- Matches existing source convention (no Slack special-case)

Cost: ~50MB/year compressed. Trivial.

### 7.4 Storage choice rationale (SQLite + raw JSONL, NOT graph DB)

Considered alternatives during design (2026-05-13):

| Option | Why rejected |
|---|---|
| Separate `slack.db` | breaks cross-source SQL JOINs used by `/narrative` |
| Postgres | violates "one laptop two API tokens" project mantra |
| Graph DB (Neo4j etc.) | Slack threads are flat 1-deep trees, not recursive — no multi-hop benefit until 10M+ rows |
| Document store (Mongo) | per-thread atomic reads only; loses aggregation queries |
| Vector store (Chroma) | semantic search is v3+ scope; FTS5 covers v1 keyword search |

events.db (SQLite + WAL + FTS5) handles ~1.4M total rows year-3 within
mid-sized DB regime. event_refs JOIN is the graph; `thread_summary` is the
fast view; `derive/story_graph.py` is the walker.

---

## 8. Ingest flow

### 8.1 Initial backfill (one-time per channel)

Skill: `.claude/commands/slack-backfill.md` (to be written). Owner-invoked, chunked across multiple sessions.

```
# Pseudo-skill outline:
for channel in config/slack_channels.yaml (chunked, N at a time):
    # Defensive DM-skip — see §12 hard constraints
    channel_meta = mcp__<slack>__slack_search_channels(query=channel.name)
    if channel_meta.is_im or channel_meta.is_mpim:
        log("REFUSE: %s is a DM/group-DM, skipping forever", channel.name)
        continue

    oldest = (now - 365 days) for compaction='standard' channels
    oldest = channel.created_at      for compaction='never' channels

    cursor = None
    while True:
        resp = mcp__<slack>__slack_read_channel(
            channel_id=channel.id,
            oldest=oldest,
            cursor=cursor,
            limit=100,
        )
        for msg in resp.messages:
            derive.slack_upsert.upsert_event(msg, channel.id)
            if msg.thread_ts == msg.ts and msg.reply_count > 0:
                replies = mcp__<slack>__slack_read_thread(channel.id, msg.ts)
                for reply in replies:
                    derive.slack_upsert.upsert_event(reply, channel.id, thread_ts=msg.ts)
        if not resp.has_more: break
        cursor = resp.next_cursor

    update_last_success(channel.id, max(msg.ts for msg in collected))
```

`derive/slack_upsert.py` is a local Python helper invoked by the skill (via `ctx_execute` / Bash). It owns the SQL UPSERT logic so the skill stays declarative.

Backfill is **chunked** because each chunk = one Claude session = bounded turn cost. Pattern:
- Run `/slack-backfill channels=service-c-internal,opsgenie-prod-service-c days=365`
- After completion, run with next chunk
- Owner orchestrates; no auto-resume across sessions in v1

### 8.2 Steady-state ingest (every 30 min via scheduled routine)

Skill: `.claude/commands/slack-ingest.md`. Fired by scheduled-tasks routine. Same loop as backfill but uses `oldest=last_success_ts` per channel instead of fixed window.

```
# Pseudo-skill outline:
for channel in config:
    channel_meta = lookup_in_state_cache(channel.id) or mcp__<slack>__slack_search_channels(...)
    if channel_meta.is_im or channel_meta.is_mpim:
        skip + log

    last_ts = read_state('state/slack_cursors.json', channel.id) or (now - 1 day)
    cursor = None
    new_msgs = []
    while True:
        resp = mcp__<slack>__slack_read_channel(
            channel_id=channel.id, oldest=last_ts, cursor=cursor, limit=100,
        )
        new_msgs.extend(resp.messages)
        if not resp.has_more: break
        cursor = resp.next_cursor

    for msg in new_msgs:
        derive.slack_upsert.upsert_event(msg, channel.id)
        if msg.thread_ts == msg.ts and msg.reply_count > 0:
            replies = mcp__<slack>__slack_read_thread(channel.id, msg.ts)
            for reply in replies:
                derive.slack_upsert.upsert_event(reply, channel.id, thread_ts=msg.ts)

    write_state('state/slack_cursors.json', channel.id, max(new_msgs.ts))
```

UPSERT key: `(source='slack', subject, event_type, ts)`. Re-running same window is safe.

### 8.3 Edit + delete reconcile (nightly via scheduled routine, 02:00 IST)

Skill: `.claude/commands/slack-reconcile.md`. Fired by scheduled-tasks routine.

```
# Pseudo-skill outline:
window_start = now - 7 days
for channel in config:
    skip_if_dm(channel)

    # Re-fetch last 7 days via MCP
    cursor = None
    api_msgs = []
    while True:
        resp = mcp__<slack>__slack_read_channel(
            channel_id=channel.id, oldest=window_start, latest=now,
            cursor=cursor, limit=100,
        )
        api_msgs.extend(resp.messages)
        if not resp.has_more: break
        cursor = resp.next_cursor

    api_ts_set = {m.ts for m in api_msgs}

    # Pass off to local Python helper for SQL reconciliation
    derive.slack_upsert.reconcile_window(
        channel_id=channel.id,
        window_start=window_start,
        api_msgs=api_msgs,
        api_ts_set=api_ts_set,
    )
```

Helper handles:
- For each `api_msg`, lookup stored row by `(channel, ts)`. If absent → INSERT (ingest gap). If `edited.ts > stored.edited_ts` OR text differs → UPDATE body + edited_ts.
- For each stored row in window not in `api_ts_set` → set `deleted_ts = now` (tombstone, body preserved).

Edits beyond 7 days drift silently. Acceptable. Owner may run `/slack-reconcile window=30` manually on suspicion of stale edits.

### 8.4 Compaction (weekly housekeeping, Sundays 03:00 IST)

**Local-only — no MCP needed.** Compaction reads existing rows from `events.db`, calls LLM (via `~/.secrets/anthropic_api_key`) for digest, writes to `subject_summary`, deletes raw rows. Runs as a regular LaunchAgent shell script.

Script: `bin/slack-compact.sh` → `ingest/slack-compact.py`.

```
cutoff = now - 365 days
for channel in config:
    if channel.compaction_policy == 'never': continue   # #incident-* exempt

    # Find threads older than cutoff
    threads = query('''
      SELECT subject, MAX(ts) AS last_ts FROM events
      WHERE source='slack' AND channel=? AND ts < ?
      GROUP BY subject
      HAVING NOT EXISTS (
        SELECT 1 FROM subject_summary WHERE subject = events.subject AND source='slack'
      )
    ''', channel.id, cutoff)

    for thread in threads:
        # Pull all events for the thread
        parent_and_replies = query('SELECT * FROM events WHERE subject=? ORDER BY ts', thread.subject)
        # LLM digest call (~$0.005 per thread)
        digest = llm_summarise(parent_and_replies)
        insert_subject_summary(thread.subject, source='slack', summary=digest, computed_at=now)

        # Delete raw events after summary verified
        delete('DELETE FROM events WHERE subject=?', thread.subject)
```

LLM digest prompt (1-line, max 200 chars, action-first):
> "Year-end balance fix discussion: Alice flagged double-credit in an account; Eve rolled a patch; EX-2642 filed as follow-up."

### 8.5 Cron-status integration

`bin/cron-status.sh` extended with SLACK block. Data sources:
- **last_run_ts / next_fire_ts** — queried via `mcp__scheduled-tasks__list_scheduled_tasks` (only resolvable from inside a Claude session; cron-status.sh therefore writes a "see /cron-status skill" pointer or caches the value to `state/slack_routine_status.json` updated by each `/slack-ingest` run)
- **event counts** — direct SQL on `events.db` (same pattern as github/jira/confluence blocks)
- **last cursor advance** — from `state/slack_cursors.json`

Sample output:

```
SLACK         ● ran today  next ~12m
    schedule  routine via scheduled-tasks MCP · */30 6-16 * * * UTC
    policy    retry on next fire · no in-skill retry
    last run  14:15  (32m ago)   +47 new  0 dup
    cursor    2026-05-13 14:15 IST  (32m old)
    db total  4,231 events  thread_reply:3120  thread_started:1111
    24h       thread_reply:89  thread_started:23
    DM skip   ✓ 0 DM/group-DM channels in active config (hard filter on)
```

The "DM skip" line surfaces the hard-skip invariant in cron-status so a misconfiguration is visible at every health check.

---

## 9. Compaction + housekeeping

### 9.1 Two-stage policy

| Stage | Trigger | Action |
|---|---|---|
| Stage 1 — noise trim | 90-day age | Delete events matching `noise` filter (config). Default: messages with `<5 chars`, `subtype='bot_message'` unless `bot_id` in `important_bots` allow-list. |
| Stage 2 — full compact | 365-day age | LLM-summarise thread → `subject_summary`. Delete events. |

Per-channel override in config:

```yaml
channels:
  - id: C01ABC
    name: oncall-service-c-txn
    compaction_policy: standard       # 90d trim + 365d compact
  - id: C02XYZ
    name: incident-payout-2026-04
    compaction_policy: never          # preserve full forever
  - id: C03DEF
    name: service-c-txn
    compaction_policy: aggressive     # 30d trim + 180d compact
```

### 9.2 Why `#incident-*` exempt by default

- Incident threads carry highest signal-per-byte: who was on, what was decided, when rollback fired, attribution chain.
- Quarterly + annual review uses cases need 1-2 year lookback to detect recurring patterns.
- Volume is naturally small — incident channels archive after closure; total events bounded.

### 9.3 Storage estimate

| Channel class | Msgs/day | Year-1 size | After Stage 1 | After Stage 2 |
|---|---|---|---|---|
| `#oncall-*` × 2 | 50/ch | 36MB | 22MB | ~4MB summaries |
| `#incident-*` × 3 (intermittent) | 200/ch lifetime | 6MB | 6MB (no compact) | 6MB |
| Team rooms × 3 | 100/ch | 110MB | 60MB | ~10MB summaries |
| **Total year-1** | — | **152MB** | **88MB** | **~20MB** |

Year-2+ steady state: ~88MB hot + ~20MB summaries = under 110MB total. Acceptable.

---

## 10. Channel config

### `config/slack_channels.yaml` (to be created)

```yaml
# Schema version
version: 1

# Default policies (applied if channel lacks override)
defaults:
  compaction_policy: standard       # 90d trim + 365d compact
  ingest_depth: full                # capture every message
  pii_redaction: none               # v1; v2 enables 'redact'

# Channels (curated; manual list owned by user)
channels:
  - id: C01ABC
    name: oncall-service-c-txn
    class: oncall
    notes: "service-c-transaction on-call rotation channel; DR drill threads land here"

  - id: C02XYZ
    name: incident-payout-2026-04
    class: incident
    compaction_policy: never
    notes: "Auto-created incident war-room for 2026-04 withholding payout regression"

  # … user fills in remainder
```

Channel discovery: `/slack-discover` skill (to be written) calls `mcp__<slack>__slack_search_channels` to look up each channel name in the yaml + populate `id` fields. Refuses to write IDs for any channel where MCP returns `is_im=true` or `is_mpim=true`.

---

## 11. Error handling + rate limits

### 11.1 MCP error response

| MCP tool result | Action |
|---|---|
| success | continue |
| error containing `rate_limit` / `retry_after` | skip remaining channels in this fire; advance cursors only for completed channels; flag in cron-status; **no in-skill retry** — next routine fire IS the retry |
| error `channel_not_found` | log; mark channel `disabled: true` in `state/slack_routine_status.json`; surface in cron-status |
| error `is_im` / `is_mpim` returned for a configured channel | **REFUSE**; do NOT ingest; surface red in cron-status with channel name |
| `mcp__<slack>__*` tool absent from session | abort routine; surface in cron-status "MCP unavailable" |
| transient timeout / network error | rely on next-fire retry |

### 11.2 Idempotency

UPSERT key on `(source='slack', subject, event_type, ts)` ensures re-running same window doesn't dupe rows. Safe to manually re-fire `/slack-ingest` or `/slack-backfill` for the same channel.

### 11.3 Cursor safety

`state/slack_cursors.json[channel_id]` only advances after the entire fetch-for-this-channel commits to db. Partial failure → cursor unchanged → next fire re-pulls same window. Bounded duplication (UPSERT-handled).

### 11.4 Edit-reconcile cost cap

If a channel has >200 edits/inserts/deletes detected in the 7-day reconcile window (paste-storm or bot misbehaviour), reconcile aborts that channel for this fire; surfaces warning in cron-status; cursor unchanged so next fire retries.

### 11.5 Rate-limit behaviour observability

First time rate-limits trip, log:
- Whether MCP returned error string (pass-through) or just slow response (internal throttle)
- Wall-clock impact on the fire
- Channel(s) that triggered

Surface in `state/slack_routine_status.json::last_rate_limit_event` and in cron-status SLACK block. After first observation, update §6 + §18 with confirmed behaviour.

### 11.6 Routine miss / Anthropic side outage

If the scheduled-tasks MCP fails to fire `/slack-ingest`:
- `state/slack_routine_status.json::last_success_ts` becomes stale
- cron-status SLACK block shows yellow / red if no run in >2h during work hours
- Owner manually invokes `/slack-ingest` in a session to catch up
- Cursors absorb the gap — no data loss as long as gap < retention window

---

## 12. Hard constraints

- **`channel_types=public_channel,private_channel` always.** `slack_search_channels` defaults to public-only; without override, private channels return empty. `/slack-discover` skill encodes this default and any user-facing search wrapper must too.
- **No DMs / group DMs — hard-coded skip, not config-driven.** Before every channel fetch, the skill calls `mcp__<slack>__slack_search_channels` (or uses cached metadata) and inspects `is_im` / `is_mpim`. If true, channel is REFUSED regardless of whether yaml lists it. This is defence-in-depth — yaml allow-list is the first gate; in-skill flag check is the second gate. Both must agree before any message is read.
- **`config/slack_channels.yaml` is the only source of truth for which channels are ingested.** No env-var override, no CLI flag bypass. Adding a channel = editing the yaml + re-running `/slack-discover` to populate `id`.
- **No writes to Slack.** Slack MCP `slack_send_message` / `slack_schedule_message` / `slack_create_canvas` / `slack_update_canvas` are NEVER invoked by ingest skills. Enforce by code review on slash command bodies; lint rule: any skill matching `slack-*` must not contain a write-MCP reference.
- **Cursors are per-channel.** Cross-channel failure doesn't reset everyone. `state/slack_cursors.json` is the canonical store.
- **Deletion is tombstone, not hard delete.** `events.deleted_ts` set; row preserved. Narrative / retro skills filter `deleted_ts IS NOT NULL` when computing activity counts but may cite tombstoned content in audit-trail queries.
- **Routine = scheduled-tasks MCP entries only.** Slash commands `/slack-ingest`, `/slack-reconcile`, `/slack-backfill` are NEVER fired by LaunchAgent or system cron. The routine mechanism is the only auto-fire path.
- **No retry inside the skill.** On error, the next routine fire is the retry. Keeps skills simple, makes cron-status the single observability surface.

---

## 13. Performance budget

Targets for v1:

| Metric | Target |
|---|---|
| Backfill wall-clock (8 channels × 1yr) | 1–2 hours (estimated; MCP latency dominant, revise after first run) |
| Steady-state poll fire | < 30s wall-clock |
| Reconcile pass (nightly) | < 5 min |
| Compaction pass (weekly) | < 15 min (≤ 500 thread digests at $0.005 each = $2.50/wk) |
| DB growth (steady state, post-compaction) | < 110 MB year-2 |
| `events.db` total bound | < 500 MB year-3 |

LLM digest budget for compaction:
- ~500 threads/wk × $0.005 = $2.50/wk = $130/yr
- Single-call summarisation (Claude Haiku or Sonnet)
- Cache by content hash — re-summarisation skipped if upstream events haven't changed

---

## 14. Roadmap

### v1 (this PRD — landing, MCP path)

**Phase A — Foundation** (2026-05-13, commit 6f7ac0d):
- [x] Slack MCP availability confirmed in session (`mcp__*__slack_read_channel` present)
- [x] Schema migration `004_slack_columns.sql` — `channel_id`, `thread_ts`, `edited_ts`, `deleted_ts`, `reactions_json` + 3 indexes
- [x] `derive/slack_upsert.py` — parser + UPSERT + reconcile (8/8 self-tests pass)
- [x] `ingest/common.py::_ensure_schema` lazy migration hook

**Phase B — Discovery** (2026-05-13, commit 387dd9c):
- [x] `.claude/commands/slack-discover.md` — looks up channel IDs via MCP + DM-skip filter
- [x] `config/slack_channels.yaml` IDs populated (8 channels, workspace=example)
- [x] `state/slack_channel_meta.json` cache written (creator, created, permalink, …)

**Phase C1 — Story-graph refs** (2026-05-13, commit 6418ba0):
- [x] `enrich_refs` extended with PR_URL_RE, SLACK_THREAD_URL_RE, SLACK_MENTION_RE
- [x] New ref_types: `pull_request`, `slack_thread`
- [x] `Refs` dataclass + `insert_event` updated for new ref_types
- [x] `slack_upsert.py` writes events + event_refs in same transaction

**Phase C2 — Story-graph view** (2026-05-13, commit 47b9478):
- [x] Migration `005_thread_summary.sql` — materialised view + 3 indexes
- [x] `derive/build_thread_summary.py` — incremental builder + `--rebuild-all` flag
- [x] `derive/story_graph.py` — BFS walker + source-aware joins for page-prefix mismatch

**Phase C3 — Ingest skills** (2026-05-13, commit b5121a1):
- [x] `derive/slack_ingest_runner.py` — local helper (upsert, cursor, status, record-fire)
- [x] `.claude/commands/slack-ingest.md` — steady-state ingest skill
- [x] `.claude/commands/slack-backfill.md` — chunked one-time backfill skill
- [x] `.claude/commands/slack-reconcile.md` — nightly edit/delete reconcile skill

**Phase C4 — Backfill utility + docs** (this commit):
- [x] `bin/refresh-event-refs.py` — re-run enrich_refs on existing rows; backfills event_refs for new ref_types + late-added people. Dry-run shows ~327k refs to add across 44k existing rows.
- [x] PRD §7 story-graph commitment + §7.1-7.4 sub-sections (this update)
- [x] §19 People-yaml late-add cookbook (this update)
- [x] §18 Operational learnings — Phase C build entry

**Remaining for v1 launch** (gated on owner-side actions):
- [ ] Run `/slack-backfill` for the 8 channels (owner — paid Claude turns)
- [ ] Apply `bin/refresh-event-refs.py` to backfill historical event_refs (decision pending — large mutation, see §19)
- [ ] `ingest/slack-compact.py` + `bin/slack-compact.sh` — weekly housekeeping (local-only, no MCP). Phase D — not started.
- [ ] LaunchAgent for compaction (`com.example.slack-compact.plist`)
- [ ] Scheduled-tasks routine for `/slack-ingest` (every 30 min, work hours)
- [ ] Scheduled-tasks routine for `/slack-reconcile` (nightly 02:00 IST)
- [ ] `bin/cron-status.sh` — SLACK block reads `state/slack_routine_status.json`. Phase E — not started.
- [ ] `derive/jira_metrics.py::detect_ops_tickets` extended to scan Slack thread parents (in addition to current Jira-title scan). Phase E.
- [ ] `prd/PRD.md` §13 Story-index row updated to "Live" — done after backfill + routines fire successfully

### v2 — PII redaction

- [ ] `derive/slack_upsert.py` extended with PAN / phone / IFSC / long-account-id regex strip before write
- [ ] Backfill redaction pass over existing v1 raw data (run once after deploy)
- [ ] `config/slack_channels.yaml` per-channel `pii_redaction: redact` flag honoured

### v3 — Direct HTTP token path (cost / reliability optimisation)

**Trigger conditions to consider this:**
- MCP path costs >$X/month in Claude turns and steady-state ingest is the dominant cost
- Scheduled-tasks MCP misses fires >5% of the time → ingest staleness becomes business problem
- Workspace admin approves the OAuth-app install path

**Scope when triggered:**
- Add `ingest/slack.py` HTTP variant (uses `xoxp-` user OAuth token or browser `xoxc-`)
- LaunchAgent-driven; bypasses MCP entirely
- Same schema, same compaction, same DM hard-skip
- Switchover gate: `config/slack_channels.yaml::transport: http|mcp` per-channel or global
- MCP code path stays as fallback

### v4 — Search-augmented

- [ ] Use `mcp__<slack>__slack_search_public_and_private` for ad-hoc keyword sweeps when adding new channels mid-cycle (find threads not currently indexed)
- [ ] Consider as input to `detect_ops_tickets` pattern extension

---

## 15. Test plan

### Unit (`derive/slack_upsert.py`)

- `normalize_message` — Slack message JSON → `event` row mapping. Cases: top-level message, thread parent, thread reply, edited message, bot_message, message with reactions, message with attachments.
- `upsert_event` — idempotent re-run on identical input produces no schema change. Re-run with newer `edited.ts` triggers UPDATE.
- `reconcile_window::detect_edits` — given stored row + API response with newer `edited.ts`, emits UPDATE.
- `reconcile_window::detect_deletions` — given stored row absent from API window, sets `deleted_ts`.
- `is_dm_channel(channel_meta)` — returns True iff `is_im=true` OR `is_mpim=true`. Skill calls this as gate.

### Integration

- Fresh DB → `/slack-backfill channels=<test-channel> days=1` → row count matches Slack UI count.
- After manual edit-in-Slack → next `/slack-reconcile` updates `body` + `edited_ts`.
- After manual delete-in-Slack → next `/slack-reconcile` sets `deleted_ts`.
- Manually point `/slack-discover` at a DM channel name → it REFUSES to write the ID + surfaces error.
- `/narrative` for a window containing test threads cites them in Ops section.

### Smoke (post-deploy)

- `bin/cron-status.sh` SLACK block green within 30 min of first routine fire.
- `state/slack_routine_status.json::last_success_ts` advances each fire.
- `select count(*) from events where source='slack' and ts > strftime('%s','now','-1 hour')` > 0 within 30 min of channel activity.
- `select count(*) from events where source='slack' and is_im` (joined via channel metadata cache) = 0 — DM hard-skip enforced.

---

## 16. Open items / decisions deferred

| # | Item | Default chosen (this draft) | Revisit when |
|---|---|---|---|
| 1 | Channel list | 8 channels provided 2026-05-13; IDs `TODO` in yaml until `/slack-discover` runs | first build of `/slack-discover` skill |
| 2 | Opsgenie channel compaction | `never` (canonical incident timeline preserved forever) + `keep_bot_messages: true` | year-2 if DB growth surprises |
| 3 | LLM model for digest | Claude Haiku (cheapest) | if digest quality complaints |
| 4 | Reconcile window | 7 days | if edits beyond 7d become common pattern |
| 5 | Routine cadence | 30 min during work hours (06:00–16:00 UTC = 11:30–21:30 IST) | if off-hours coverage needed |
| 6 | Storage cap | 500 MB year-3 | when DB approaches limit |
| 7 | Direct HTTP token path | deferred to v3 | when MCP cost or reliability becomes painful |
| 8 | scheduled-tasks MCP reliability | assume good for v1 | if missed fires >5% during first 30 days |
| 9 | Per-fire $$ tracking | track via `state/slack_routine_status.json::cost_estimate` after each run | once first month's data lands |

---

## 17. References

- Slack MCP tools list (see SessionStart `<system-reminder>` deferred-tools dump for current MCP function set)
- Slack Web API endpoint reference (the MCP tools wrap these):
  - `conversations.history` — https://api.slack.com/methods/conversations.history
  - `conversations.replies` — https://api.slack.com/methods/conversations.replies
  - `conversations.list` — https://api.slack.com/methods/conversations.list
- Slack message-edit semantics — https://api.slack.com/events/message/message_changed
- scheduled-tasks MCP for routine fire — `mcp__scheduled-tasks__create_scheduled_task`
- Existing ingest patterns (shape this PRD follows — but `ingest/` Python scripts are replaced by `.claude/commands/` skills in v1): `ingest/jira.py`, `ingest/github.py`, `ingest/confluence.py`
- Handoff trail: `work-context/handoff-2026-05-12-2239.md` §7 "Open backlog — Slack ingest strategy decision"
- Parent PRD: [`PRD.md`](PRD.md) (§13 Story index)
- USER-JOURNEYS impact: [`USER-JOURNEYS.md`](USER-JOURNEYS.md) — all 🔵 BLOCKED-ON-SLACK markers
- Module integration target: `derive/jira_metrics.py::detect_ops_tickets` + `OPS_PATTERNS`
- Session pivot rationale (MCP over bot): chat 2026-05-13 — owner chose MCP-only to skip Slack app provisioning + workspace admin approval cost

---

## 18. Operational learnings (log as observed)

Section appended-to as we hit real-world behaviour that differs from this PRD's assumptions. Each entry: date · observed behaviour · PRD assumption · resolution.

### 2026-05-13 — initial schema + live probe

- **MCP user identity confirmed**: `owner@example.com` workspace=`example`, user_id=`U0EXAMPLE`. MCP authed as owner's Slack account (user scope).
- **Cursor pagination works as designed**: `slack_read_channel` returned `pagination_info: cursor: bmV4dF90czoxNzc4...` on first page of `service-c-internal`. Confirms PRD §6 design.
- **Response format is human-readable text, NOT JSON.** `=== Message from <Name> (U…) at <human-ts> ===` block delimiter + `Message TS: <raw-ts>` line. `response_format=detailed` required to get raw ts. PRD §6 updated.
- **Default `channel_types` is public-only.** All 8 curated channels are private — discovery utility must always pass `public_channel,private_channel`. PRD §12 updated.
- **Rate-limit behaviour NOT observed yet** — assumption still: pass-through of Slack 429 as MCP error string. To revisit on first trip.
- **Edit / delete signal format** — not observed yet on a known-edited message. Verify on first occurrence + log here.
- **Bot-message ID prefix** — likely `B0…` vs human `U0…`. Verify on opsgenie channel ingest + log here.

### 2026-05-13 — Phase G — first Slack backfill landed (service-c-internal, 30d)

- **Channel:** C0EXAMPLE (service-c-internal, private, 'team', standard compaction)
- **Window:** 30 days (2026-04-13 → 2026-05-13). 365d deferred to dedicated session per cost concern.
- **Page 1 only** — owner-decision to stop here to avoid context bloat in this session. Routine takes over from advanced cursor.
- **Top-level messages:** 94 thread_started events inserted. **Thread replies SKIPPED** — reconcile routine (nightly 02:09 IST, 7-day window) catches replies for recent threads.
- **event_refs populated:** 102 person + 20 project + 3 slack_thread (cross-thread links) + 3 page + 2 pull_request + 1 ticket.
- **thread_summary:** 94 threads built. **5 ops-pattern hits**: 3 incidents, 1 drill ("VendorX dr drill got extended"), 1 year_end (Alice withholding `example_tds_table` table).
- **Cron-status SLACK block:** green, "ran today", surfaces +94 new, 1 active cursor, DM-skip ✓.
- **Cursor at:** newest message ts = 1778667150.756969 (2026-05-13 15:42 IST). Next scheduled-tasks /slack-ingest fire (2026-05-14 12:06 IST) picks up from here for new messages.

**Confirmed working (live data):**
- MCP response parsing (regex over `=== Message from <Name> (U…) at <ts> ===` blocks + `Message TS:` line)
- `<@U…>` mention resolution via `slack_users_cache` built from `people.yaml::slack_id` (caught Alice, Frank, Eve, Bob, owner, Dan, Grace, Carol, Ivan mentions)
- `[Epic EX-N]` jira_epics matching for project refs (kept noise low)
- Slack thread permalink → cross-thread ref (`slack:<C>:<ts>`) extraction working
- DM hard-skip held (no DM channels in config, no DM messages ingested)
- thread_summary `ops_pattern_match` regex (incident/drill/year_end detected correctly)
- `started_by_canonical` resolved for all 94 threads (no raw U-ids leaked)

**Still NOT observed live:**
- Edit signal format (no edits in 30d window — verify next time someone edits a message)
- Bot-message ID prefix is `B0…` (confirmed — `B0EXAMPLE` for example-monthly-update Standup, `B0EXAMPLE` for Weekly-Oncall-Review)
- Deletion signal (no deletes observed)
- Rate-limit behaviour (single fire well under tier-3 limit)
- /slack-reconcile routine (next fire 02:09 IST tomorrow)
- /slack-ingest routine (next fire 12:06 IST tomorrow)

**Deferred for follow-up session:**
- Full 365d backfill (cost: ~$1-2 in Claude turns + many tool calls)
- Thread reply backfill for the 25 threads with `reply_count > 0` in this window (reconcile will catch last 7d; older threads need explicit fetch)
- Remaining 7 channels (service-c-public, service-c-public, service-c-core-team, example-migration-wg, opsgenie-prod-service-c, service-c-oncall, on-call) — never backfilled

### 2026-05-13 — Phase A-C built end-to-end (commits 6f7ac0d → b5121a1)

- **Schema** + helper (Phase A): migration 004 applied to live events.db; `slack_upsert.py` 380 lines, 8 self-tests pass. 43,921 existing rows untouched (NULL in new columns).
- **Discovery** (Phase B): all 8 channels resolved via MCP `slack_search_channels`. 0 DM violations, 0 archived, 0 not-found. Channel-types-public-only default trap confirmed (all 8 are private).
- **Story-graph extraction** (Phase C1): `enrich_refs` extended with 3 new regex patterns (PR URLs, Slack thread permalinks, `<@U…>` mentions). 9th self-test added covering all 6 ref_types extracted from one Slack body.
- **thread_summary view + walker** (Phase C2): migration 005 applied; live `derive/story_graph.py` walk from `EX-2238` returned 14 child Jira tickets at hop=1 + service-a#517 (Alice withholding PR) at hop=2. Confirms graph works end-to-end on existing data, even WITHOUT Slack data yet.
- **3 ingest skills + runner** (Phase C3): smoke-test of runner against live MCP probe response: 1 msg parsed → inserted → cursor advanced → cleanup confirmed clean.
- **Ref-backfill utility** (Phase C4): `bin/refresh-event-refs.py` dry-run shows ~327k missing refs across existing 44k rows (mostly project keyword matches that postdate older ingest runs, plus newly-introduced ref_types pull_request + slack_thread). Owner-decision when to apply.

**Still NOT observed live** (will log when first triggered):
- MCP rate-limit behaviour under sustained backfill
- Slack edit / delete signal format in MCP text response
- Bot-message ID prefix variation (opsgenie channel ingest)
- Backfill wall-clock vs §13 budget estimates
- Compaction LLM digest quality (Phase D, not built)

### Template for future entries

```
### YYYY-MM-DD — <short title>

- **Observed**: <what happened>
- **PRD assumption**: <what we expected (link to section)>
- **Delta / impact**: <how it differs>
- **Resolution**: <patch applied OR followup tracked OR no-op>
```

---

## 19. People-yaml late-add cookbook

Applies to ALL sources, not just Slack. Documented in this PRD because Slack
ingest surfaced the question, and the design discussion (this session
2026-05-13) clarified the rules.

### How attribution actually works

`events.actor` is ALWAYS the **raw source identifier** (github handle, jira
email, confluence email, slack U-id). Never canonicalised at write time.

`derive/jira_metrics.py::get_aliases_for(canonical)` reads `config/people.yaml`
at query time and returns every alias for that canonical. `/narrative`, `/retro`
queries use `WHERE actor IN (alias_list)`. So **adding a person to people.yaml
later automatically picks up their historical events at query time**, no data
migration needed for the `events.actor` column.

BUT `event_refs` IS canonicalised at write time (see `enrich_refs` in
`ingest/common.py`). Person refs from mentions / authorship resolve to canonical
the moment the row is inserted. Late-added people get NULL person refs on
historical rows.

Similarly, materialised views (`thread_summary.started_by_canonical`,
`thread_summary.participants_json`, `trd_owners.owner`, `trd_owners.scores_json`)
snapshot canonical resolution at build time.

### Cookbook — "I added a person to people.yaml"

```
# 1. Edit config/people.yaml — add the new person:
#      canonical: jane-doe
#      name: Jane Doe
#      email: jane.doe@example.com
#      github: example-janedoe
#      slack_id: U0EXAMPLE...
#      jira_id: EXAMPLE_ACCOUNT_ID
#      role: SDE2

cd ~/context/work-context

# 2. Backfill person refs in event_refs (historical mentions, authorship).
#    Restrict to the late-add target to keep work bounded:
.venv/bin/python bin/refresh-event-refs.py --missing-person

# Or scope to specific source + date window:
.venv/bin/python bin/refresh-event-refs.py --source jira --since 2026-01-01T00:00:00Z

# 3. Refresh Confluence TRD ownership snapshots:
.venv/bin/python derive/build_trd_owners.py --rebuild-all

# 4. Refresh Slack thread_summary snapshots (started_by + participants):
.venv/bin/python derive/build_thread_summary.py --rebuild-all

# 5. Refresh subject_summary (if person added changes domain attribution
#    for any subject). Usually not needed; skip unless a /narrative run
#    surfaces stale domain claims.
/rollup       # Re-classifies pending subjects via chat-driven classifier

# 6. Verify: run /narrative for the new person to confirm events surface
/narrative jane-doe 60
```

### What does NOT need to happen

- **No re-ingest from any source.** github / jira / confluence / slack rows
  stay where they are.
- **No `events.actor` column migration.** Already raw, already
  alias-resolvable at query time.
- **No re-fetch of Slack history.** /slack-discover doesn't need to re-run.
- **No `event_refs` DELETE.** `refresh-event-refs.py` uses INSERT OR IGNORE —
  additive only, idempotent.

### Edge cases

| Scenario | Procedure |
|---|---|
| Person's `slack_id` was wrong in people.yaml (typo) | Fix yaml, run steps 2 + 4 |
| Person had different github handle in past | Add both as aliases (e.g. `github: example-handle-current`, `github_alt: handle-old`). `get_aliases_for` returns all. Future change: extend Refs to support array fields |
| Person changed name | Update `name` field. Re-run steps 2, 3, 4 since name appears in some title extractions. |
| Person leaves the org | Do NOT remove from people.yaml. Mark `status: alumnus` (new field; ignored by current queries). Historical attribution stays intact. |
| Person was never in any tool you ingest | Their events don't exist in events.db. People.yaml entry has no effect until they appear in github/jira/confluence/slack. |

### Backfill cost estimate

Full `refresh-event-refs.py` run over current 44k-row events.db (2026-05-13
dry-run):

| Sub-metric | Count |
|---|---|
| rows scanned | 43,921 |
| rows that would gain refs | 15,084 (~34%) |
| total refs to add | ~327,554 |
| by type | project: 316,326 · ticket: 7,056 · person: 3,950 · slack_thread: 127 · pull_request: 95 |
| by source | jira: 317,364 · github: 8,693 · confluence: 1,497 |
| wall-clock | ~2-3 min |

Owner-decision when to run. Large mutation but reversible per (event_id, ref_type, ref_value) PK if needed.

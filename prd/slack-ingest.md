# Slack ingest — detailed PRD (v1, MCP path)

> ⚠️ **SUPERSEDED — design history only.**
> This is the original v1 MCP-only design. Slack ingest shipped on the
> **direct Slack Web-API path** instead — see
> [`slack-app-migration.md`](slack-app-migration.md) (designed 2026-05-15,
> shipped 2026-05-20). Current entry points: `ingest/slack_ingest_app.py` +
> `ingest/slack_backfill_app.py`; the MCP `*-mcp` commands remain as legacy
> fallback. Run instructions: `work-context/README.md` → "Slack workspace + channels".
>
> **This doc survives as the canonical reference for the bits skill specs cite:**
> the ops-pattern enum (§7.2), compaction (§9 + §11.1), and open items (§16 — esp.
> the opsgenie `compaction_policy: never` decision). Section anchors are stable.
> Superseded MCP transport mechanics, the scheduled-routine runbook, pseudo-skill
> outlines, and MCP tool catalogs have been dropped.

**Owner:** owner · **Status:** Superseded by direct-API path (shipped 2026-05-20) · **Last revised:** 2026-05-13
**Parent:** [`PRD.md`](PRD.md)
**Auth model (v1):** Slack MCP only. Direct HTTP token paths (`xoxb`/`xoxp`/`xoxc`) deferred to v3. Decision 2026-05-13 — see §16.

---

## 1. Problem

`events.db` captures GitHub, Jira, Confluence — missing ~30-40% of high-signal eng work that happens in Slack:

- **Incident war-rooms** live in Slack threads; RCA Confluence docs are summaries written days later (lose the decision trail).
- **DR drill coordination** is Slack-native (confirmed invisible to `events.db` in Handoff §5).
- **On-call rotation handoffs** are Slack-only.
- **Year-end CMR coordination** anchors on Slack threads that fan out to Jira only after the fact.

Result: `/narrative` undercounts ops work, `/retro` misses incident attribution; future skills inherit the blind spot.

This PRD specifies a polling Slack ingest landing curated channels into `events.db` with the same shape as existing sources, so downstream skills consume it without bespoke logic.

---

## 2. Goals

- **G1.** Slack messages from curated channels → `events.db` rows with `source='slack'`, fitting existing schema.
- **G2.** Runs on schedule (LaunchAgent), catches up after laptop sleep.
- **G3.** Edits + deletions within a 7-day reconcile window reflected — no drift for recent messages.
- **G4.** `derive/jira_metrics.py::detect_ops_tickets` extended to scan Slack thread parents (not just Jira titles); `/narrative` ops sections gain Slack-thread evidence.
- **G5.** Storage bounded: 1-year hot window in `events`, older threads compacted to one-liners in `subject_summary`. `#incident-*` exempt.

## 3. Non-goals (v1)

- Not real-time (30-min polling; Socket Mode / Events API deferred).
- Not full-workspace mirror — curated channels only.
- No DMs / group DMs (privacy red line).
- No PII redaction (deferred v2; mitigated by disk encryption + `.gitignore` of `*.db`).
- No writes to Slack (read-only).
- No search (`search.messages` deferred).

---

## 4. User story + acceptance tests

> As an EM whose team runs incidents in Slack, I want every message in curated channels in `events.db` within ~30 min, so `/narrative` can attribute DR-drill / incident / RCA work that currently shows "_None observed in window._".

**Acceptance tests:**

1. `bin/cron-status.sh` shows a `SLACK` block (runs :15 + :45, last-success ts, +N new).
2. `select count(*) from events where source='slack'` non-zero after first run.
3. Test message in a curated channel appears within 30 min.
4. Edit within 24h → updated `body` + `edited_ts` after nightly reconcile.
5. Delete within 24h → `deleted_ts` set after reconcile.
6. `detect_ops_tickets` on a window with a known DR-drill thread returns it as an OpsTicket.
7. `/narrative` for an EM in an incident window produces an "Ops & incident response" section citing Slack threads, not just Jira.
8. Threads >365 days (non-`#incident-*`) appear in `subject_summary` with one-line digest; corresponding `events` rows deleted.

---

## 5. Auth — Slack MCP

**Decision: Slack MCP server (already provisioned).** Auth owned by the MCP host (Anthropic). No tokens stored locally. MCP tools internally hit Slack Web API.

**Read tools used:** `slack_search_channels` (discover IDs), `slack_read_channel` (history), `slack_read_thread` (replies), `slack_read_user_profile` (user→email), `slack_search_users` (reverse lookup), `slack_search_public_and_private` (keyword search, v4 only).
**Write tools** (`slack_send_message`, `slack_schedule_message`, canvas family) are **never used** — ingest is read-only.

**Scope implication:** MCP user-scope token sees everything the authed user sees — incl. DMs/group DMs. **DM hard-skip is load-bearing** — see §12.

**Why MCP over HTTP token (v1):** zero setup cost (MCP already provisioned), no Slack-app create/scopes/install/secrets. Trade-offs accepted: per-fire Claude-turn cost, must run inside a Claude session (vs LaunchAgent), depends on MCP + scheduled-task reliability, failure visibility lives in session transcript. (Full HTTP-token comparison → v3, §14.)

---

## 6. Transport — MCP via scheduled routine

**Decision: MCP tools called from inside a Claude session, fired by a scheduled-tasks routine.** Per-channel loop: `slack_read_channel(oldest=last_ts)` → for each thread parent with replies `slack_read_thread(thread_ts)` → filter `is_im`/`is_mpim` → UPSERT to `events.db` via `derive/slack_upsert.py`.

### Key transport facts (load-bearing)

- **Steady-state budget:** ~25-75 tool calls/fire (8 ch × ~5 msgs), ~30-90s wall-clock.
- **Backfill budget:** ~6400 tool calls for 1yr × 8 channels (≤100 msgs/`slack_read_channel` call; ~300 pages/channel + ~4000 thread reads). Spread across 4-6 chunked `/slack-backfill` runs, owner-driven, overnight.
- **Slack rate-limit tiers** (apply to MCP user token): Tier 3 `conversations.history`/`.replies` → 50 req/min; Tier 2 `conversations.list`/`users.lookupByEmail` → 20 req/min; Tier 1 `search.messages` → ~20 req/min.
- **Rate-hit behaviour unknown until first trip** (pass-through 429 error string vs internal throttle). Either way §11 holds: no in-skill retry, next fire IS the retry. Log first observation in §18.

### Cursor + pagination

`slack_read_channel(channel_id, oldest=<float-secs>, latest, cursor, limit=100)`. **Cursor confirmed via live probe 2026-05-13.**

| Tool | Cursor | `limit` default | max |
|---|---|---|---|
| `slack_read_channel` | yes | 100 | 100 |
| `slack_read_thread` | yes | 100 | 1000 |
| `slack_search_channels` / `_users` / `slack_search_public` | yes | 20 | 20 |

`pagination_info.cursor` (base64) carries next page, or "End of results" sentinel.

### Response format = text, NOT JSON

MCP returns human-readable blocks; `derive/slack_upsert.py` parses with regex. Stable per-message fields:
- `=== Message from <Name> \((U[A-Z0-9]+)\) at <human-ts> ===` → actor_id
- `Message TS: (\d+\.\d+)` → raw Slack ts (cursor + UPSERT key)
- Mention `<@U…|Name>` → resolve via `state/slack_users_cache.json`
- Body = lines between Message-TS line and next `===`

Always use `response_format="detailed"` (`concise` strips the `Message TS:` line). Edit/bot signals to confirm on first occurrence: edits may carry `(edited)` / `Edited at:`; bots show `B0…` prefix vs human `U0…`.

### Discovery skews public

`slack_search_channels` defaults `channel_types=public_channel`. All 8 curated channels are **private** → must pass `channel_types=public_channel,private_channel` on every discovery/search call.

### Cursor-advance flow

Read `last_success_ts` (`state/slack_cursors.json`) → `slack_read_channel(oldest=last_success_ts)` → UPSERT each → follow `pagination_info.cursor` → after pagination, advance cursor to `max(msg.ts)`. **Advance only after all page writes succeed**; partial failure leaves cursor unchanged → next fire re-pulls (UPSERT absorbs dupes).

### Backoff

On MCP error containing `rate_limit`/`retry_after`: skip remaining channels, advance cursors for completed channels only, report partial in cron-status. **No in-skill sleep/retry — the routine cadence IS the retry.**

---

## 7. Schema delta

### `events` table extensions

```sql
ALTER TABLE events ADD COLUMN edited_ts TEXT;       -- ISO ts of latest edit; NULL if never edited
ALTER TABLE events ADD COLUMN deleted_ts TEXT;      -- ISO ts when deletion noticed (tombstone)
ALTER TABLE events ADD COLUMN thread_ts TEXT;       -- thread parent ts (NULL if top-level)
ALTER TABLE events ADD COLUMN reactions_json TEXT;  -- {":+1:": 5, ":eyes:": 2} or NULL
```

### Slack → `events` row mapping

| Slack concept | `events` row |
|---|---|
| Top-level message | `event_type='thread_started'`, `thread_ts=NULL`, `subject='slack:<channel_id>:<ts>'` |
| Reply in thread | `event_type='thread_reply'`, `thread_ts=<parent_ts>`, `subject='slack:<channel_id>:<parent_ts>'` (same subject as parent) |
| Reaction added | NOT a row — captured in parent's `reactions_json`, updated on reconcile |
| Edit | UPSERT by `(source, subject, event_type, ts)`; overwrite `body` + set `edited_ts` |
| Delete | UPDATE `deleted_ts = now()`; body preserved |

Subject form `slack:<channel_id>:<thread_parent_ts>` → one subject per thread (matches one-subject = one work-item convention).

### `actor` resolution

Slack `user: "U01ABC"` → `users.info` → `profile.email` → canonical via `config/people.yaml::slack_id`|`email`. No match → store raw email / `slack:U01ABC` (surfaces in cross-team appendix). Cache `users.info` in `state/slack_users_cache.json` (weekly refresh, ~7 calls/wk).

### Reactions capture

Each poll: serialise `reactions: [{name,count,users}]` → `reactions_json` `{":+1:": 5}`; most-recent-wins. Feeds `detect_ops_tickets` priority scoring (v2).

### 7.1 Story-graph commitment — events linkable across sources

Events join via `event_refs`. A "story" = connected component reachable from any subject by walking ref edges across sources (Jira → Confluence TRD → PR → Slack threads).

**Canonical ref vocabulary** (Phase C1; used by `slack_upsert.py` + `ingest/common.py::enrich_refs`):

| ref_type | ref_value canonical form | emitted by |
|---|---|---|
| `person` | canonical from `config/people.yaml` | all |
| `project` | slug from `config/projects.yaml` | all |
| `ticket` | `EX-2629` (uppercase prefix + dash + number) | all (text) |
| `page` | `EXAMPLE_PAGE_ID` (numeric ID, no prefix) | all (URL/body) |
| `pull_request` | `example-org/service-a#629` (org/repo#N) | all (NEW) |
| `slack_thread` | `slack:<channel_id>:<ts>` (matches subject form) | all (NEW) |

Subject-form mismatch: confluence `events.subject` is `page:N` but page `ref_value` is `N`. Story-graph walks handle via `derive/story_graph.py::_REF_TO_SUBJECT_EXPR` — no migration needed.

### 7.2 `thread_summary` materialised view

Per-thread fast-lookup row, built post-ingest by `derive/build_thread_summary.py` (called from ingest/backfill/reconcile after write). Table per migration `derive/migrations/005_thread_summary.sql`.

Columns:
- `subject` (PK = `slack:<channel>:<parent_ts>`)
- `channel_id`, `channel_name`, `channel_class` (denormed from yaml + meta cache)
- `started_by_canonical` (parent author via people.yaml)
- `participants_json` (distinct actors → canonical; unresolved raw U-ids dropped silently, recovered on `--rebuild-all`)
- `first_ts`, `last_ts`, `msg_count`, `reply_count`
- `referenced_tickets`, `referenced_pages`, `referenced_prs`, `referenced_threads` (denorm json arrays from event_refs)
- **`ops_pattern_match`** (`incident` | `drill` | `rca` | `year_end` | `rollback` | `NULL` — title regex)
- `digest` (1-line LLM summary; NULL pre-compaction, populated at year+ compaction)
- `computed_at`

Powers "what happened on this thread" as a 1-row lookup. Consumed by `/narrative` ops section + future `/story` skill.

### 7.3 Raw JSONL mirror

`work-context/raw/slack/YYYY/MM/DD.jsonl` — one line/message (in addition to SQL row). Written by `ingest/common.py::append_raw` from `derive/slack_ingest_runner.py`. Enables DR rebuild, reparse on regex evolution, immutable audit. Matches existing source convention. ~50MB/yr compressed.

### 7.4 Storage choice rationale (SQLite + raw JSONL, NOT graph DB)

Rejected alternatives (2026-05-13): separate `slack.db` (breaks cross-source JOINs); Postgres (violates "one laptop two API tokens" mantra); Graph DB (threads are flat 1-deep — no multi-hop benefit until 10M+ rows); Mongo (loses aggregation); Chroma (semantic = v3+; FTS5 covers v1 keyword).

`events.db` (SQLite + WAL + FTS5) handles ~1.4M rows year-3. `event_refs` JOIN is the graph; `thread_summary` the fast view; `derive/story_graph.py` the walker.

---

## 8. Ingest flow (superseded — see slack-app-migration.md for shipped path)

Three skills, all on the MCP path. UPSERT key `(source='slack', subject, event_type, ts)` makes every re-run safe. `derive/slack_upsert.py` owns SQL so skills stay declarative.

- **8.1 Backfill** (`/slack-backfill`, owner-invoked, chunked). Per channel: DM-skip check → `oldest = now-365d` (standard) / `channel.created_at` (never) → page `slack_read_channel(limit=100)` → upsert each msg; if `thread_ts==ts and reply_count>0`, fetch+upsert replies → advance cursor to `max(ts)`. Chunked one-session-at-a-time (`/slack-backfill channels=… days=365`); no auto-resume in v1.
- **8.2 Steady-state** (`/slack-ingest`, every 30 min via routine). Same loop, `oldest=last_success_ts` per channel (fallback `now-1day`).
- **8.3 Reconcile** (`/slack-reconcile`, nightly 02:00 IST). Re-fetch last 7 days → `slack_upsert.reconcile_window(...)`: absent stored row → INSERT (ingest gap); `edited.ts > stored.edited_ts` or text differs → UPDATE body+edited_ts; stored row in window absent from API → set `deleted_ts` (tombstone). Edits >7d drift silently (acceptable; owner can run `/slack-reconcile window=30`).
- **8.4 Compaction** (Sundays 03:00 IST) — **local-only, no MCP.** `bin/slack-compact.sh` → `ingest/slack-compact.py`. Reads `events.db`, LLM-digests threads >365d (skips `compaction_policy=='never'`), writes `subject_summary`, deletes raw rows. Digest = 1-line, ≤200 chars, action-first (e.g. "Year-end balance fix: Alice flagged double-credit; Eve rolled a patch; EX-2642 filed as follow-up.").
- **8.5 Cron-status** — `bin/cron-status.sh` SLACK block. `last_run`/`next_fire` via `mcp__scheduled-tasks__list_scheduled_tasks` (cached to `state/slack_routine_status.json`); event counts via SQL; cursor age from `state/slack_cursors.json`. Surfaces a **"DM skip ✓"** line so misconfiguration is visible at every health check.

---

## 9. Compaction + housekeeping

### 9.1 Two-stage policy

| Stage | Trigger | Action |
|---|---|---|
| Stage 1 — noise trim | 90-day age | Delete events matching `noise` filter. Default: `<5 chars`, or `subtype='bot_message'` unless `bot_id` in `important_bots` allow-list. |
| Stage 2 — full compact | 365-day age | LLM-summarise thread → `subject_summary`. Delete events. |

Per-channel override:

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

Highest signal-per-byte (who was on, decisions, rollback timing, attribution); quarterly/annual review needs 1-2yr lookback; volume naturally small (channels archive after closure).

### 9.3 Storage estimate

| Channel class | Msgs/day | Year-1 | After Stage 1 | After Stage 2 |
|---|---|---|---|---|
| `#oncall-*` × 2 | 50/ch | 36MB | 22MB | ~4MB |
| `#incident-*` × 3 (intermittent) | 200/ch lifetime | 6MB | 6MB (no compact) | 6MB |
| Team rooms × 3 | 100/ch | 110MB | 60MB | ~10MB |
| **Total year-1** | — | **152MB** | **88MB** | **~20MB** |

Year-2+ steady state: ~88MB hot + ~20MB summaries = <110MB total.

---

## 10. Channel config

`config/slack_channels.yaml` (to be created):

```yaml
version: 1
defaults:
  compaction_policy: standard       # 90d trim + 365d compact
  ingest_depth: full
  pii_redaction: none               # v1; v2 enables 'redact'
channels:
  - id: C01ABC
    name: oncall-service-c-txn
    class: oncall
    notes: "service-c-transaction on-call; DR drill threads land here"
  - id: C02XYZ
    name: incident-payout-2026-04
    class: incident
    compaction_policy: never
    notes: "Auto-created incident war-room, 2026-04 withholding payout regression"
  # … user fills in remainder
```

Discovery: `/slack-discover` calls `slack_search_channels` per yaml name, populates `id`. Refuses to write IDs where MCP returns `is_im=true` / `is_mpim=true`.

---

## 11. Error handling + rate limits

### 11.1 MCP error response

| MCP result | Action |
|---|---|
| success | continue |
| error `rate_limit` / `retry_after` | skip remaining channels; advance cursors for completed only; flag in cron-status; **no in-skill retry — next fire IS the retry** |
| error `channel_not_found` | log; mark `disabled: true` in `state/slack_routine_status.json`; surface in cron-status |
| `is_im` / `is_mpim` for a configured channel | **REFUSE**; do NOT ingest; surface red in cron-status with name |
| `mcp__<slack>__*` tool absent | abort routine; cron-status "MCP unavailable" |
| transient timeout / network | rely on next-fire retry |

### 11.2 Idempotency

UPSERT key `(source='slack', subject, event_type, ts)` — re-running a window never dupes. Safe to manually re-fire `/slack-ingest` / `/slack-backfill`.

### 11.3 Cursor safety

`state/slack_cursors.json[channel_id]` advances only after the whole channel fetch commits. Partial failure → unchanged → next fire re-pulls (UPSERT-handled).

### 11.4 Edit-reconcile cost cap

>200 edits/inserts/deletes in the 7-day window (paste-storm / bot misbehaviour) → reconcile aborts that channel for this fire; warning in cron-status; cursor unchanged → next fire retries.

### 11.5 Rate-limit observability

On first rate-limit trip, log: error-string (pass-through) vs slow response (internal throttle); wall-clock impact; triggering channel(s). Surface in `state/slack_routine_status.json::last_rate_limit_event` + cron-status. Update §6/§18 after first observation.

### 11.6 Routine miss / Anthropic outage

Scheduled-tasks fails to fire → `last_success_ts` stale → cron-status yellow/red if no run >2h in work hours → owner manually invokes `/slack-ingest`. Cursors absorb the gap (no loss if gap < retention window).

---

## 12. Hard constraints

- **`channel_types=public_channel,private_channel` always.** Default is public-only → private channels return empty. Encoded in `/slack-discover` + any search wrapper.
- **No DMs / group DMs — hard-coded skip, not config-driven.** Before every fetch, inspect `is_im`/`is_mpim` (live or cached meta); if true, REFUSE regardless of yaml. Defence-in-depth: yaml allow-list (gate 1) + in-skill flag check (gate 2); both must agree.
- **`config/slack_channels.yaml` is the only source of truth.** No env-var / CLI bypass. Add a channel = edit yaml + re-run `/slack-discover`.
- **No writes to Slack.** `slack_send_message`/`_schedule_message`/canvas never invoked. Lint: any `slack-*` skill must not reference a write-MCP tool.
- **Cursors are per-channel.** Cross-channel failure doesn't reset others. `state/slack_cursors.json` canonical.
- **Deletion is tombstone, not hard delete.** `events.deleted_ts` set, row preserved. Narrative/retro filter `deleted_ts IS NOT NULL` for counts but may cite tombstoned content in audit queries.
- **Routine = scheduled-tasks MCP entries only.** `/slack-ingest`/`/slack-reconcile`/`/slack-backfill` are NEVER fired by LaunchAgent or system cron.
- **No retry inside the skill.** Next routine fire is the retry; cron-status is the single observability surface.

---

## 13. Performance budget

| Metric | Target |
|---|---|
| Backfill (8 ch × 1yr) | 1-2 hrs (est; MCP latency dominant) |
| Steady-state poll fire | < 30s |
| Reconcile (nightly) | < 5 min |
| Compaction (weekly) | < 15 min (≤500 digests @ $0.005 = $2.50/wk) |
| DB growth (post-compaction) | < 110 MB year-2 |
| `events.db` total | < 500 MB year-3 |

LLM digest: ~500 threads/wk × $0.005 = $2.50/wk = $130/yr; single-call (Haiku/Sonnet); cache by content hash (skip if upstream unchanged).

---

## 14. Roadmap

### v1 (this PRD, MCP path) — built end-to-end, launch gated on owner actions

Built (2026-05-13, commits `6f7ac0d`→`b5121a1`):
- **Phase A** — migration `004_slack_columns.sql` (`channel_id`, `thread_ts`, `edited_ts`, `deleted_ts`, `reactions_json` + 3 indexes); `derive/slack_upsert.py` (parser/UPSERT/reconcile, 8/8 self-tests); `ingest/common.py::_ensure_schema` lazy hook.
- **Phase B** — `slack-discover.md` (ID lookup + DM-skip); `config/slack_channels.yaml` IDs (8 channels, workspace=example); `state/slack_channel_meta.json` cache.
- **Phase C1** — `enrich_refs` + PR_URL_RE / SLACK_THREAD_URL_RE / SLACK_MENTION_RE; new ref_types `pull_request`, `slack_thread`; `Refs` + `insert_event` updated; `slack_upsert.py` writes events + event_refs in one txn.
- **Phase C2** — migration `005_thread_summary.sql`; `derive/build_thread_summary.py` (+`--rebuild-all`); `derive/story_graph.py` (BFS walker + source-aware page-prefix join).
- **Phase C3** — `derive/slack_ingest_runner.py`; `slack-ingest.md` / `slack-backfill.md` / `slack-reconcile.md`.
- **Phase C4** — `bin/refresh-event-refs.py` (re-run enrich_refs on existing rows; dry-run ~327k refs across 44k rows); PRD §7 + §7.1-7.4 + §18 + §19.

Remaining for launch (owner-gated):
- [ ] Run `/slack-backfill` for 8 channels (paid turns)
- [ ] Apply `bin/refresh-event-refs.py` (large mutation, see §19)
- [ ] `ingest/slack-compact.py` + `bin/slack-compact.sh` (Phase D, local-only) + `com.example.slack-compact.plist` LaunchAgent
- [ ] Scheduled-tasks routines: `/slack-ingest` (30 min, work hours) + `/slack-reconcile` (nightly 02:00 IST)
- [ ] `bin/cron-status.sh` SLACK block reads `state/slack_routine_status.json` (Phase E)
- [ ] `detect_ops_tickets` extended to scan Slack thread parents (Phase E)
- [ ] `prd/PRD.md` §13 Story-index row → "Live" after backfill + routines fire

### v2 — PII redaction
`slack_upsert.py` PAN/phone/IFSC/long-account-id strip before write; one-time backfill redaction pass; honour per-channel `pii_redaction: redact`.

### v3 — Direct HTTP token path (cost/reliability optimisation)
**Triggers:** MCP >$X/mo in turns; scheduled-tasks misses >5%; admin approves OAuth-app install.
**Scope:** `ingest/slack.py` HTTP variant (`xoxp-`/`xoxc-`); LaunchAgent-driven, bypasses MCP; same schema/compaction/DM-skip; switchover gate `transport: http|mcp` (per-channel or global); MCP stays as fallback.

### v4 — Search-augmented
`slack_search_public_and_private` for ad-hoc keyword sweeps when adding channels mid-cycle; consider as `detect_ops_tickets` input.

---

## 15. Test plan

**Unit (`derive/slack_upsert.py`):** `normalize_message` (top-level / parent / reply / edited / bot_message / reactions / attachments); `upsert_event` (idempotent re-run; newer `edited.ts` → UPDATE); `reconcile_window::detect_edits` / `detect_deletions`; `is_dm_channel` (True iff `is_im` OR `is_mpim`).

**Integration:** fresh DB → `/slack-backfill channels=<test> days=1` → count matches Slack UI; edit-in-Slack → `/slack-reconcile` updates body+edited_ts; delete → sets deleted_ts; point `/slack-discover` at a DM name → REFUSES + errors; `/narrative` cites test threads in Ops section.

**Smoke (post-deploy):** cron-status SLACK green within 30 min of first fire; `last_success_ts` advances each fire; `count(*) where source='slack' and ts > now-1h` > 0 within 30 min of activity; `count(*) where source='slack' and is_im` = 0 (DM hard-skip).

---

## 16. Open items / decisions deferred

| # | Item | Default chosen | Revisit when |
|---|---|---|---|
| 1 | Channel list | 8 channels (2026-05-13); IDs `TODO` until `/slack-discover` runs | first build of `/slack-discover` |
| 2 | **Opsgenie channel compaction** | **`never`** (canonical incident timeline preserved forever) **+ `keep_bot_messages: true`** | year-2 if DB growth surprises |
| 3 | LLM model for digest | Claude Haiku (cheapest) | if digest quality complaints |
| 4 | Reconcile window | 7 days | if edits beyond 7d become common |
| 5 | Routine cadence | 30 min, work hours (06:00–16:00 UTC = 11:30–21:30 IST) | if off-hours coverage needed |
| 6 | Storage cap | 500 MB year-3 | when DB approaches limit |
| 7 | Direct HTTP token path | deferred to v3 | when MCP cost/reliability painful |
| 8 | scheduled-tasks MCP reliability | assume good for v1 | if missed fires >5% in first 30 days |
| 9 | Per-fire $$ tracking | via `state/slack_routine_status.json::cost_estimate` | once first month's data lands |

> **Note on item 2 (opsgenie-prod-service-c):** channel-creation backfill — `compaction_policy: never` means backfill pulls from `channel.created_at` (not `now-365d`), preserving the full incident timeline including bot messages.

---

## 17. References

- Slack MCP tools — SessionStart `<system-reminder>` deferred-tools dump.
- Slack Web API (MCP wraps these): `conversations.history`, `conversations.replies`, `conversations.list`; message-edit semantics → `message_changed`.
- scheduled-tasks MCP routine fire — `mcp__scheduled-tasks__create_scheduled_task`.
- Existing ingest patterns (shape followed; `ingest/` scripts replaced by `.claude/commands/` skills in v1): `ingest/jira.py`, `github.py`, `confluence.py`.
- Handoff: `work-context/handoff-2026-05-12-2239.md` §7.
- Parent PRD: [`PRD.md`](PRD.md) (§13 Story index).
- Module integration target: `derive/jira_metrics.py::detect_ops_tickets` + `OPS_PATTERNS`.
- Session pivot (MCP over bot, chat 2026-05-13): owner chose MCP-only to skip Slack-app provisioning + admin approval.

---

## 18. Operational learnings (log as observed)

Append-only; each entry: date · observed · PRD assumption · resolution.

### 2026-05-13 — initial schema + live probe

- **MCP identity:** `owner@example.com`, workspace=`example`, `U0EXAMPLE` (user scope).
- **Cursor pagination works** (`slack_read_channel` returned `pagination_info.cursor` on page 1 of `service-c-internal`). Confirms §6.
- **Response is human-readable text, NOT JSON** — `=== Message from <Name> (U…) at <ts> ===` + `Message TS:`; `response_format=detailed` required. §6 updated.
- **Default `channel_types` public-only**; all 8 curated channels private → must pass `public_channel,private_channel`. §12 updated.
- **Not yet observed:** rate-limit behaviour (assume 429 pass-through); edit/delete signal format; bot-message ID prefix (likely `B0…`).

### 2026-05-13 — Phase G — first backfill (service-c-internal, 30d)

- **Channel** C0EXAMPLE (service-c-internal, private, 'team', standard). **Window** 30d (2026-04-13→05-13); 365d deferred (cost).
- **Page 1 only** (owner stopped to avoid context bloat; routine takes over from advanced cursor).
- **94 thread_started inserted; thread replies SKIPPED** — reconcile (nightly 02:09 IST, 7d) catches recent-thread replies.
- **event_refs:** 102 person + 20 project + 3 slack_thread + 3 page + 2 pull_request + 1 ticket.
- **thread_summary:** 94 threads; **5 ops-pattern hits** (3 incident, 1 drill "VendorX dr drill got extended", 1 year_end — Alice withholding `example_tds_table`).
- **Cron-status:** green, +94 new, 1 active cursor, DM-skip ✓. **Cursor at** 1778667150.756969 (2026-05-13 15:42 IST).

Confirmed working (live): regex parsing; `<@U…>` mention resolution via `slack_users_cache` from `people.yaml::slack_id` (Alice/Frank/Eve/Bob/owner/Dan/Grace/Carol/Ivan); `[Epic EX-N]` project matching; thread permalink → cross-thread ref; DM hard-skip held; `ops_pattern_match` regex correct; `started_by_canonical` resolved for all 94 (no raw U-id leaks).

Still NOT observed live: edit signal format (no edits in window); deletion signal; rate-limit (fire under tier-3); `/slack-reconcile` + `/slack-ingest` routines (next fires 02:09 / 12:06 IST tomorrow). **Bot ID prefix confirmed `B0…`** (`B0EXAMPLE` for example-monthly-update Standup + Weekly-Oncall-Review).

Deferred: full 365d backfill (~$1-2 + many calls); thread-reply backfill for 25 threads with `reply_count>0` (reconcile catches last 7d; older need explicit fetch); remaining 7 channels (service-c-public ×2, service-c-core-team, example-migration-wg, opsgenie-prod-service-c, service-c-oncall, on-call) — never backfilled.

### 2026-05-13 — Phase A-C built end-to-end (`6f7ac0d`→`b5121a1`)

- **Phase A:** migration 004 applied live; `slack_upsert.py` 380 lines, 8 self-tests; 43,921 existing rows untouched (NULL new cols).
- **Phase B:** all 8 channels resolved; 0 DM violations / 0 archived / 0 not-found; public-only trap confirmed (all 8 private).
- **Phase C1:** `enrich_refs` + 3 regexes; 9th self-test covers all 6 ref_types from one Slack body.
- **Phase C2:** migration 005 applied; `story_graph.py` walk from `EX-2238` → 14 child Jira at hop=1 + service-a#517 (Alice withholding PR) at hop=2. Graph works end-to-end on existing data, even without Slack data.
- **Phase C3:** runner smoke-test vs live MCP probe: 1 msg parsed→inserted→cursor advanced→cleanup clean.
- **Phase C4:** `refresh-event-refs.py` dry-run ~327k missing refs across 44k rows. Owner-decision when to apply.

Still NOT observed live: MCP rate-limit under sustained backfill; edit/delete signal format; bot ID variation; backfill wall-clock vs §13; compaction digest quality (Phase D, not built).

### Template for future entries

```
### YYYY-MM-DD — <short title>
- **Observed**: <what happened>
- **PRD assumption**: <what we expected (link section)>
- **Delta / impact**: <how it differs>
- **Resolution**: <patch / followup / no-op>
```

---

## 19. People-yaml late-add cookbook

Applies to ALL sources (Slack surfaced the question 2026-05-13).

**How attribution works:** `events.actor` is ALWAYS the raw source identifier (github handle / jira email / confluence email / slack U-id), never canonicalised at write time. `derive/jira_metrics.py::get_aliases_for(canonical)` reads `people.yaml` at query time; `/narrative`/`/retro` use `WHERE actor IN (alias_list)`. So **adding a person later auto-picks-up historical events at query time** — no migration for `events.actor`.

**BUT `event_refs` IS canonicalised at write time** (`enrich_refs`). Late-added people get NULL person refs on historical rows. Same for materialised views (`thread_summary.started_by_canonical`/`participants_json`, `trd_owners.owner`/`scores_json`) — snapshot at build time.

### Cookbook — "I added a person to people.yaml"

```
# 1. Edit config/people.yaml: canonical / name / email / github / slack_id / jira_id / role

cd ~/context/work-context

# 2. Backfill person refs (historical mentions, authorship). Scope to keep bounded:
.venv/bin/python bin/refresh-event-refs.py --missing-person
# Or: --source jira --since 2026-01-01T00:00:00Z

# 3. Refresh Confluence TRD ownership snapshots:
.venv/bin/python derive/build_trd_owners.py --rebuild-all

# 4. Refresh Slack thread_summary snapshots:
.venv/bin/python derive/build_thread_summary.py --rebuild-all

# 5. Refresh subject_summary IF person changes domain attribution (usually skip):
/rollup

# 6. Verify:
/narrative jane-doe 60
```

### What does NOT need to happen

No re-ingest from any source; no `events.actor` migration (already raw, alias-resolvable at query time); no re-fetch of Slack history (`/slack-discover` need not re-run); no `event_refs` DELETE (`refresh-event-refs.py` is INSERT OR IGNORE — additive, idempotent).

### Edge cases

| Scenario | Procedure |
|---|---|
| `slack_id` typo | Fix yaml, run steps 2 + 4 |
| Different past github handle | Add both aliases (`github`, `github_alt`); `get_aliases_for` returns all. Future: array-field Refs |
| Name change | Update `name`, re-run 2/3/4 (name appears in title extractions) |
| Person leaves org | Do NOT remove. Mark `status: alumnus` (new field, ignored by queries). Attribution stays intact |
| Never in any ingested tool | No effect until they appear in github/jira/confluence/slack |

### Backfill cost (full `refresh-event-refs.py`, 2026-05-13 dry-run)

| Sub-metric | Count |
|---|---|
| rows scanned | 43,921 |
| rows gaining refs | 15,084 (~34%) |
| total refs to add | ~327,554 |
| by type | project 316,326 · ticket 7,056 · person 3,950 · slack_thread 127 · pull_request 95 |
| by source | jira 317,364 · github 8,693 · confluence 1,497 |
| wall-clock | ~2-3 min |

Owner-decision when to run. Large but reversible per `(event_id, ref_type, ref_value)` PK.

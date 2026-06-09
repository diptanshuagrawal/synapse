Steady-state Slack ingest. Reads new messages from every channel in `config/slack_channels.yaml` since their last cursor, writes to `events.db` + raw JSONL. Refreshes `thread_summary` for affected threads.

## Usage — `/slack-ingest-mcp [channel]`

If invoked with `help`, `-h`, or `--help` as the argument: print this Usage block verbatim and STOP — do not run anything.

**What it does:** Steady-state Slack ingest via MCP — reads new messages per channel since last cursor into `events.db` + JSONL, refreshes thread summaries.

**Modes:**
- empty → all channels in the yaml since their last cursor.
- `<channel-name>` → ingest just that one channel.

Normally fired by a `scheduled-tasks` MCP routine every ~30 min during work hours; refreshes `thread_summary` for affected threads.

**Usage:** `/slack-ingest-mcp [channel]`
- `channel` (optional) — empty → all channels; a name ingests just that one.

Fired by a `scheduled-tasks` MCP routine every ~30 min during work hours; can also be invoked manually with `$ARGUMENTS = <channel-name>` to ingest one specific channel.

## Permission posture (unattended)

When fired by routine: NEVER pause for permission prompts.
- File/Bash/MCP under `$HOME/context/**`, `/tmp/**`, `.venv/bin/python *`, `derive/* *`, `bin/* *`, `git *`, `sqlite3 *`, Slack MCP, context-mode MCP — all pre-approved.
- Owner has `defaultMode: bypassPermissions` in `$HOME/context/.claude/settings.local.json`.
- If a tool unexpectedly gates on permission: abort the fire, record `errors: ["permission_blocked:<tool>"]` via `record-fire`, exit. Do NOT wait for input. Next 30-min fire retries.

## Phase 1 — Setup

```bash
cd $HOME/context/work-context
```

Read channel list. Use Python via `ctx_execute` so output stays bounded:

```python
import yaml, json
with open('config/slack_channels.yaml') as f:
    cfg = yaml.safe_load(f)
channels = [c for c in cfg['channels'] if c.get('id') and c['id'] != 'TODO']
# If $ARGUMENTS is set, filter to that single channel by name.
print(json.dumps([{'id': c['id'], 'name': c['name'], 'class': c.get('class')} for c in channels]))
```

If filtered list is empty: print error + stop. Otherwise proceed.

## Phase 2 — Per-channel ingest loop

For each channel `(id, name, class)`:

### 2a. Read cursor

For unattended routine fires: read ALL cursors in one batch call BEFORE the loop, so the per-channel iteration uses an in-memory list (no shell variable expansion → no permission chip):

```bash
.venv/bin/python derive/slack_ingest_runner.py read-cursors-all
```

Output: JSON list of `{id, name, class, cursor_ts}` for every channel in `config/slack_channels.yaml`. Iterate that list.

(Single-channel manual invocation can still use `read-cursor --channel-id <ID>`.)

If `cursor_ts` is null, that channel has never been ingested — for `/slack-ingest` (steady-state), skip and recommend running `/slack-backfill` for that channel first. Don't backfill silently from a steady-state invocation.

### 2b. Fetch top-level messages since cursor

```
mcp__<slack>__slack_read_channel(
    channel_id=<ID>,
    oldest=<cursor_ts>,     # raw Slack ts string
    limit=100,
    response_format="detailed",
)
```

Capture full response (results + pagination_info).

### 2c. Hand response to runner

**Tool result shape:** the `PostToolUse` hook `slack-mcp-persist.sh` (registered in `$HOME/context/.claude/settings.json`) intercepts every `slack_read_channel` / `slack_read_thread` response over ~8KB and persists the byte-faithful body to `/tmp/slack_mcp_cache/<channel>_<unix_ms>.txt`. Claude sees a stub `{file_saved, channel_id, body_bytes, ...}`. Bodies ≤8KB come inline — `Write` them to `/tmp/slack_ingest_<channel>_p1.txt` as before.

Never re-emit a >8KB body through `Write` — transcription corruption risk. Use `file_saved` directly.

```bash
.venv/bin/python derive/slack_ingest_runner.py upsert \
    --channel-id <ID> \
    --response-file <file_saved-from-stub OR /tmp/slack_ingest_<channel>_p1.txt>
```

Runner returns JSON: `{parsed, inserted, updated, skipped, errors, next_cursor, thread_parents_with_replies}`. Capture.

### 2d. Pagination

If runner's `next_cursor` is non-null: re-fetch with same params + `cursor=<next>`, hand to runner again. Loop until `next_cursor` is null.

**Pagination cap:** max 10 pages per channel per fire (1000 messages). If 10th page still has more, log warning + advance cursor + return — pick up rest on next fire.

### 2e. Fetch thread replies

For each `thread_parent_ts` returned by runner (parents with replies):

```
mcp__<slack>__slack_read_thread(
    channel_id=<ID>,
    message_ts=<thread_parent_ts>,
    limit=1000,
    response_format="detailed",
)
```

Hook persists the response if >8KB (typical for active threads). Inline-only path: `Write` to `/tmp/slack_ingest_<channel>_<thread_ts>.txt`. Either way, hand to runner with `--thread-parent-ts`:

```bash
.venv/bin/python derive/slack_ingest_runner.py upsert \
    --channel-id <ID> \
    --thread-parent-ts <thread_parent_ts> \
    --response-file <file_saved-from-stub OR /tmp/slack_ingest_<channel>_<thread_ts>.txt>
```

(Cursor advance is suppressed when `--thread-parent-ts` is set — thread replies are pulled by parent_ts, not by channel cursor.)

### 2f. Per-channel error handling

Per `prd/slack-ingest.md` §11.1:
- MCP error containing `rate_limit` / `retry_after` → stop fire; cursors only advance for completed channels; next fire retries naturally.
- `channel_not_found` → log + skip channel + mark disabled in `state/slack_routine_status.json`.
- `is_im` / `is_mpim` in MCP-returned metadata → REFUSE; surface red error; skip.
- Any uncaught Python exception in runner → log + skip channel; cursor unchanged → next fire retries.

## Phase 2.5 — Stale-thread reconcile (catches replies-on-old-parents)

`/slack-ingest`'s cursor advances forward, so a parent posted *before* cursor that gains a reply *after* cursor will never appear in the channel-page feed again. The `reply_count` column lets us detect this drift cheaply.

For unattended routine fires: emit the full stale map once (no shell loop):

```bash
.venv/bin/python derive/slack_backfill_helper.py stale-threads-all
```

Output: `{channel_id: [parent_ts, ...]}` for every channel in `config/slack_channels.yaml`. Iterate that map.

(Single-channel manual invocation can still use `stale-threads <ID>`.)

Returns parent_ts where stored `reply_count` > thread_reply rows actually in DB. Typically 0-20 per channel per fire in steady state.

For each parent_ts returned, fire `slack_read_thread` (same call shape as Phase 2e) and drain via:

```bash
.venv/bin/python derive/slack_backfill_helper.py drain-threads <ID>
```

The thread response carries Slack's current declared reply count; the parser updates `events.reply_count` so the drift is closed even if the actual thread_reply rows we just inserted don't match (e.g. a reply was deleted between fetches).

If `stale-threads` returns more than 50 entries: log a warning, fetch the oldest 50, defer rest to next fire. Don't blow the wall-clock budget.

## Phase 3 — Refresh `thread_summary`

Incremental rebuild (only threads with new ts):

```bash
.venv/bin/python derive/build_thread_summary.py
```

Captures the JSON output (counts).

## Phase 4 — Record fire status

```bash
SUMMARY='{"channels_attempted":N,"channels_succeeded":N,"total_inserted":N,"total_updated":N,"errors":[]}'
.venv/bin/python derive/slack_ingest_runner.py record-fire --summary-json "$SUMMARY"
```

## Phase 5 — Print summary

```
✓ slack-ingest fire complete @ <ISO ts>

<channel-name>     +N new   +M edits   <P thread fetches>
<channel-name>     +N new   ...
...
errors:            <list>
total new events:  <sum>
total thread refs: <sum from runner reports>
threads built:     <build_thread_summary inserted+updated>

Cursor advanced for: <N>/8 channels
```

## Hard constraints

- DM hard-skip: runner enforces; skill also checks `config/slack_channels.yaml` is the only source-of-truth for which channels to fetch.
- No in-skill retry. Errors → flag + move on. Next routine fire IS the retry.
- Never call any `slack_send_message` / `slack_schedule_message` / `slack_*canvas` MCP tool — ingest is read-only.
- Pagination cap = 10 pages/channel/fire (~1000 messages). Spillover handled on next fire via cursor.
- Skip channels with `cursor_ts == null` — those need `/slack-backfill` first; don't silently backfill from a steady-state fire.
- After ingest, ALWAYS run `build_thread_summary.py` (incremental) to keep the materialised view current.

## After write

Owner can verify:

```bash
.venv/bin/python derive/slack_ingest_runner.py status     # show cursors + last fire
sqlite3 index/events.db "SELECT COUNT(*) FROM events WHERE source='slack'"
sqlite3 index/events.db "SELECT COUNT(*) FROM thread_summary"
```

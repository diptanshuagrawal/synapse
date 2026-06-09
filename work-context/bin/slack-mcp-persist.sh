#!/usr/bin/env bash
# PostToolUse hook: intercept Slack MCP responses (slack_read_channel,
# slack_read_thread) and persist the full byte-faithful body to disk.
# Replaces the tool output Claude sees with a tiny stub pointing to the file.
#
# Why: large Slack MCP responses come back inline in the model context.
# Re-emitting them through Write tool risks transcription corruption when
# the body is ~30KB+. The harness, however, hands hooks the literal bytes
# via stdin (no truncation). We dump those bytes to disk and let the
# slack_ingest_runner read them directly.
#
# Stdin schema (from Claude Code PostToolUse):
#   {
#     "tool_name": "mcp__<srv>__slack_read_channel",
#     "tool_input": { "channel_id": "...", "cursor": "...", ... },
#     "tool_response": [ { "type":"text", "text":"<JSON wrapper>" }, ... ],
#     "tool_use_id": "toolu_...",
#     ...
#   }
#
# Output: JSON on stdout that mutates the tool result Claude sees:
#   { "hookSpecificOutput": { "hookEventName":"PostToolUse",
#                              "updatedMCPToolOutput": { "file_saved":"...", ... } } }
#
# Failure mode: if extraction fails, emit nothing → harness keeps original
# tool output (Claude falls back to the inline-handling path).

set -u

CACHE_DIR="/tmp/slack_mcp_cache"
TRACE_LOG="/tmp/slack_hook_trace.log"
PROJECT_ROOT="${WORK_CONTEXT_ROOT:-$HOME/context}"
mkdir -p "$CACHE_DIR" 2>/dev/null || {
  # mkdir failed — log + passthrough so harness keeps original response.
  printf '%s\tmkdir_failed\tdir=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$CACHE_DIR" >> "$TRACE_LOG" 2>/dev/null
  exit 0
}

trace() {
  # Defensive tracing — never let the trace itself break the hook.
  printf '%s\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$TRACE_LOG" 2>/dev/null || true
}

# Read entire stdin once.
input=$(cat)

# Trace: record every invocation regardless of outcome.
trace "hook_fired	bytes=${#input}"

# Scope guard: hook is registered globally (~/.claude/settings.json) but only
# applies to the ~/context project. Exit silently if session CWD is elsewhere.
session_cwd=$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null || echo "")
case "$session_cwd" in
  "$PROJECT_ROOT"|"$PROJECT_ROOT"/*) ;;
  *)
    trace "scope_skip	cwd=$session_cwd"
    exit 0
    ;;
esac

# Extract pieces. jq -e returns non-zero on null/missing so we can detect.
tool_name=$(printf '%s' "$input" | jq -r '.tool_name // empty')
case "$tool_name" in
  mcp__*__slack_read_channel|mcp__*__slack_read_thread) ;;
  *) exit 0 ;;  # not our matcher; do nothing
esac

channel_id=$(printf '%s' "$input" | jq -r '.tool_input.channel_id // "unknown"')
thread_ts=$(printf '%s' "$input" | jq -r '.tool_input.message_ts // empty')
cursor=$(printf '%s' "$input" | jq -r '.tool_input.cursor // empty')

# tool_response is a list of {type,text}. Concatenate all text parts.
# If it's already an object (some servers), fall back to raw JSON.
body=$(printf '%s' "$input" | jq -r '
  if (.tool_response | type) == "array" then
    .tool_response | map(select(.type == "text") | .text) | join("")
  elif (.tool_response | type) == "object" then
    .tool_response | tostring
  else
    .tool_response | tostring
  end
')

# Detect error responses — leave them inline for Claude to handle.
if [ -z "$body" ] || [ "$body" = "null" ]; then
  exit 0
fi

# Always persist Slack MCP responses to disk: zero transcription risk
# trumps the small extra inode cost. Backfill creates hundreds of small
# files; that's fine — periodic /tmp cleanup handles it.
body_bytes=${#body}

# Filename: <channel>_<unix_ms>[_thread_<parent_ts>][_p<cursor_hash>].txt
unix_ms=$(python3 -c 'import time; print(int(time.time()*1000))')
suffix=""
[ -n "$thread_ts" ] && suffix="${suffix}_thread_${thread_ts}"
[ -n "$cursor" ]    && suffix="${suffix}_c$(printf '%s' "$cursor" | shasum | cut -c1-8)"
out_file="${CACHE_DIR}/${channel_id}_${unix_ms}${suffix}.txt"

if ! printf '%s' "$body" > "$out_file" 2>/dev/null; then
  trace "write_failed	file=$out_file passthrough"
  exit 0  # passthrough: harness keeps original (model handles inline)
fi

# Verify size — if disk full or short-write, bail to passthrough.
actual_bytes=$(wc -c < "$out_file" 2>/dev/null | tr -d ' ')
if [ -z "$actual_bytes" ] || [ "$actual_bytes" -lt "$body_bytes" ]; then
  trace "short_write	file=$out_file expected=$body_bytes actual=$actual_bytes passthrough"
  rm -f "$out_file" 2>/dev/null
  exit 0
fi

trace "persisted	file=$out_file bytes=$body_bytes"

# Emit stub. Must match the MCP tool_response shape: an array of
# {type:"text", text:"..."} parts, so the harness can reduce over it.
stub_json=$(jq -n -c \
  --arg file "$out_file" \
  --arg channel "$channel_id" \
  --arg thread "$thread_ts" \
  --arg cursor "$cursor" \
  --argjson bytes "$body_bytes" \
  --arg tool "$tool_name" \
  '{
    file_saved: $file,
    channel_id: $channel,
    thread_parent_ts: (if $thread == "" then null else $thread end),
    cursor_in: (if $cursor == "" then null else $cursor end),
    body_bytes: $bytes,
    tool: $tool,
    note: "Full response on disk. Pass file_saved to slack_ingest_runner.py via --response-file."
  }')

jq -n --arg stub "$stub_json" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    updatedMCPToolOutput: [
      { type: "text", text: $stub }
    ]
  }
}'

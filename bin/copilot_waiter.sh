#!/usr/bin/env bash
# copilot_waiter.sh — while copilot is ARMED, ensure the live transcriber runs
# for every recording (EXPERIMENTAL P6). Spawned by the /copilot arm toggle so
# the live transcript flows even when no Claude session has latched yet (the
# session provides SUGGESTIONS; the transcript must not depend on it).
# Exits when disarmed. Self-limiting: one instance via lock dir.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CAP="$REPO/work-context/transcripts/.capture"
LOCK="$CAP/.waiter.lock"

mkdir "$LOCK" 2>/dev/null || exit 0

# Resident whisper-server (v2 live layer): model loads ONCE and stays warm for
# the whole armed stretch — kills the per-slice reload tax (~2s/slice). Runs
# ONLY while armed; killed on disarm. live_transcribe falls back to whisper-cli
# if the server is missing/dead, so this is a soft dependency.
ASR_PID=""
TURBO="$HOME/.whisper-models/ggml-large-v3-turbo.bin"
VAD="$HOME/.whisper-models/ggml-silero-v5.1.2.bin"
if [ -f "$TURBO" ] && command -v whisper-server >/dev/null; then
  VARGS=""; [ -f "$VAD" ] && VARGS="--vad --vad-model $VAD"
  nohup whisper-server -m "$TURBO" --host 127.0.0.1 --port 8790 -l en $VARGS \
    >/dev/null 2>&1 &
  ASR_PID=$!
fi
trap '[ -n "$ASR_PID" ] && kill "$ASR_PID" 2>/dev/null; rm -rf "$LOCK"' EXIT

while [ -f "$CAP/copilot.on" ]; do
  if [ -f "$CAP/pid" ] && kill -0 "$(cut -d' ' -f1 "$CAP/pid")" 2>/dev/null; then
    if [ -n "$ASR_PID" ]; then
      # v3.1: BOTH streams via persistent server-slice daemons (~2s slices,
      # server-side silero VAD — whisper-stream was flooding "Thank you."
      # hallucinations on quiet steps, retired 2026-07-19). Sorted by [mm:ss].
      "$REPO/work-context/.venv/bin/python3" \
        "$REPO/work-context/derive/meetings/live_stream_daemon.py" me >/dev/null 2>&1 &
      MPID=$!
      "$REPO/work-context/.venv/bin/python3" \
        "$REPO/work-context/derive/meetings/live_stream_daemon.py" them >/dev/null 2>&1
      # reap ONLY the me-daemon — a bare `wait` also waits on the resident
      # whisper-server child and wedges the loop forever (bug 2026-07-19)
      wait $MPID 2>/dev/null
    else
      # fallback: v2 chunked path (handles both streams)
      bash "$REPO/bin/live_transcribe.sh" >/dev/null 2>&1
    fi
  fi
  sleep 5
done

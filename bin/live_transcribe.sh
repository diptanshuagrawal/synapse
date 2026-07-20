#!/usr/bin/env bash
# live_transcribe.sh — incremental (near-real-time) transcription of the
# RECORDING IN PROGRESS. EXPERIMENTAL (copilot P6) — read-only on the capture:
# it slices the growing wav files the recorder is writing and never touches
# the production pipeline (meet-record / meet_watch / sweep are unaware of it).
#
#   loop while a recording is active:
#     every ~12s: transcribe the new audio span of mic.wav (Me) and sys.wav
#     (Them) with the FAST turbo model → append tagged lines to
#     .capture/live_transcript.txt (which the /copilot page + skill tail).
#
# Latency ≈ chunk size + decode ≈ 15-20s behind speech. The FINAL transcript
# still comes from the normal large-v3 batch pass after the meeting — this
# stream is disposable working text for the copilot only.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CAP="$REPO/work-context/transcripts/.capture"
LIVE="$CAP/live_transcript.txt"
LOCK="$CAP/.live.lock"
TURBO="$HOME/.whisper-models/ggml-large-v3-turbo.bin"
CHUNK=4           # seconds per incremental slice (owner: still slow at 6)
MIN_NEW=2         # don't bother decoding less than this many new seconds

[ -f "$TURBO" ] || { echo "no turbo model"; exit 1; }
mkdir "$LOCK" 2>/dev/null || { echo "already running"; exit 0; }
trap 'rm -rf "$LOCK"' EXIT

is_recording() { [ -f "$CAP/pid" ] && kill -0 "$(cut -d' ' -f1 "$CAP/pid")" 2>/dev/null; }

is_recording || { echo "no active recording"; exit 0; }
: > "$LIVE"
echo "live transcription started $(date '+%H:%M:%S')" >> "$LIVE"

# VAD (kills silence hallucinations on quiet chunks) + the domain-vocab prompt
# (cached once — build_vocab hits events.db; don't re-run it every 12s).
VAD_MODEL="$HOME/.whisper-models/ggml-silero-v5.1.2.bin"
VAD_ARGS=""; [ -f "$VAD_MODEL" ] && VAD_ARGS="--vad --vad-model $VAD_MODEL"
PROMPT=""
if [ -x "$REPO/work-context/.venv/bin/python3" ]; then
  PROMPT=$("$REPO/work-context/.venv/bin/python3" "$REPO/work-context/derive/meetings/build_vocab.py" 2>/dev/null)
fi

# A WAV mid-write has STALE header sizes — ffprobe duration and ffmpeg -ss both
# lie/stop early. So: read the stream FORMAT from the header (valid from byte 0),
# then compute duration from FILE SIZE and slice raw PCM bytes with tail/head.
probe_fmt() {  # -> "codec rate ch bits" (empty until header readable)
  ffprobe -v error -show_entries stream=codec_name,sample_rate,channels,bits_per_sample \
    -of csv=p=0 "$1" 2>/dev/null | head -1 | tr ',' ' '
}

# Streams run as PARALLEL subshells, so per-stream state (offset + format)
# lives in files, not shell vars (a subshell can't mutate the parent).
rm -f "$CAP"/.live_state_* 2>/dev/null

process_stream() {  # $1=tag $2=src $3=label; state in $CAP/.live_state_$tag
  local tag="$1" SRC="$2" LBL="$3"
  [ -s "$SRC" ] || return 0
  local ST="$CAP/.live_state_$tag" bps=0 rate=0 chan=0 fmt="" done_s=0
  [ -f "$ST" ] && read -r done_s bps rate chan fmt < "$ST"
  if [ "$bps" -eq 0 ]; then
    local codec r c bits
    read -r codec r c bits <<< "$(probe_fmt "$SRC")"
    [ -n "${bits:-}" ] && [ "${bits:-0}" -gt 0 ] || return 0
    bps=$(( r * c * bits / 8 )); rate=$r; chan=$c
    case "$codec" in
      pcm_s16le) fmt="s16le";; pcm_s24le) fmt="s24le";; *) fmt="f32le";;
    esac
  fi
  local size total new span off cnt
  size=$(stat -f %z "$SRC")
  total=$(( (size - 4096) / bps ))   # header/finalization slop
  new=$(( total - done_s ))
  [ "$new" -lt "$MIN_NEW" ] && return 0
  span=$new; [ "$span" -gt 30 ] && span=30   # cap catch-up slices
  off=$(( 44 + done_s * bps ))
  cnt=$(( span * bps ))
  local RAW=/tmp/live_${tag}_$$.raw S=/tmp/live_${tag}_$$.wav T=/tmp/live_${tag}_$$
  tail -c "+$(( off + 1 ))" "$SRC" | head -c "$cnt" > "$RAW"
  ffmpeg -y -loglevel error -f "$fmt" -ar "$rate" -ac "$chan" -i "$RAW" -ar 16000 -ac 1 "$S" 2>/dev/null
  rm -f "$RAW"
  if [ -s "$S" ]; then
    local ts txt=""
    ts=$(printf '%02d:%02d' $(( done_s / 60 )) $(( done_s % 60 )))
    # PRIMARY: resident whisper-server (warm model, ~1s decode) + in-process
    # corrections. FALLBACK (exit 3 = server down): spawn whisper-cli as before.
    if [ -x "$REPO/work-context/.venv/bin/python3" ]; then
      txt=$("$REPO/work-context/.venv/bin/python3" \
        "$REPO/work-context/derive/meetings/live_asr_client.py" "$S" \
        ${PROMPT:+"This bank meeting discusses $PROMPT."} 2>/dev/null) || txt=""
    fi
    if [ -z "$txt" ]; then
      # fallback: per-slice whisper-cli (model reload tax, but always works)
      whisper-cli -m "$TURBO" -f "$S" -l en -otxt -of "$T" --no-prints -mc 0 $VAD_ARGS \
        ${PROMPT:+--prompt "This bank meeting discusses $PROMPT."} >/dev/null 2>&1
      if [ -s "$T.txt" ]; then
        txt=$(tr '\n' ' ' < "$T.txt" | sed -E 's/ +/ /g; s/^ //; s/ $//')
        if [ -x "$REPO/work-context/.venv/bin/python3" ]; then
          txt=$(printf '%s' "$txt" | "$REPO/work-context/.venv/bin/python3" "$REPO/work-context/derive/meetings/correct.py")
        fi
      fi
    fi
    if [ -n "$txt" ] && ! echo "$txt" | grep -qiE 'www\.|https?://|thanks for watching|for more information'; then
      echo "[$ts] $LBL: $txt" >> "$LIVE"
    fi
  fi
  rm -f "$S" "$T.txt"
  echo "$(( done_s + span )) $bps $rate $chan $fmt" > "$ST"
}

while is_recording; do
  process_stream me "$CAP/mic.wav" "Me" &
  P1=$!
  process_stream them "$CAP/sys.wav" "Them" &
  P2=$!
  wait $P1 $P2
  sleep "$CHUNK"
done
echo "live transcription ended $(date '+%H:%M:%S')" >> "$LIVE"

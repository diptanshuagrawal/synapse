#!/usr/bin/env bash
# transcribe.sh — local Whisper transcription for meeting audio.
#
# Usage: transcribe.sh <audio-file> <output-prefix>
#   Produces <output-prefix>.json (timestamped segments) + <output-prefix>.txt
#
# Fully offline: ffmpeg downmix → whisper.cpp (whisper-cli). No cloud, no API.
# Model override: WHISPER_MODEL=/path/to/ggml-*.bin
set -euo pipefail

AUDIO="${1:?usage: transcribe.sh <audio-file> <output-prefix>}"
OUT="${2:?usage: transcribe.sh <audio-file> <output-prefix>}"
# Default = full large-v3 (A/B 2026-07-18: equal-or-better accuracy than turbo,
# 2x slower but still ~7x realtime — irrelevant for background transcription).
# Falls back to turbo when large-v3 is absent; WHISPER_MODEL overrides both.
MODEL="${WHISPER_MODEL:-$HOME/.whisper-models/ggml-large-v3.bin}"
[ -f "$MODEL" ] || MODEL="$HOME/.whisper-models/ggml-large-v3-turbo.bin"

command -v whisper-cli >/dev/null || { echo "ERROR: whisper-cli not found (brew install whisper-cpp)" >&2; exit 1; }
command -v ffmpeg      >/dev/null || { echo "ERROR: ffmpeg not found" >&2; exit 1; }
[ -f "$MODEL" ] || { echo "ERROR: model not found: $MODEL" >&2; exit 1; }
[ -f "$AUDIO" ] || { echo "ERROR: audio not found: $AUDIO" >&2; exit 1; }

# whisper.cpp wants 16 kHz mono wav. Keep the intermediate next to the output
# (same filesystem) and clean it up on exit.
WAV="$(mktemp -t transcribe_XXXX).wav"
trap 'rm -f "$WAV"' EXIT
ffmpeg -y -loglevel error -i "$AUDIO" -ar 16000 -ac 1 "$WAV"

# SILENCE GATE — a near-silent track makes Whisper hallucinate training-data
# junk ("For more information visit www.fema.org.au", "Thanks for watching").
# The classic trigger: the system-audio ('them') stream of an IN-PERSON meeting
# with no remote party. Real speech peaks near 0 dB; an empty track maxes below
# -40 dB. If there's no real audio, emit an empty transcript instead of
# inventing one. (Runs per-stream, so it only nulls the silent side of a pair.)
MAXVOL=$(ffmpeg -i "$WAV" -af volumedetect -f null /dev/null 2>&1 \
  | sed -n 's/.*max_volume: \(-*[0-9.]*\) dB.*/\1/p')
if [ -n "$MAXVOL" ] && awk "BEGIN{exit !($MAXVOL < -40)}"; then
  : > "$OUT.txt"
  printf '{"transcription":[]}' > "$OUT.json"
  echo "OK (silent track ${MAXVOL}dB, skipped): $OUT.json"
  exit 0
fi

# Flag set tuned empirically 2026-07-17 (see project memory for the matrix):
# -l auto: meetings code-switch between languages; let Whisper detect.
# -bs 5: beam search — measurably better decoding, ~1.5x slower (fine offline).
# --prompt: domain vocab from config/transcribe.yaml biases decoding toward
#   real jargon/names (fixed "lean"→lien 24/24 on a real meeting). The bias
#   only propagates with DEFAULT context carry — do NOT add -mc 0/-mc 64
#   (both silently kill the vocab effect). Phrase the prompt as a natural
#   sentence: a "Vocabulary:"-style label leaks verbatim into the output.
# Context carry re-enables whisper's repetition loops; those are collapsed
# deterministically below (and in ingest) instead of at the decoder.
WC="$(dirname "$0")/../work-context"
VOCAB_FILE="$WC/config/transcribe.yaml"
PROMPT=""
# Auto-vocab (derive/meetings/build_vocab.py): curated terms + team first
# names + active service names & epic vocabulary from events.db + project
# keywords — new teammates/services/initiatives improve transcription with
# zero manual list-keeping. Static-list fallback when venv python is absent.
if [ -x "$WC/.venv/bin/python3" ]; then
  PROMPT=$("$WC/.venv/bin/python3" "$WC/derive/meetings/build_vocab.py" 2>/dev/null)
fi
if [ -z "$PROMPT" ] && [ -f "$VOCAB_FILE" ]; then
  PROMPT=$(sed -n 's/^  - //p' "$VOCAB_FILE" | paste -sd ', ' -)
fi
# -sns (suppress non-speech tokens) further reduces silence hallucinations
# that survive the gate above (partial-silence stretches within a real track).
# --vad (silero) trims non-speech BEFORE decoding when the model is present —
# fewer junk segments, tighter timestamps. Soft dependency: absent model → off.
VAD_MODEL="$HOME/.whisper-models/ggml-silero-v5.1.2.bin"
VAD_ARGS=""
[ -f "$VAD_MODEL" ] && VAD_ARGS="--vad --vad-model $VAD_MODEL"
whisper-cli -m "$MODEL" -f "$WAV" -l auto -oj -otxt -of "$OUT" --no-prints -bs 5 -sns $VAD_ARGS \
  ${PROMPT:+--prompt "This bank meeting discusses $PROMPT."} >/dev/null

# Post-ASR correction on the .txt (names + phrase map). The dual-stream path
# gets this via merge_streams; this covers the single-stream .txt the UI shows.
if [ -x "$WC/.venv/bin/python3" ]; then
  "$WC/.venv/bin/python3" "$WC/derive/meetings/correct.py" < "$OUT.txt" > "$OUT.txt.corr" 2>/dev/null \
    && mv "$OUT.txt.corr" "$OUT.txt" || rm -f "$OUT.txt.corr"
fi

# Collapse hallucination loops: >2 consecutive identical lines are never real
# speech ("Virtual transactions." x18) — keep the first two, drop the rest.
awk 'prev==$0 {c++; if (c<2) print; next} {c=0; prev=$0; print}' "$OUT.txt" > "$OUT.txt.dedup" \
  && mv "$OUT.txt.dedup" "$OUT.txt"

# Drop known Whisper silence-hallucination lines that leak past the gate.
# These YouTube/caption artifacts essentially never occur in a bank meeting;
# dropping URL/caption-boilerplate lines is safe (real URLs get garbled anyway).
grep -viE 'www\.|https?://|\.org(\.au)?|thanks for watching|please subscribe|subtitles? (by|provided)|amara\.org|for more information,? visit|fema\.gov' \
  "$OUT.txt" > "$OUT.txt.clean" || true
mv "$OUT.txt.clean" "$OUT.txt"

[ -f "$OUT.json" ] || { echo "ERROR: whisper produced no output for $AUDIO" >&2; exit 1; }
echo "OK: $OUT.json"

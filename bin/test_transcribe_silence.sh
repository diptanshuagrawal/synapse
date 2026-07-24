#!/usr/bin/env bash
# test_transcribe_silence.sh — regression guard for the in-person-meeting bug
# (2026-07-21): a silent 'them' (system-audio) stream made transcribe.sh exit
# non-zero, and transcripts_process.sh then discarded the whole meeting with
# "FAIL (dual-stream)". A silent stream MUST produce an empty transcript and
# exit 0 — never fail.
#
# Deterministic: exercises the SILENCE GATE, which returns before whisper ever
# runs, so no model inference and no flakiness. Skips cleanly (exit 0) when the
# offline toolchain (ffmpeg / whisper-cli / a model) is absent, so it's CI-safe.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TRANSCRIBE="$REPO/bin/transcribe.sh"

# Soft deps — same ones transcribe.sh itself requires before the gate.
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v whisper-cli >/dev/null 2>&1; then
  echo "SKIP: ffmpeg/whisper-cli not installed"; exit 0
fi
MODEL="${WHISPER_MODEL:-$HOME/.whisper-models/ggml-large-v3.bin}"
[ -f "$MODEL" ] || MODEL="$HOME/.whisper-models/ggml-large-v3-turbo.bin"
[ -f "$MODEL" ] || { echo "SKIP: no whisper model present"; exit 0; }

TMP="$(mktemp -d -t transcribe_silence_XXXX)"
trap 'rm -rf "$TMP"' EXIT

fail=0
check() {  # $1 = label ; runs a silent-stream case, asserts exit 0 + empty outputs
  local label="$1" wav="$2" prefix="$TMP/out"
  rm -f "$prefix.txt" "$prefix.json"
  local rc=0
  WHISPER_MODEL="$MODEL" bash "$TRANSCRIBE" "$wav" "$prefix" >/dev/null 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "  FAIL [$label]: transcribe.sh exited $rc on a silent stream (regression!)"; fail=1; return
  fi
  if [ ! -f "$prefix.json" ] || [ ! -f "$prefix.txt" ]; then
    echo "  FAIL [$label]: missing output ($prefix.json / .txt)"; fail=1; return
  fi
  if [ -s "$prefix.txt" ]; then
    echo "  FAIL [$label]: expected empty .txt, got content"; fail=1; return
  fi
  if ! grep -q '"transcription":\[\]' "$prefix.json"; then
    echo "  FAIL [$label]: expected empty transcription json, got: $(cat "$prefix.json")"; fail=1; return
  fi
  echo "  ok [$label]: exit 0, empty transcript"
}

echo "==> transcribe.sh silence gate"

# Case 1: PURE silence → ffmpeg reports "max_volume: -inf dB" (empty MAXVOL).
# This is the real in-person 'them' stream and the exact bug path.
SIL="$TMP/silent.wav"
ffmpeg -y -loglevel error -f lavfi -i anullsrc=r=16000:cl=mono -t 3 "$SIL"
check "pure-silence-inf" "$SIL"

# Case 2: near-silent but measurable (very low tone, well under the -40 dB floor)
# → the numeric "<-40 dB" branch of the gate.
QUIET="$TMP/quiet.wav"
ffmpeg -y -loglevel error -f lavfi -i "sine=frequency=440:duration=3,volume=-60dB" -ar 16000 -ac 1 "$QUIET"
check "near-silent-below-floor" "$QUIET"

if [ "$fail" -ne 0 ]; then echo "test_transcribe_silence: FAILED"; exit 1; fi
echo "test_transcribe_silence: PASS"

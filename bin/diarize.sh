#!/usr/bin/env bash
# diarize.sh — speaker diarization for single-mic meetings (Steno P5 overlay).
#
# Usage: diarize.sh <audio-file> <turns.json>
#   ffmpeg-downmix to 16 kHz mono → pyannote (in the ISOLATED diarize venv) →
#   writes <turns.json> = {"turns":[{"start_ms","end_ms","speaker"},...]}
#
# SOFT DEPENDENCY: exits non-zero (WITHOUT touching <turns.json>) when the
# diarize venv or the side-loaded models are absent — the sweep then falls back
# to the un-diarized transcript. A missing diarizer must never cost a meeting.
#
# The heavy torch inference sits INSIDE the sweep's existing power gates (AC +
# battery floor + not-mid-recording), so no extra gating is needed here.
set -uo pipefail

# launchd agents get a bare PATH — ffmpeg lives in homebrew.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

AUDIO="${1:?usage: diarize.sh <audio-file> <turns.json>}"
OUT="${2:?usage: diarize.sh <audio-file> <turns.json>}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WC="$REPO/work-context"
DIAR_HOME="${STENO_DIARIZE_HOME:-$HOME/.steno-diarize}"
PY="$DIAR_HOME/venv/bin/python3"

# Soft dependency: no diarize venv → skip (caller keeps today's behavior).
[ -x "$PY" ] || { echo "diarize: venv absent ($PY) — run bin/steno-diarize-setup.sh; skipping" >&2; exit 3; }
command -v ffmpeg >/dev/null || { echo "diarize: ffmpeg not found" >&2; exit 3; }
[ -f "$AUDIO" ] || { echo "diarize: audio not found: $AUDIO" >&2; exit 1; }

# whisper.cpp already downmixed for transcription; pyannote wants its own 16k
# mono wav. Keep the intermediate on the same filesystem and clean it up.
# mktemp creates the base file; ffmpeg writes to the .wav sibling. Clean BOTH
# (the bare base would otherwise leak — trap removed only the .wav).
WAV="$(mktemp -t diarize_XXXX).wav"
trap 'rm -f "$WAV" "${WAV%.wav}"' EXIT
ffmpeg -y -loglevel error -i "$AUDIO" -ar 16000 -ac 1 "$WAV" \
  || { echo "diarize: ffmpeg downmix failed for $AUDIO" >&2; exit 1; }

# Exit code propagates: diarize.py exits 3 (deps/models absent) or 4 (run failed)
# → the sweep's `if diarize.sh …` is false → fall back to the plain transcript.
# (Not `exec`, so the EXIT trap still cleans up the temp wav.)
"$PY" "$WC/derive/meetings/diarize.py" --wav "$WAV" --out "$OUT" || exit $?

# Match diarized clusters against the local voiceprint gallery → speakers.json
# sidecar (auto-suggested names + confidence, owner-confirmable in the Steno UI).
# Best-effort: an empty gallery just yields anonymous Speaker N. Never fails the
# diarization (the turns are already written and are what the sweep needs).
case "$OUT" in
  *.diar.json) SPK="${OUT%.diar.json}.speakers.json" ;;
  *)           SPK="${OUT%.json}.speakers.json" ;;
esac
"$PY" "$WC/derive/meetings/voice_gallery.py" resolve "$OUT" "$SPK" || true

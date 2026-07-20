#!/usr/bin/env bash
# transcripts_process.sh — sweep the meeting-audio inbox end to end.
#
#   inbox/*.m4a → transcribe (local whisper) → ingest into events.db
#   → audio + transcript + scratchpad archived to archive/YYYY-MM/
#
# Idempotent + safe to run from a routine every fire:
#   - lock dir prevents two concurrent sweeps double-processing a file
#   - files modified in the last 60s are skipped (may still be copying)
#   - only audio extensions are picked up; anything else is ignored
#   - a file that fails transcription/ingest stays in the inbox for the
#     next sweep (error reported, sweep continues)
#
# Meeting metadata comes from the filename + file mtime:
#   slug  = filename stem, sanitized (e.g. "standup 16 jul.m4a" → standup-16-jul)
#   start = file birth/modification time (recording end ≈ close enough for
#           same-day subject dating; pass-through to ingest as ISO)
set -uo pipefail

# launchd agents get a bare PATH — ffmpeg/whisper-cli live in homebrew.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WC="$REPO/work-context"
INBOX="$WC/transcripts/inbox"
ARCHIVE="$WC/transcripts/archive"
LOCK="$WC/transcripts/.process.lock"

mkdir -p "$INBOX" "$ARCHIVE"

# --- transcription pause toggle (owner: "pause transcription for now") ------
# While this flag exists, auto-sweeps no-op — recording a call costs no
# whisper/battery, the audio still lands in the inbox and shows on Steno
# (untranscribed). The Steno "Transcribe" button sets FORCE_TRANSCRIBE=1 to
# run one on demand. Unpause: rm the flag (or re-enable meeting-notes-auto).
if [ -f "$WC/transcripts/.transcription_paused" ] && [ "${FORCE_TRANSCRIBE:-}" != "1" ]; then
  echo "TRANSCRIBE PAUSED — skipping sweep ($WC/transcripts/.transcription_paused)"
  exit 0
fi

# --- power-aware: whisper on the GPU is the battery cost. Never drain for it.
# Off AC → defer; the file stays in the inbox (shows on Steno raw) and the
# periodic transcripts-watch drains it the moment you plug in. On AC, large-v3's
# energy is irrelevant. FORCE_TRANSCRIBE=1 (Steno "Transcribe") overrides. ------
if [ "${FORCE_TRANSCRIBE:-}" != "1" ]; then
  _ps=$(pmset -g ps 2>/dev/null)
  _pct=$(printf '%s' "$_ps" | grep -oE '[0-9]+%' | head -1 | tr -d '%')
  _floor="${TRANSCRIBE_BATTERY_FLOOR:-30}"
  if ! printf '%s' "$_ps" | grep -q "AC Power"; then
    echo "ON BATTERY — deferring sweep until on AC power"
    exit 0
  fi
  # Even on AC: a heavy GPU job at very low charge can out-draw a weak charger
  # (net drain while "charging"). Hold off below the floor.
  if [ -n "$_pct" ] && [ "$_pct" -lt "$_floor" ]; then
    echo "LOW BATTERY (${_pct}% < ${_floor}% floor) despite AC — deferring sweep"
    exit 0
  fi
fi

# --- meeting-aware: don't fight a live recording for the GPU (bad for both the
# meeting and battery). Defer until the meeting ends; next sweep picks it up. ---
if [ "${FORCE_TRANSCRIBE:-}" != "1" ] \
   && kill -0 "$(cut -d' ' -f1 "$WC/transcripts/.capture/pid" 2>/dev/null)" 2>/dev/null; then
  echo "RECORDING ACTIVE — deferring sweep until the meeting ends"
  exit 0
fi

# --- single-flight lock (mkdir is atomic; stale >45 min self-expires) -------
if ! mkdir "$LOCK" 2>/dev/null; then
  age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ))
  if [ "$age" -lt 2700 ]; then echo "LOCKED: another sweep in progress — exit"; exit 0; fi
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 0
fi
trap 'rm -rf "$LOCK"' EXIT

# --- resolve a python that can import yaml (mirrors refresh-skeletons fix) --
PY=""
for cand in python3 "$WC/.venv/bin/python3" "$REPO/.venv/bin/python3"; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import yaml' 2>/dev/null; then PY="$cand"; break; fi
done
[ -n "$PY" ] || { echo "ERROR: no python3 with yaml available"; exit 1; }

NOW=$(date +%s)
processed=0 failed=0 skipped=0

shopt -s nullglob nocaseglob
for audio in "$INBOX"/*.{m4a,wav,mp3,mp4,aac,aiff,webm,ogg,flac}; do
  name="$(basename "$audio")"
  stem="${name%.*}"

  # Speaker-stream halves are processed WITH their m4a, never standalone.
  case "$name" in *.me.wav|*.them.wav) continue ;; esac

  # Skip files still being written (AirDrop/export in flight).
  mtime=$(stat -f %m "$audio")
  if [ $(( NOW - mtime )) -lt 60 ]; then
    echo "SKIP (too fresh): $name"; skipped=$((skipped+1)); continue
  fi

  slug=$(echo "$stem" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g')
  [ -n "$slug" ] || slug="meeting"
  start=$(date -u -r "$mtime" +%Y-%m-%dT%H:%M:%SZ)
  month=$(TZ=Asia/Kolkata date -r "$mtime" +%Y-%m)
  dest="$ARCHIVE/$month"
  mkdir -p "$dest"
  prefix="$dest/$(TZ=Asia/Kolkata date -r "$mtime" +%Y-%m-%d)-$slug"

  echo "PROCESS: $name → $slug"
  # Dual-stream pair (mic + system audio captured separately) → transcribe
  # each and merge with Me:/Them: speaker labels; else single-file path.
  me_wav="$INBOX/$stem.me.wav"; them_wav="$INBOX/$stem.them.wav"
  if [ -s "$me_wav" ] && [ -s "$them_wav" ]; then
    if bash "$REPO/bin/transcribe.sh" "$me_wav" "$prefix.me" \
       && bash "$REPO/bin/transcribe.sh" "$them_wav" "$prefix.them" \
       && "$PY" "$WC/derive/meetings/merge_streams.py" \
            --me "$prefix.me.json" --them "$prefix.them.json" --out "$prefix"; then
      rm -f "$me_wav" "$them_wav"   # m4a remains the audio archive
    else
      echo "FAIL (dual-stream): $name — left in inbox"; failed=$((failed+1)); continue
    fi
  elif ! bash "$REPO/bin/transcribe.sh" "$audio" "$prefix"; then
    echo "FAIL (transcribe): $name — left in inbox"; failed=$((failed+1)); continue
  fi
  if ! "$PY" "$WC/derive/meetings/ingest_transcript.py" \
        --json "$prefix.json" --slug "$slug" --start "$start"; then
    echo "FAIL (ingest): $name — left in inbox"; failed=$((failed+1)); continue
  fi

  mv "$audio" "$dest/"
  # Scratchpad + context links (owner's during-meeting attachments) ride along.
  [ -f "$INBOX/$stem.notes.md" ] && mv "$INBOX/$stem.notes.md" "$prefix.notes.md"
  [ -f "$INBOX/$stem.links" ] && mv "$INBOX/$stem.links" "$prefix.links"
  processed=$((processed+1))
done

echo "SWEEP DONE: processed=$processed failed=$failed skipped=$skipped"
# Failures leave files in the inbox for retry; don't fail the caller.
exit 0

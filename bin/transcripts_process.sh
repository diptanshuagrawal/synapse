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
# Per-machine config (gitignored) — sets TRANSCRIBE_LANG so the auto-sweep pins
# the language (Hinglish → hi) instead of whisper's unreliable auto-detect. The
# export flows to the transcribe.sh children below.
[ -f "$WC/config/steno.local.sh" ] && . "$WC/config/steno.local.sh"
INBOX="$WC/transcripts/inbox"
ARCHIVE="$WC/transcripts/archive"
LOCK="$WC/transcripts/.process.lock"

mkdir -p "$INBOX" "$ARCHIVE"

# --- detach from launchd's lifecycle (fixes recurring `Terminated: 15`) ------
# launchd tracks this job's WHOLE process group. On a long recording the sweep
# runs far past the agent's 10-min StartInterval (57-min audio × dual-stream ×
# the -mc 0 loop-guard retry), AND the sweep mutates the WatchPaths-watched
# inbox as it runs (rm of the .me/.them wavs, mv of the m4a out). Either one
# re-fires the agent; launchd then tears down / relaunches the running job and
# SIGTERMs its process group — killing whisper/ffmpeg mid-file (the ffmpeg child
# itself gets `Terminated: 15`, not the shell). Running detached by hand escapes
# that group and survives — the tell that the killer is launchd, not the code.
#
# Fix: the launchd-invoked process becomes a thin kicker that re-execs the real
# sweep as a detached background job and exits 0 immediately. launchd's job then
# completes at once (nothing long-lived left to signal); the detached copy holds
# the single-flight lock and runs to completion, immune to any relaunch/kill.
# setsid (a clean new session) is used when present; macOS ships without it, so
# the agent plist's AbandonProcessGroup=true is what actually stops launchd from
# reaping the backgrounded group when the kicker exits. FORCE_TRANSCRIBE (Steno's
# on-demand "Transcribe" button) stays synchronous — it isn't under launchd and
# its caller may wait on the result.
if [ "${FORCE_TRANSCRIBE:-}" != "1" ] && [ "${TRANSCRIPTS_DETACHED:-}" != "1" ]; then
  _log="$WC/transcripts/.capture/sweep.log"; mkdir -p "$WC/transcripts/.capture"
  if command -v setsid >/dev/null 2>&1; then
    TRANSCRIPTS_DETACHED=1 setsid nohup "$0" "$@" >>"$_log" 2>&1 </dev/null &
  else
    TRANSCRIPTS_DETACHED=1 nohup "$0" "$@" >>"$_log" 2>&1 </dev/null &
  fi
  disown 2>/dev/null || true
  echo "transcripts_process: detached sweep started (pid $!) → $_log"
  exit 0
fi

# --- transcription pause toggle (owner: "pause transcription for now") ------
# While this flag exists, auto-sweeps no-op — recording a call costs no
# whisper/battery, the audio still lands in the inbox and shows on Steno
# (untranscribed). The Steno "Transcribe" button sets FORCE_TRANSCRIBE=1 to
# run one on demand. Unpause: rm the flag (or re-enable meeting-notes-auto).
if [ -f "$WC/transcripts/.transcription_paused" ] && [ "${FORCE_TRANSCRIBE:-}" != "1" ]; then
  echo "TRANSCRIBE PAUSED — skipping sweep ($WC/transcripts/.transcription_paused)"
  exit 0
fi

# --- power-aware: whisper on the GPU is the battery cost. large-v3 is the drain;
# turbo is ~4-6x faster = a fraction of the energy. So instead of waiting for AC
# (notes sat in the inbox until you plugged in), transcribe RIGHT AFTER the
# meeting even on battery — just with turbo, and only above a low-charge floor.
# On AC → large-v3 (energy irrelevant). FORCE_TRANSCRIBE=1 (Steno "Transcribe")
# overrides everything. --------------------------------------------------------
if [ "${FORCE_TRANSCRIBE:-}" != "1" ]; then
  _ps=$(pmset -g ps 2>/dev/null)
  _pct=$(printf '%s' "$_ps" | grep -oE '[0-9]+%' | head -1 | tr -d '%')
  _floor="${TRANSCRIBE_BATTERY_FLOOR:-30}"
  _batt_floor="${TRANSCRIBE_BATTERY_TURBO_FLOOR:-20}"
  if ! printf '%s' "$_ps" | grep -q "AC Power"; then
    # On battery: below the turbo floor the charge is genuinely low → defer to AC
    # (file stays in the inbox; transcripts-watch drains it the moment you plug in).
    if [ -n "$_pct" ] && [ "$_pct" -lt "$_batt_floor" ]; then
      echo "ON BATTERY, LOW (${_pct}% < ${_batt_floor}% floor) — deferring sweep until on AC power"
      exit 0
    fi
    # Enough charge → transcribe now with turbo (cheap). Signals the model block.
    export TRANSCRIBE_ON_BATTERY=1
    echo "ON BATTERY (${_pct:-?}% >= ${_batt_floor}% floor) — transcribing now with turbo (low-energy)"
  elif [ -n "$_pct" ] && [ "$_pct" -lt "$_floor" ]; then
    # Even on AC: a heavy GPU job at very low charge can out-draw a weak charger
    # (net drain while "charging"). Hold off below the floor.
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

# --- single-flight lock (mkdir is atomic; stale >3 h self-expires) ----------
# TTL is 3 h (was 45 min): a real 60-min recording, dual-stream, with the -mc 0
# loop-guard retry can legitimately run well over an hour. At 45 min a still-live
# sweep looked "stale" to a fresh fire, which rm'd the lock and started a SECOND,
# competing sweep. 3 h covers the max realistic recording so only ONE sweep ever
# runs. (Kickers relaunched by launchd detach, hit this valid lock, and exit.)
if ! mkdir "$LOCK" 2>/dev/null; then
  age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ))
  if [ "$age" -lt 10800 ]; then echo "LOCKED: another sweep in progress — exit"; exit 0; fi
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 0
fi
trap 'rm -rf "$LOCK"' EXIT
# A killed sweep must still drop the lock. An untrapped SIGTERM/SIGINT kills the
# shell WITHOUT firing the EXIT trap → stale lock blocks the next sweep until the
# 3-h self-expiry. Convert the signal into a normal exit so EXIT cleans up.
trap 'exit 143' TERM
trap 'exit 130' INT

# --- resolve a python that can import yaml (mirrors refresh-skeletons fix) --
PY=""
for cand in python3 "$WC/.venv/bin/python3" "$REPO/.venv/bin/python3"; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import yaml' 2>/dev/null; then PY="$cand"; break; fi
done
[ -n "$PY" ] || { echo "ERROR: no python3 with yaml available"; exit 1; }

NOW=$(date +%s)
processed=0 failed=0 skipped=0

# --- PHASE 1: build a PRIORITY QUEUE (order, not parallelism) --------------
# Blind glob (alphabetical) order once let a single 57-min recording sit at the
# front and block six short meetings behind it for 30+ min. whisper is GPU-bound
# so we still transcribe ONE at a time — the win is the ORDER we drain in:
#   STARRED first  → owner-pinned meetings never wait.
#   then SHORTEST first (duration asc) → many small meetings clear in minutes;
#        the one giant file goes LAST instead of blocking everything.
#   newest-first as a tiebreak → recent meetings usually matter more.
# Freshness + speaker-half skips happen HERE so they don't inflate the backlog
# count that drives model tiering below.
shopt -s nullglob nocaseglob
_queue_keys=""   # one sortable line per item: <star_rank>\t<dur>\t<neg_mtime>\t<path>
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

  # Duration (seconds) is the ordering key. ffprobe is authoritative; if it
  # can't read the container, estimate from size (~16 KB/s for compressed
  # meeting audio) so a probe-less file still sorts roughly right.
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$audio" 2>/dev/null | cut -d. -f1)
  if ! [ "${dur:-}" -ge 0 ] 2>/dev/null; then
    bytes=$(stat -f %z "$audio" 2>/dev/null || echo 0); dur=$(( bytes / 16000 ))
  fi

  # Starred? Mirror meet_retention._meeting_stem: id = <IST-date-of-mtime>-<slug>,
  # star sidecar = management/meetings/<id>.star (the /api/star pin).
  slug=$(echo "$stem" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g')
  [ -n "$slug" ] || slug="meeting"
  mid="$(TZ=Asia/Kolkata date -r "$mtime" +%Y-%m-%d)-$slug"
  star_rank=1
  [ -f "$REPO/management/meetings/$mid.star" ] && star_rank=0

  _queue_keys+="$star_rank	$dur	$((-mtime))	$audio
"
done

# star_rank asc (0=starred first), dur asc (shortest first), neg_mtime asc (newest first).
QUEUE=()
while IFS=$'\t' read -r _sr _du _nm _path; do
  [ -n "$_path" ] && QUEUE+=("$_path")
done < <(printf '%s' "$_queue_keys" | sort -t$'\t' -k1,1n -k2,2n -k3,3n)
qcount=${#QUEUE[@]}

# --- catch-up model tiering ------------------------------------------------
# Backlog of 1 (single fresh meeting) → large-v3, the owner's 2026-07-18 A/B
# pick (best accuracy, quality matters most here). Backlog > threshold →
# large-v3-turbo (~4-6x faster) so the whole queue — even a loop-guard -mc 0
# retry — drains fast. All three knobs are env-overridable; an explicit
# WHISPER_MODEL from the caller (e.g. Steno's Transcribe button) wins outright.
THRESH="${TURBO_BACKLOG_THRESHOLD:-1}"
MODEL_QUALITY="${WHISPER_MODEL_QUALITY:-$HOME/.whisper-models/ggml-large-v3.bin}"
MODEL_TURBO="${WHISPER_MODEL_TURBO:-$HOME/.whisper-models/ggml-large-v3-turbo.bin}"
if [ -n "${WHISPER_MODEL:-}" ]; then
  _model_why="caller override"
elif [ "${TRANSCRIBE_ON_BATTERY:-}" = "1" ]; then
  export WHISPER_MODEL="$MODEL_TURBO"; _model_why="turbo (on battery — low-energy)"
elif [ "$qcount" -gt "$THRESH" ]; then
  export WHISPER_MODEL="$MODEL_TURBO"; _model_why="turbo (backlog $qcount > $THRESH)"
else
  export WHISPER_MODEL="$MODEL_QUALITY"; _model_why="quality (backlog $qcount <= $THRESH)"
fi

echo "QUEUE: $qcount file(s), model=$(basename "${WHISPER_MODEL:-large-v3}") [$_model_why]"
_i=0; for _q in ${QUEUE[@]+"${QUEUE[@]}"}; do _i=$((_i+1)); echo "  $_i. $(basename "$_q")"; done

# Dry-run: print the plan (order + model) and stop, so "why is X still pending"
# is answerable without transcribing anything.
if [ "${TRANSCRIPTS_DRY_RUN:-}" = "1" ]; then
  echo "DRY RUN — not transcribing"; exit 0
fi

# --- PHASE 2: drain the queue in priority order ----------------------------
# ${arr[@]+…} guard: an empty array under `set -u` is an unbound-var error on
# macOS bash 3.2 — an empty inbox must be a clean no-op, not a crash.
for audio in ${QUEUE[@]+"${QUEUE[@]}"}; do
  name="$(basename "$audio")"
  stem="${name%.*}"
  mtime=$(stat -f %m "$audio")

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
    # The 'me' (mic) stream is the meeting: if IT fails to transcribe there's no
    # content to save, so that's the only hard gate. The 'them' (system-audio)
    # stream is EXPECTED to be silent/empty for an in-person meeting (nobody
    # dialed in) — its transcribe must NEVER discard the meeting. If it fails or
    # returns empty for ANY reason, synthesize an empty them.json and fall through
    # to the in-person path below. (Historically an empty 'them' returning
    # non-zero triggered FAIL (dual-stream) and threw the whole meeting away.)
    if ! bash "$REPO/bin/transcribe.sh" "$me_wav" "$prefix.me"; then
      echo "FAIL (dual-stream, me): $name — left in inbox"; failed=$((failed+1)); continue
    fi
    if ! bash "$REPO/bin/transcribe.sh" "$them_wav" "$prefix.them"; then
      echo "WARN (them transcribe failed — treating as in-person): $name" >&2
      : > "$prefix.them.txt"; printf '{"transcription":[]}' > "$prefix.them.json"
    fi
    # IN-PERSON: nobody dialed in → the 'them' (system-audio) stream is silent,
    # so it carries an empty transcript. Me:/Them: then gives no separation
    # (everyone is on the ONE mic) — diarize the mic instead (Speaker 1/2/…).
    # A real CALL has speech on 'them' → keep ground-truth Me:/Them:. Diarization
    # is a SOFT overlay: if the diarizer is unavailable the in-person branch
    # falls back to the plain Me:/Them: merge (today).
    _call_path=0
    if grep -q '"text"' "$prefix.them.json" 2>/dev/null; then
      _call_path=1
      # The far-side ('them') is CLEAN digital system-audio (no room echo) with
      # every remote/in-room voice mixed in — the clean case for diarization.
      # Split it into `Them · Speaker N` (keeping `Me:` for the owner) so two
      # people on the call, or a room on one Teams mic, don't collapse into one
      # `Them`. SOFT overlay: diarizer unavailable (exit 3/4) → flat `Them:`
      # (today). Diarize BEFORE the wavs are rm'd below. (must run before merge)
      _them_diar=()
      if bash "$REPO/bin/diarize.sh" "$them_wav" "$prefix.them.diar.json"; then
        _them_diar=(--them-diarize "$prefix.them.diar.json")
      fi
      "$PY" "$WC/derive/meetings/merge_streams.py" \
        --me "$prefix.me.json" --them "$prefix.them.json" \
        ${_them_diar[@]+"${_them_diar[@]}"} --out "$prefix"; merge_rc=$?
    elif bash "$REPO/bin/diarize.sh" "$me_wav" "$prefix.diar.json"; then
      "$PY" "$WC/derive/meetings/merge_streams.py" \
        --single "$prefix.me.json" --diarize "$prefix.diar.json" --out "$prefix"; merge_rc=$?
    else
      "$PY" "$WC/derive/meetings/merge_streams.py" \
        --me "$prefix.me.json" --them "$prefix.them.json" --out "$prefix"; merge_rc=$?
    fi
    if [ "$merge_rc" -ne 0 ]; then
      echo "FAIL (merge): $name — left in inbox"; failed=$((failed+1)); continue
    fi
    # On-speakers guard: on the CALL path, if the system-audio ('them') stream
    # captured far less than the mic ('me'), the two sides likely weren't cleanly
    # separated — you were on laptop SPEAKERS (not headphones), so the mic picked
    # up BOTH voices while the system tap barely registered. Me:/Them: labels are
    # then unreliable (everything collapses onto 'Me'). Flag it honestly at the
    # top of the transcript instead of presenting wrong attribution as fact.
    # Headphones give a clean split; diarization can't recover it from a mono mix.
    if [ "$_call_path" = 1 ]; then
      _me_n=$(grep -cvE '^[[:space:]]*$' "$prefix.me.txt" 2>/dev/null || true); _me_n=${_me_n:-0}
      _them_n=$(grep -cvE '^[[:space:]]*$' "$prefix.them.txt" 2>/dev/null || true); _them_n=${_them_n:-0}
      if [ "$_them_n" -gt 0 ] && [ "$_me_n" -ge $((_them_n * 6)) ]; then
        _warn='⚠️ Speaker labels may be unreliable: the "Them" side captured far less audio than your mic — you were likely on speakers (not headphones), so the mic caught both voices. Use headphones for a clean Me/Them split.'
        # Read the transcript into a var BEFORE the redirect: `> file` truncates
        # before `$(cat file)` runs, which would wipe the transcript.
        _body=$(cat "$prefix.txt" 2>/dev/null)
        printf '%s\n\n%s\n' "$_warn" "$_body" > "$prefix.txt"
        echo "WARN (on-speakers: them=${_them_n} me=${_me_n} lines — flagged unreliable labels): $name" >&2
      fi
    fi
    # Keep the per-stream audio so the dual-stream pipeline can be RE-RUN later
    # (better model / re-diarization) — deleting it immediately made a re-run
    # impossible. Archive each stream as ~64k AAC (raw wav is huge; re-transcribe
    # downmixes to 16k mono anyway), then drop the raw wav. meet_retention.py
    # prunes these with the mixed m4a at the retention window (star-exempt).
    for _s in me them; do
      _w="$INBOX/$stem.$_s.wav"
      [ -s "$_w" ] || continue
      if ffmpeg -y -loglevel error -i "$_w" -c:a aac -b:a 64k "$prefix.$_s.m4a" 2>/dev/null; then
        rm -f "$_w"
      else
        echo "WARN: could not archive $_s stream (kept raw wav): $name" >&2
      fi
    done
  elif bash "$REPO/bin/transcribe.sh" "$audio" "$prefix"; then
    # Lone file (AirDropped phone recording / rescued mono) — always single-mic,
    # so diarize best-effort and relabel with Speaker N. If the diarizer is
    # unavailable we KEEP transcribe.sh's output untouched (today's behavior).
    if bash "$REPO/bin/diarize.sh" "$audio" "$prefix.diar.json"; then
      "$PY" "$WC/derive/meetings/merge_streams.py" \
        --single "$prefix.json" --diarize "$prefix.diar.json" --out "$prefix" \
        || echo "WARN (diarize relabel failed): $name — using plain transcript" >&2
    fi
  else
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

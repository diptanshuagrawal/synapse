#!/usr/bin/env bash
# meet_watch.sh — call-state-driven auto-recorder (the actual Granola model).
#
# Persistent daemon (launchd KeepAlive). Poll loop every 5s:
#
#   START  a meeting app holds the microphone for ~15s → recording starts.
#          The calendar does NOT decide this — it only provides the label
#          (matching event → slug; none → "adhoc"). So overruns, overlaps,
#          late starts, and off-calendar calls are all the same case.
#   STOP   multi-signal, per Granola's documented behavior:
#            s1  call app released the mic for ~45s        (primary)
#            s2  no speech-level audio for 15 min          (inactivity)
#            s3  past calendar end +5min AND quiet ≥3min   (secondary, only
#                when the recording was labeled from a calendar event)
#            s4  3h hard cap                               (backstop)
#
# Respects manual control: recordings started by hand are never touched, and
# a manual stop mid-call suppresses re-start until the mic is fully released.
# Known merge (Granola merges here too): back-to-back calls where the app
# never releases the mic become one recording under the first label.
#
# Master switch: `auto_record: true` under `calendar:` in config/sources.yaml.
set -u

# launchd gives daemons a bare PATH — clang/ffmpeg live in homebrew.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WC="$REPO/work-context"
CAP="$WC/transcripts/.capture"
PIDF="$CAP/pid"
AUTOF="$CAP/auto"            # "uid end_epoch" for the daemon-started recording
MR="$REPO/bin/meet-record"
MW="$CAP/mic_watch"
LOG="$CAP/watch.log"

POLL="${MEET_WATCH_POLL:-5}"
START_POLLS="${MEET_WATCH_START_POLLS:-2}"     # ~10s of sustained mic use
RELEASE_POLLS="${MEET_WATCH_RELEASE_POLLS:-5}" # ~25s of mic released (was 45s — felt too long; still debounces transient drops + merges gap-free back-to-back calls)
# Apps whose mic use means "you are in a call" (browsers cover Meet).
APPS="${MEET_WATCH_APPS:-MSTeams|Microsoft Teams|Teams|Slack|zoom.us|Google Chrome|Google Chrome Helper|Safari|Arc|firefox|FaceTime|Webex}"

mkdir -p "$CAP"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# Self-build the mic prober if missing/stale.
if [ ! -x "$MW" ] || [ "$REPO/bin/mic_watch.m" -nt "$MW" ]; then
  clang -fobjc-arc -O2 "$REPO/bin/mic_watch.m" -o "$MW" \
    -framework CoreAudio -framework Foundation || { log "FATAL mic_watch build failed"; exit 1; }
fi

PY=""
for cand in "$WC/.venv/bin/python3" python3; do
  command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import yaml, dateutil' 2>/dev/null && PY="$cand" && break
done

is_recording() { [ -f "$PIDF" ] && kill -0 "$(cut -d' ' -f1 "$PIDF" 2>/dev/null)" 2>/dev/null; }

oncnt=0
offcnt=0
log "daemon up (poll=${POLL}s apps=$APPS)"

while true; do
  sleep "$POLL"
  grep -Eq '^\s*auto_record:\s*true' "$WC/config/sources.yaml" 2>/dev/null || continue

  inuse="$("$MW" 2>/dev/null | grep -v 'meet-record-capture' | grep -E "$APPS" | head -3)"

  if is_recording; then
    oncnt=0
    [ -f "$AUTOF" ] || continue          # manual recording — hands off
    read -r auto_uid auto_end < "$AUTOF"
    if [ -n "$inuse" ]; then offcnt=0; else offcnt=$((offcnt+1)); fi
    NOW=$(date +%s)
    started=$(cut -d' ' -f3 "$PIDF")
    voice_ts=$(stat -f %m "$CAP/voice_active" 2>/dev/null || echo "$started")
    quiet=$(( NOW - voice_ts ))
    reason=""
    if   [ "$offcnt" -ge "$RELEASE_POLLS" ]; then reason="call app released mic"
    elif [ "$quiet" -ge 900 ]; then reason="15min audio inactivity"
    elif [ "${auto_end:-0}" -gt 0 ] && [ "$NOW" -gt $(( auto_end + 300 )) ] && [ "$quiet" -ge 180 ]; then reason="past calendar end + quiet"
    elif [ $(( NOW - started )) -gt 10800 ]; then reason="3h cap"
    fi
    if [ -n "$reason" ]; then
      log "STOP ($reason)"
      "$MR" stop >>"$LOG" 2>&1
      rm -f "$AUTOF" "$CAP/voice_active"
      offcnt=0
    fi
    continue
  fi

  # Not recording. A leftover AUTOF means the owner manually stopped an
  # auto-recording mid-call — stay suppressed until the mic is released.
  if [ -f "$AUTOF" ]; then
    [ -z "$inuse" ] && rm -f "$AUTOF"
    continue
  fi

  # NUDGE — a calendar meeting is live but no call app is on the mic (you
  # likely joined IN PERSON). Auto-record can't fire; remind once per event so
  # it isn't silently missed. Skipped if a mic app is active (that path
  # auto-records) or the meeting is nearly over.
  if [ -z "$inuse" ] && [ -n "$PY" ]; then
    nstate="$("$PY" "$WC/derive/meetings/calendar_feed.py" now 2>/dev/null | head -1)" || nstate="NONE"
    case "$nstate" in
      ACTIVE\|*)
        IFS='|' read -r _ nslug nend nuid ntitle <<< "$nstate"
        # Persistent nudge for the Steno banner — the one-shot notification is
        # easy to miss (DnD / perms; an on-calendar 1-1 recorded mislabeled
        # because of exactly this). Refresh it every poll while the meeting is live and
        # unrecorded; cleared below once recording starts or the meeting ends.
        if [ "${nend:-0}" -gt "$NOW" ]; then
          printf '%s|%s\n' "$ntitle" "$nend" > "$CAP/nudge"
        fi
        if [ "${nend:-0}" -gt $(( NOW + 300 )) ] && ! grep -q "^$nuid$" "$CAP/nudged" 2>/dev/null; then
          echo "$nuid" >> "$CAP/nudged"
          osascript -e "display notification \"Not recording — click to start in Steno\" with title \"Meeting now: $ntitle\" sound name \"\"" 2>/dev/null || true
          log "NUDGE calendar meeting live, not recording: $ntitle"
        fi ;;
      *) rm -f "$CAP/nudge" 2>/dev/null || true ;;
    esac
  else
    rm -f "$CAP/nudge" 2>/dev/null || true
  fi

  # PRE-CALL NUDGE — remind BEFORE a scheduled meeting starts so you can arm
  # Steno (most valuable for IN-PERSON meetings the daemon can't auto-detect;
  # the live nudge above only fires once the meeting is already underway).
  # Fires the macOS notification once per event (dedup in .prenudged) and keeps
  # a .prenudge banner file refreshed for the Steno UI (a notification alone is
  # easy to miss — same lesson as the live nudge). Clears once the meeting
  # starts (the live path / auto-record takes over) or the window passes.
  if [ -n "$PY" ]; then
    pnow=$(date +%s)
    soon="$("$PY" "$WC/derive/meetings/calendar_feed.py" soon "${MEET_PRENUDGE_MIN:-5}" 2>/dev/null | head -1)" || soon="NONE"
    case "$soon" in
      SOON\|*)
        IFS='|' read -r _ pslug pstart pend puid pmins ptitle <<< "$soon"
        if [ "${pstart:-0}" -gt "$pnow" ]; then
          printf '%s|%s|%s\n' "$ptitle" "$pstart" "$pmins" > "$CAP/prenudge"
          if ! grep -q "^$puid$" "$CAP/prenudged" 2>/dev/null; then
            echo "$puid" >> "$CAP/prenudged"
            osascript -e "display notification \"Starts in ${pmins}m — arm Steno to record\" with title \"Upcoming: $ptitle\" sound name \"\"" 2>/dev/null || true
            log "PRENUDGE upcoming meeting in ${pmins}m: $ptitle"
          fi
        else
          rm -f "$CAP/prenudge" 2>/dev/null || true
        fi ;;
      *) rm -f "$CAP/prenudge" 2>/dev/null || true ;;
    esac
  fi

  offcnt=0
  if [ -n "$inuse" ]; then oncnt=$((oncnt+1)); else oncnt=0; fi
  if [ "$oncnt" -ge "$START_POLLS" ]; then
    oncnt=0
    # Off-calendar fallback label from the app holding the mic — a Slack
    # huddle should say so, not "adhoc". (/meeting-notes infers WHO from the
    # transcript and titles the note "Huddle with <name>".)
    app="$(echo "$inuse" | head -1 | cut -d' ' -f2-)"
    case "$app" in
      *Slack*)                       slug="slack-huddle" ;;
      *Teams*|*MSTeams*)             slug="teams-call" ;;
      *Chrome*|*Safari*|*Arc*|*fire*) slug="browser-call" ;;
      *FaceTime*)                    slug="facetime-call" ;;
      *)                             slug="adhoc" ;;
    esac
    end=0; uid="$slug-$(date +%s)"
    if [ -n "$PY" ]; then
      state="$("$PY" "$WC/derive/meetings/calendar_feed.py" now 2>/dev/null)" || state="NONE"
      case "$state" in
        ACTIVE\|*) IFS='|' read -r _ slug end uid _ <<< "$state" ;;
      esac
    fi
    log "START call detected ($(echo "$inuse" | head -1 | cut -d' ' -f2-)) label=$slug"
    if "$MR" start "$slug" >>"$LOG" 2>&1; then
      echo "$uid $end" > "$AUTOF"
    fi
  fi
done

#!/usr/bin/env bash
# threads_sweep_once.sh — shared ONCE-PER-DAY pre-digest slack thread sweep.
#
# Both daily-standup (06:00) and track-work-ticketize (06:15) need yesterday's late
# thread-replies in events.db before they gather. The sweep is the expensive part
# (~minutes across the team's active channels); the gather is cheap and read-only, and
# events.db is the shared cache. So: whichever routine fires FIRST runs the sweep and
# stamps a dated marker; the SECOND sees today's marker and SKIPS — it just re-gathers
# against the already-fresh DB. Idempotent: a missing/old marker simply re-sweeps.
#
# Best-effort: NEVER blocks the calling routine (always exits 0). The marker is stamped
# only on a clean sweep, so a failed sweep is retried by the other routine / next fire.
#
# Self-locating (bin/ is at the repo root, alongside standup_gather.py) — no path
# templating needed, so the same file works for every clone.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WC="$REPO/work-context"
MARK="$WC/state/last_threads_sweep.date"
TODAY="$(TZ=Asia/Kolkata date +%F)"

if [ "$(cat "$MARK" 2>/dev/null)" = "$TODAY" ]; then
  echo "threads-sweep: already done today ($TODAY) — skip (events.db already fresh)"
  exit 0
fi

cd "$WC" 2>/dev/null || { echo "threads-sweep: work-context missing — skip"; exit 0; }
if PYTHONPATH="$WC" "$WC/.venv/bin/python" -m ingest.slack_ingest_app --threads-sweep; then
  echo "$TODAY" > "$MARK"
  echo "threads-sweep: done + stamped $TODAY"
else
  echo "threads-sweep: FAILED — proceeding without it (freshness may lag; the other routine retries)"
fi
exit 0

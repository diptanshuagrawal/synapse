"""Shared run-health helpers: flag ingest runs that overrun their fire interval.

Imported by bin/cron-status.sh (terminal) and bin/dashboard.py (web) so the
overrun thresholds + verdict logic live in exactly ONE place.

Overrun = a single ingest run takes longer than the gap until its next
scheduled fire. When that happens launchd SIGTERMs the still-running fire as
the next interval lands (this is what killed slack sweeps before the
2026-06-03 30->60min widening). We flag it BEFORE it collides:
  - duration >= 80% of interval  -> WARN  (near-limit, yellow)
  - duration >= 100% of interval -> FAIL  (overrun, red)
"""
from __future__ import annotations
import subprocess
from datetime import datetime

WARN_RATIO = 0.80   # >= 80% of interval -> near-limit
FAIL_RATIO = 1.00   # >= 100% of interval -> overrun (collides with next fire)

# Main ingest script per source — used to confirm a run is genuinely live
# (pgrep) before flagging an "in-flight overrun". Without this gate a run
# whose Done. line got mis-attributed during concurrent-fire log interleaving
# leaves its start spuriously "open" and would false-flag as running forever.
_INGEST_SCRIPT = {
    "github":     "ingest/github.py",
    "jira":       "ingest/jira.py",
    "confluence": "ingest/confluence.py",
    "slack":      "ingest/slack_ingest_app.py",
}


def source_running(src: str) -> bool:
    """True if the source's ingest process is actually live (pgrep -f)."""
    pat = _INGEST_SCRIPT.get(src)
    if not pat:
        return False
    try:
        r = subprocess.run(["pgrep", "-f", pat],
                           capture_output=True, text=True, timeout=3)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def fire_interval_min(fire_minutes: list[int]) -> int | None:
    """Tightest intra-day gap (minutes) between consecutive scheduled fires.

    That gap is the window a run must finish within. Overnight gaps (last fire
    of the day -> first of the next) are ignored — they carry ample slack.
    Returns None for fewer than 2 fires (a once-a-day job can't overrun).
    """
    fm = sorted(set(fire_minutes))
    if len(fm) < 2:
        return None
    gaps = [b - a for a, b in zip(fm, fm[1:]) if b - a > 0]
    return min(gaps) if gaps else None


def parse_log_ts(ts_str: str) -> datetime | None:
    """Parse a 'YYYY-MM-DD HH:MM:SS[,ms]' ingest-log timestamp (naive, IST-local)."""
    if not ts_str:
        return None
    try:
        return datetime.strptime(ts_str.strip()[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def run_duration_min(start_ts: str, done_ts: str) -> float | None:
    """Minutes between start and done log timestamps; None if unparseable/negative."""
    s, d = parse_log_ts(start_ts), parse_log_ts(done_ts)
    if not s or not d:
        return None
    secs = (d - s).total_seconds()
    return secs / 60 if secs >= 0 else None


def inflight_duration_min(start_ts: str, now: datetime | None = None) -> float | None:
    """Minutes a still-running fire has been going (now - start)."""
    s = parse_log_ts(start_ts)
    if not s:
        return None
    now = now or datetime.now()
    secs = (now - s).total_seconds()
    return secs / 60 if secs >= 0 else None


def overrun_verdict(duration_min: float | None,
                    interval_min: int | None,
                    in_flight: bool = False) -> dict | None:
    """Classify a run's duration against its fire interval.

    Returns None when fine / not computable, else a dict:
      {"level": "warn"|"fail", "symbol": "!"|"x", "in_flight": bool,
       "duration_min": int, "interval_min": int, "pct": int, "label": str}
    """
    if duration_min is None or not interval_min:
        return None
    ratio = duration_min / interval_min
    if ratio < WARN_RATIO:
        return None
    dur = round(duration_min)
    pct = round(ratio * 100)
    scope = "running " if in_flight else ""
    if ratio >= FAIL_RATIO:
        return {"level": "fail", "symbol": "x", "in_flight": in_flight,
                "duration_min": dur, "interval_min": interval_min, "pct": pct,
                "label": f"{scope}OVERRUN {dur}m / {interval_min}m interval"}
    return {"level": "warn", "symbol": "!", "in_flight": in_flight,
            "duration_min": dur, "interval_min": interval_min, "pct": pct,
            "label": f"{scope}near-limit {dur}m / {interval_min}m interval"}

#!/usr/bin/env python3
"""Local cron-status dashboard.

Zero-dep web UI for cron-status. Wraps the same state files + DB queries
exposed by bin/cron-status.sh, served via stdlib http.server. Auto-refreshes
every 30s via vanilla JS. Chart.js loaded from CDN for time-series.

Usage:
    bin/dashboard.py             # serves on http://127.0.0.1:8765
    bin/dashboard.py --port 9000
    open http://127.0.0.1:8765

Routes:
    GET /                        — main page (HTML + JS)
    GET /api/snapshot            — full status JSON
    GET /api/identity-timeseries — signals over past 7d, hour-bucketed
    GET /api/log-tail?name=...   — log file tail (identity_reconcile / ingest / etc.)
    GET /api/slack-channels      — per-channel detail JSON
"""
from __future__ import annotations

import argparse
import json
import plistlib
import re
import sqlite3
import sys
import yaml
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Shared overrun-detection helpers (also used by bin/cron-status.sh).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _run_health as rh
import _codegraph_status as cg
import _routines as rt

PLIST_DIR = Path.home() / "Library/LaunchAgents"
_WD_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from derive.sources_config import launchd_prefix  # noqa: E402
from derive import holidays as _holidays  # noqa: E402
_LP = launchd_prefix()
DB_PATH = ROOT / "index/events.db"
STATE = ROOT / "state"
LOGS = ROOT / "logs"
IST = timezone(timedelta(hours=5, minutes=30))

SOURCES = ["github", "jira", "confluence", "slack"]
AGENT_MAP = {
    "github":     f"{_LP}.github-ingest",
    "jira":       f"{_LP}.jira-ingest",
    "confluence": f"{_LP}.confluence-ingest",
    "slack":      f"{_LP}.slack-ingest",
}


# ── data helpers ──────────────────────────────────────────────────────────────

def _read_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _read_marker(src: str) -> str | None:
    p = STATE / f"last_{src}_success.date"
    return p.read_text().strip() if p.exists() else None


def read_plist_weekly(label: str) -> dict:
    """Return {sched, next, entries} for a weekly/multi-day LaunchAgent."""
    p = PLIST_DIR / f"{label}.plist"
    if not p.exists():
        p = ROOT / "launchagents" / f"{label}.plist"
    if not p.exists():
        return {"sched": "(plist not found)", "next": "?"}
    try:
        with p.open("rb") as f:
            data = plistlib.load(f)
    except Exception:
        return {"sched": "(unreadable)", "next": "?"}
    sci = data.get("StartCalendarInterval")
    if not sci:
        return {"sched": "(no schedule)", "next": "?"}
    if isinstance(sci, dict):
        sci = [sci]
    entries = [(e.get("Weekday"), e.get("Hour", 0), e.get("Minute", 0)) for e in sci]
    times = {(h, m) for _, h, m in entries}
    days = [_WD_NAMES[wd] for wd, _, _ in entries if isinstance(wd, int) and 0 <= wd <= 6]
    if len(times) == 1:
        h, m = next(iter(times))
        sched = f"{'+'.join(days)} {h:02d}:{m:02d}"
    else:
        sched = " · ".join(f"{_WD_NAMES[wd]} {h:02d}:{m:02d}"
                           for wd, h, m in entries if isinstance(wd, int))
    now = datetime.now(IST)
    best = None
    for wd, h, m in entries:
        if not isinstance(wd, int):
            continue
        py_target = (wd - 1) % 7
        days_ahead = (py_target - now.weekday()) % 7
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=days_ahead)
        if cand <= now:
            cand += timedelta(days=7)
        if best is None or cand < best:
            best = cand
    if best is None:
        return {"sched": sched, "next": "?"}
    tot = int((best - now).total_seconds()) // 60
    d, rem = divmod(tot, 1440)
    h2, mn = divmod(rem, 60)
    nxt = f"~{d}d {h2}h" if d else (f"~{h2}h {mn}m" if h2 else f"~{mn}m")
    return {"sched": sched, "next": nxt}


def parse_housekeeping_log() -> dict:
    """Last-run info from logs/housekeeping.log (mode, date, files, bytes, actions).

    Mirrors parse_housekeeping() in bin/cron-status.sh so the web dashboard and the
    terminal status agree on what the weekly launchd prune actually did.
    """
    log = LOGS / "housekeeping.log"
    if not log.exists():
        return {}
    info: dict = {}
    try:
        text = log.read_text()
        run_starts = list(re.finditer(
            r"=== Housekeeping \((\w[\w-]*)\) ===\nToday: (\d{4}-\d{2}-\d{2})",
            text,
        ))
        if run_starts:
            last = run_starts[-1]
            body = text[last.start():]
            actions: dict[str, int] = {}
            for m in re.finditer(r"\[(?:DELETED|TRUNCD|DRY-RUN)\s*\]\s+(\S+)", body):
                actions[m.group(1)] = actions.get(m.group(1), 0) + 1
            info = {"mode": last.group(1), "date": last.group(2), "actions": actions}
            summary = re.search(
                r"=== Summary ===\s*\nFiles affected: (\d+)\s*\nBytes affected: (\S+)",
                body,
            )
            if summary:
                info["files"] = int(summary.group(1))
                info["bytes"] = summary.group(2)
        info.setdefault(
            "mtime",
            datetime.fromtimestamp(log.stat().st_mtime, tz=IST)
                    .isoformat(timespec="seconds"),
        )
    except Exception:
        pass
    return info


_SRC_MARKERS = {
    "github":     ("github ingest", "fetching prs", "/repos/"),
    "jira":       ("jira ingest", "jira project="),
    "confluence": ("confluence ingest", "confluence"),
    "slack":      ("slack ingest",),
}


def get_last_run_ts() -> dict[str, str]:
    """Return {source: ISO ts} of most recent 'ingest starting' per source.

    Reverse-scan logs/ingest.log so we never miss a source whose run is older
    than the tail window. Cap scan at 30k lines for speed.
    """
    log = LOGS / "ingest.log"
    if not log.exists():
        return {}
    lines = log.read_text().splitlines()[-30000:]
    out: dict[str, str] = {}
    needed = set(SOURCES)
    for ln in reversed(lines):
        if "ingest starting" not in ln.lower():
            continue
        ll = ln.lower()
        for s in list(needed):
            if s in ll:
                out[s] = ln.split(",")[0].strip()
                needed.discard(s)
                break
        if not needed:
            break
    return out


def _plist_fire_minutes(label: str) -> list[int]:
    """Minute-of-day list for every scheduled fire of a StartCalendarInterval job."""
    p = PLIST_DIR / f"{label}.plist"
    if not p.exists():
        p = ROOT / "launchagents" / f"{label}.plist"
    if not p.exists():
        return []
    try:
        with p.open("rb") as f:
            data = plistlib.load(f)
    except Exception:
        return []
    sci = data.get("StartCalendarInterval")
    if not sci:
        return []
    if isinstance(sci, dict):
        sci = [sci]
    minutes = sorted({e["Minute"] for e in sci if "Minute" in e}) or [0]
    hours = sorted({e["Hour"] for e in sci if "Hour" in e})
    return [h * 60 + m for h in hours for m in minutes]


def get_run_health() -> dict:
    """Per-source overrun verdict: run duration vs fire interval.

    Pairs each source's most-recent start->Done. (or flags an in-flight run
    with no Done yet) and classifies via _run_health. Returns {src: verdict}
    only for sources that warrant a flag (warn/fail); fine runs are omitted.
    """
    log = LOGS / "ingest.log"
    if not log.exists():
        return {}
    lines = log.read_text().splitlines()[-30000:]

    def src_of(line: str) -> str | None:
        ll = line.lower()
        for s in SOURCES:
            if s in ll:
                return s
        return None

    open_starts: dict[str, str] = {}
    last_done: dict[str, tuple[str, str]] = {}   # src -> (start_ts, done_ts)
    last_attr: str | None = None
    for ln in lines:
        if "ingest starting" in ln.lower():
            s = src_of(ln)
            if s:
                open_starts[s] = ln.split(",")[0].strip()
                last_attr = s
            continue
        if "Done." in ln:
            s = src_of(ln)
            if s not in open_starts:
                s = last_attr if last_attr in open_starts else next(iter(open_starts), None)
            if s and s in open_starts:
                last_done[s] = (open_starts.pop(s), ln.split(",")[0].strip())
                last_attr = None

    out: dict = {}
    for s in SOURCES:
        interval = rh.fire_interval_min(_plist_fire_minutes(AGENT_MAP[s]))
        if not interval:
            continue
        if s in open_starts and rh.source_running(s):
            v = rh.overrun_verdict(rh.inflight_duration_min(open_starts[s]),
                                   interval, in_flight=True)
        elif s in last_done:
            st, dn = last_done[s]
            v = rh.overrun_verdict(rh.run_duration_min(st, dn), interval)
        else:
            v = None
        if v:
            out[s] = v
    return out


def _db_stats(conn) -> dict:
    out: dict = {"by_source": {}, "by_type": {}, "recent_24h": {}, "total": 0}
    out["by_source"] = dict(conn.execute(
        "SELECT source, COUNT(*) FROM events GROUP BY source"
    ).fetchall())
    out["total"] = sum(out["by_source"].values())
    by_type: dict[str, list] = {}
    for src, et, n in conn.execute(
        "SELECT source, event_type, COUNT(*) FROM events "
        "GROUP BY source, event_type ORDER BY source, 3 DESC"
    ).fetchall():
        by_type.setdefault(src, []).append([et, n])
    out["by_type"] = by_type
    since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent: dict[str, list] = {}
    for src, et, n in conn.execute(
        "SELECT source, event_type, COUNT(*) FROM events WHERE ts >= ? "
        "GROUP BY source, event_type ORDER BY source, 3 DESC", (since_24h,)
    ).fetchall():
        recent.setdefault(src, []).append([et, n])
    out["recent_24h"] = recent
    return out


def get_snapshot() -> dict:
    snap: dict = {
        "computed_at": datetime.now(timezone.utc)
                       .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "today": datetime.now(IST).strftime("%Y-%m-%d"),
        "now_ist": datetime.now(IST).strftime("%a %d %b %Y · %H:%M IST"),
    }
    snap["cursors"] = _read_json(STATE / "cursors.json")
    snap["markers"] = {s: _read_marker(s) for s in SOURCES}
    snap["last_run_ts"] = get_last_run_ts()
    snap["run_health"] = get_run_health()
    snap["codegraph"] = cg.read_status(STATE, PLIST_DIR)
    # Weekly housekeeping prune (launchd LaunchAgent, not a /schedule routine).
    hk_sched = read_plist_weekly(f"{_LP}.housekeeping")
    snap["housekeeping"] = {**hk_sched, **parse_housekeeping_log()}
    snap["routines"] = rt.load_routines()
    snap["slack_cursors"] = _read_json(STATE / "slack_cursors.json")
    snap["slack_channel_meta"] = _read_json(STATE / "slack_channel_meta.json").get("channels", {})
    # Slack discover summary + schedule.
    sd = _read_json(STATE / "last_slack_discover.json")
    disc_sched = read_plist_weekly(f"{_LP}.slack-discover")
    _disc_full = sd.get("auto_full", []) or []
    _disc_owner = [c for c in _disc_full if c.get("owner_bypass")]
    snap["discover"] = {
        "n_full":   len(_disc_full) - len(_disc_owner),
        "n_owner":  len(_disc_owner),
        "n_team":   len(sd.get("auto_team_involved", []) or []),
        "n_review": len(sd.get("needs_review", []) or []),
        "n_silent": len(sd.get("team_silent", []) or []),
        "sched":    disc_sched["sched"],
        "next":     disc_sched["next"],
        "has_cache": bool(sd),
    }
    # User-group (subteam) discovery summary — propose-only, owner applies layers.
    ug = _read_json(STATE / "last_slack_discover_usergroups.json")

    def _ug_rows(bucket: str) -> list:
        out = []
        for g in (ug.get(bucket, []) or []):
            out.append({
                "id":      g.get("id", ""),
                "handle":  g.get("handle", ""),
                "size":    g.get("size", 0),
                "owner_in": bool(g.get("owner_in")),
                "reports": g.get("reports", 0),
                "broad":   bool(g.get("broad")),
                "layer":   g.get("layer", ""),
            })
        return out

    snap["discover_ug"] = {
        "n_mgr":    len(ug.get("manager", []) or []),
        "n_team":   len(ug.get("team", []) or []),
        "n_amb":    len(ug.get("ambiguous", []) or []),
        "n_config": len(ug.get("configured", []) or []),
        "has_cache": bool(ug),
        "manager":   _ug_rows("manager"),
        "team":      _ug_rows("team"),
        "ambiguous": _ug_rows("ambiguous"),
        "configured": _ug_rows("configured"),
    }
    snap["identity"] = _read_json(STATE / "last_identity_reconcile.json")
    snap["validate"] = {s: _read_json(STATE / f"last_{s}_validate.json")
                        for s in SOURCES}

    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            snap["db"] = _db_stats(conn)
            # Embedding stats.
            if conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='embedding'"
            ).fetchone():
                snap["embedding"] = {
                    "total": conn.execute("SELECT COUNT(*) FROM embedding").fetchone()[0],
                    "newest": conn.execute("SELECT MAX(computed_at) FROM embedding").fetchone()[0],
                    "oldest": conn.execute("SELECT MIN(computed_at) FROM embedding").fetchone()[0],
                    "by_source": dict(conn.execute(
                        "SELECT source, COUNT(*) FROM embedding GROUP BY source"
                    ).fetchall()),
                }
            # Identity signals total.
            if conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='identity_signals'"
            ).fetchone():
                snap["signals"] = {
                    "total": conn.execute(
                        "SELECT COUNT(*) FROM identity_signals"
                    ).fetchone()[0],
                    "by_source": dict(conn.execute(
                        "SELECT source, COUNT(*) FROM identity_signals GROUP BY source"
                    ).fetchall()),
                }
            conn.close()
        except Exception as e:
            snap["db_error"] = str(e)
    return snap


def get_slack_channels() -> list:
    cursors = _read_json(STATE / "slack_cursors.json")
    meta = _read_json(STATE / "slack_channel_meta.json").get("channels", {})
    cfg_channels: list[dict] = []
    if (ROOT / "config/slack_channels.yaml").exists():
        try:
            cfg = yaml.safe_load((ROOT / "config/slack_channels.yaml").read_text()) or {}
            cfg_channels = cfg.get("channels") or []
        except Exception:
            pass
    counts: dict = {}
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH))
            for cid, n, lt in conn.execute(
                "SELECT channel_id, COUNT(*), MAX(ts) FROM events "
                "WHERE source='slack' AND channel_id IS NOT NULL GROUP BY channel_id"
            ).fetchall():
                counts[cid] = (n, lt or "")
            conn.close()
        except Exception:
            pass
    checked = _read_json(STATE / "slack_channel_checked.json")
    cfg_by_id = {c.get("id"): c for c in cfg_channels if c.get("id")}
    # Active-ingest list = channels in slack_channels.yaml (what ingest actually
    # reads). Do NOT union in cursors/event-counts: those drag in ORPHANS —
    # channels with leftover events from past ingestion/backfill that are no
    # longer configured (and would render as "?" with no name). They're not
    # being ingested, so they don't belong in this list.
    all_ids = sorted(cfg_by_id,
                     key=lambda i: counts.get(i, (0, ""))[0], reverse=True)
    out = []
    for cid in all_ids:
        m = meta.get(cid, {})
        n, last_ts = counts.get(cid, (0, ""))
        out.append({
            "id":          cid,
            "name":        cfg_by_id.get(cid, {}).get("name") or m.get("name") or "?",
            "is_private":  m.get("is_private", False),
            "is_archived": m.get("is_archived", False),
            "cursor_ts":   cursors.get(cid),
            "events":      n,
            "last_activity": last_ts,
            "checked_ts":  checked.get(cid),
            "has_cursor":  cid in cursors,
        })
    return out


def get_identity_timeseries(days: int = 7) -> dict:
    """Signal observations bucketed by hour for the last `days` days."""
    if not DB_PATH.exists():
        return {"buckets": [], "by_source": {}}
    conn = sqlite3.connect(str(DB_PATH))
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='identity_signals'"
    ).fetchone():
        conn.close()
        return {"buckets": [], "by_source": {}}
    since = (datetime.now(timezone.utc) - timedelta(days=days)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT strftime('%Y-%m-%d %H:00', observed_at) bucket, source, COUNT(*) n "
        "FROM identity_signals WHERE observed_at >= ? "
        "GROUP BY bucket, source ORDER BY bucket", (since,)
    ).fetchall()
    conn.close()
    by_source: dict[str, dict[str, int]] = {}
    buckets: list[str] = []
    seen = set()
    for bucket, src, n in rows:
        if bucket not in seen:
            seen.add(bucket)
            buckets.append(bucket)
        by_source.setdefault(src, {})[bucket] = n
    return {"buckets": buckets, "by_source": by_source}


def get_discover() -> dict:
    """Full discovered-channel proposals from last_slack_discover.json.

    Owner-presence bypass channels (announcement / manager / HR rooms the owner
    sits in but the team doesn't — see slack_discover_channels._decide_mode) are
    split out of auto_full into their own `owner_channels` group so the dashboard
    can show them in a separate section.
    """
    sd = _read_json(STATE / "last_slack_discover.json")
    disc_sched = read_plist_weekly(f"{_LP}.slack-discover")
    auto_full = sd.get("auto_full") or []
    owner_channels = [c for c in auto_full if c.get("owner_bypass")]
    auto_full = [c for c in auto_full if not c.get("owner_bypass")]
    return {
        "generated_at": sd.get("generated_at"),
        "days": sd.get("days"),
        "sched": disc_sched["sched"],
        "next": disc_sched["next"],
        "owner_channels": owner_channels,
        "auto_full": auto_full,
        "auto_team_involved": sd.get("auto_team_involved") or [],
        "needs_review": sorted(sd.get("needs_review") or [],
                               key=lambda c: c.get("team_msgs", 0), reverse=True),
        "team_silent": sorted(sd.get("team_silent") or [],
                              key=lambda c: c.get("total_msgs", 0), reverse=True),
    }


def get_clusters(limit: int = 10, status: str | None = None) -> list[dict]:
    """Top clusters from topic_brief, sorted by member_count desc."""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='topic_brief'"
    ).fetchone():
        conn.close()
        return []
    cols = [r[1] for r in conn.execute("pragma table_info(topic_brief)").fetchall()]
    where = "WHERE status = ?" if status else ""
    params: tuple = (status,) if status else ()
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM topic_brief {where} "
        f"ORDER BY member_count DESC, last_activity_ts DESC LIMIT ?",
        params + (limit,),
    ).fetchall()
    conn.close()
    out: list[dict] = []
    for r in rows:
        d = dict(zip(cols, r))
        # Parse json columns for nicer rendering.
        for jc in ("decisions_json", "blockers_json", "participants_json",
                   "source_breakdown_json", "outcomes_json", "followups_json",
                   "risk_areas_json", "stakeholders_json", "artifacts_json"):
            if d.get(jc):
                try:
                    d[jc.replace("_json", "")] = json.loads(d[jc])
                except Exception:
                    pass
                d.pop(jc, None)
        out.append(d)
    return out


LEAVES_WINDOW_PAST = 14
LEAVES_WINDOW_FUTURE = 60

# Same dedup logic as derive/render_leaves.py: one slack message can land twice
# (top-level + thread-context form). Keep the lowest event_id per logical leave.
_LEAVES_DEDUP_CTE = """
    WITH dedup AS (
        SELECT actor, date_start, date_end, reason, mentioned_at,
               channel_id, channel_name, body_excerpt, url,
               ROW_NUMBER() OVER (
                   PARTITION BY actor,
                                COALESCE(date_start,'_'),
                                COALESCE(date_end,'_'),
                                COALESCE(reason,'_')
                   ORDER BY LENGTH(event_id), event_id
               ) AS rn
        FROM team_leaves
    )
"""


def get_leaves() -> dict:
    """Dated leaves within [today-14d, today+60d] + recent undated ones.

    Feeds the /leaves Gantt page. `leaves` are dated rows overlapping the
    window (one dict per logical leave); `ambiguous` are recent date-TBD
    mentions chat couldn't pin down. Window bounds + today returned so the
    frontend can lay out the time axis without re-deriving dates.
    """
    out = {
        "today": datetime.now(IST).strftime("%Y-%m-%d"),
        "window_start": (datetime.now(IST) - timedelta(days=LEAVES_WINDOW_PAST)).strftime("%Y-%m-%d"),
        "window_end": (datetime.now(IST) + timedelta(days=LEAVES_WINDOW_FUTURE)).strftime("%Y-%m-%d"),
        "leaves": [],
        "ambiguous": [],
        "total": 0,
    }
    # Company holidays — calendar-driven, display-only (not in team_leaves).
    # Window-scoped for the gantt; next_holiday for the lane headline.
    out["holidays"] = _holidays.in_window(out["window_start"], out["window_end"])
    out["next_holiday"] = _holidays.next_holiday(out["today"])
    out["next_fixed_holiday"] = _holidays.next_holiday(out["today"], fixed_only=True)
    if not DB_PATH.exists():
        return out
    try:
        conn = sqlite3.connect(str(DB_PATH))
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='team_leaves'"
        ).fetchone():
            conn.close()
            return out
        cols = ["actor", "date_start", "date_end", "reason", "mentioned_at",
                "channel_id", "channel_name", "body_excerpt", "url"]
        sel = ", ".join(cols)
        dated = conn.execute(_LEAVES_DEDUP_CTE + f"""
            SELECT {sel} FROM dedup WHERE rn = 1
              AND date_start IS NOT NULL
              AND date_start <= ?
              AND COALESCE(date_end, date_start) >= ?
            ORDER BY date_start, actor
        """, (out["window_end"], out["window_start"])).fetchall()
        ambig = conn.execute(_LEAVES_DEDUP_CTE + f"""
            SELECT {sel} FROM dedup WHERE rn = 1
              AND date_start IS NULL
              AND mentioned_at >= datetime('now', '-30 days')
            ORDER BY mentioned_at DESC
        """).fetchall()
        out["total"] = conn.execute("SELECT COUNT(*) FROM team_leaves").fetchone()[0]
        conn.close()
        out["leaves"] = [dict(zip(cols, r)) for r in dated]
        out["ambiguous"] = [dict(zip(cols, r)) for r in ambig]
    except Exception as e:
        out["error"] = str(e)
    return out


# Logs that live outside LOGS/ (resolved by absolute path).
LOGS_EXTERNAL = {
    "session-reaper.log": Path.home() / ".claude" / "logs" / "session-reaper.log",
}


def get_log_tail(name: str, n: int = 80) -> list[str]:
    safe = {"identity_reconcile.log", "ingest.log", "rollup.log",
            "housekeeping.log", "github-reset.log", "session-reaper.log"}
    if name not in safe:
        return [f"refused: {name} not in allowlist"]
    p = LOGS_EXTERNAL.get(name, LOGS / name)
    if not p.exists():
        return ["no such log"]
    return p.read_text().splitlines()[-n:]


# ── HTML page (inline; no template engine) ────────────────────────────────────

INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<title>cron-status · dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<style>
:root{
  color-scheme: dark;
  --bg:#0b0f14; --bg-deep:#080c10; --panel:#11161d; --panel-raised:#161c25;
  --hover:#0e1319; --line:#2a313b; --line-faint:#1a212b;
  --text:#d5d9e0; --text2:#a7afba; --text-strong:#ffffff;
  --muted:#6e7681; --muted2:#7a8497; --ink:#0b0f14;
  --overlay:#ffffff0d; --overlay-faint:#ffffff09;
  --green:#48d597; --yellow:#d5b248; --red:#d54848; --blue:#4d8eff;
  --purple:#9b8eff; --amber:#f2c14e; --accent:#f2c14e;
  --pill-ok-bg:#193b25; --pill-warn-bg:#3b2e19; --pill-fail-bg:#3b1919;
}
html[data-theme="light"]{
  color-scheme: light;
  --bg:#f6f7f9; --bg-deep:#eceef2; --panel:#ffffff; --panel-raised:#ffffff;
  --hover:#eef1f5; --line:#d3d8e0; --line-faint:#e6e9ee;
  --text:#1a1f26; --text2:#3f4753; --text-strong:#000000;
  --muted:#6b7480; --muted2:#8a929c; --ink:#0b0f14;
  --overlay:#0000000f; --overlay-faint:#00000008;
  --green:#1f9d63; --yellow:#9a7a16; --red:#cf3b3b; --blue:#2f6fe0;
  --purple:#6f5fe0; --amber:#c98a12; --accent:#c98a12;
  --pill-ok-bg:#d7f2e3; --pill-warn-bg:#f3ebcf; --pill-fail-bg:#f7dcdc;
}
html{ transition: background-color .15s ease, color .15s ease; }
#themeToggle{ position:fixed; top:10px; right:14px; z-index:1000; display:flex;
  background:var(--panel); border:1px solid var(--line); border-radius:6px;
  overflow:hidden; font:11px ui-monospace,Menlo,monospace; box-shadow:0 2px 8px #00000026; }
#themeToggle button{ background:transparent; color:var(--muted); border:0;
  padding:4px 10px; cursor:pointer; font:inherit; }
#themeToggle button:hover{ color:var(--text); }
#themeToggle button.on{ background:var(--blue); color:#fff; }
body { font: 13px ui-monospace,SFMono-Regular,Menlo,monospace; background:var(--bg);
       color:var(--text); max-width: 1240px; margin: 16px auto; padding: 0 16px; }
h1 { font-size:18px; margin:0 0 6px; letter-spacing:1px; }
.subtitle { color:var(--muted); margin-bottom:18px; font-size:11px; }
.grid { display:grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.lane { background:var(--panel); border-left:3px solid var(--blue); border-radius:4px;
        padding:10px 14px; }
.lane[data-state="ok"]   { border-color:var(--green); }
.lane[data-state="warn"] { border-color:var(--yellow); }
.lane[data-state="fail"] { border-color:var(--red); }
.lane h2 { font-size:13px; margin:0 0 8px; display:flex; justify-content:space-between; }
.pill { font-size:10px; padding:1px 6px; border-radius:8px; background:var(--pill-ok-bg); color:var(--green); }
.pill.warn { background:var(--pill-warn-bg); color:var(--yellow); }
.pill.fail { background:var(--pill-fail-bg); color:var(--red); }
.kv { display:grid; grid-template-columns:110px 1fr; gap:2px 12px; font-size:12px;
      color:var(--text2); }
.kv b { color:var(--text); font-weight:normal; }
details { margin-top:8px; }
summary { cursor:pointer; color:var(--muted2); font-size:11px; padding:4px 0; }
details[open] summary { color:var(--text); }
table { border-collapse:collapse; width:100%; margin-top:6px; font-size:12px; }
th { color:var(--muted); font-weight:normal; text-align:left; padding:3px 6px;
     border-bottom:1px solid var(--line); cursor:pointer; user-select:none; }
th:hover { color:var(--text); }
td { padding:3px 6px; }
tr:nth-child(even) td { background:var(--hover); }
.muted { color:var(--muted); }
.chart-wrap { background:var(--panel); padding:12px; border-radius:4px; margin-top:14px; }
.tail { background:var(--bg-deep); padding:8px; border-radius:3px; max-height:260px;
        overflow:auto; white-space:pre; font-size:11px; color:var(--muted2);
        border:1px solid var(--line-faint); }
.finding { font-size:11px; padding:4px 8px; margin:4px 0; border-radius:3px;
           border-left:3px solid var(--line); background:var(--hover); cursor:help; }
.pill[title] { cursor:help; }
.finding[title]:hover { background:var(--hover); }
.finding.warn { border-left-color:var(--yellow); }
.finding.fail { border-left-color:var(--red); }
.finding.muted { border-left-color:var(--muted2); opacity:0.7; }
.finding b { color:var(--text); }
.cluster { background:var(--panel); border-left:3px solid var(--blue); padding:10px 12px;
           margin:8px 0; border-radius:3px; }
.cluster h3 { font-size:13px; margin:0 0 4px; color:var(--text); font-weight:normal; }
.cluster .meta { color:var(--muted); font-size:11px; margin-bottom:4px; }
.cluster .summary { color:var(--text2); font-size:12px; margin:6px 0; }
.cluster .chips span { display:inline-block; padding:1px 6px; margin:2px;
                       font-size:10px; background:var(--line-faint); border-radius:8px;
                       color:var(--text2); }
.cluster .json-block { background:var(--bg-deep); padding:6px; margin:4px 0;
                       border-radius:3px; font-size:11px; color:var(--muted2);
                       white-space:pre-wrap; word-break:break-word; }
.cluster[data-status="ACTIVE"]    { border-left-color:var(--green); }
.cluster[data-status="RECURRING"] { border-left-color:var(--blue); }
.cluster[data-status="STALE"]     { border-left-color:var(--yellow); }
.cluster[data-status="RESOLVED"]  { border-left-color:var(--muted); }
/* Cluster pack chart */
#clusterPack { width:100%; height:560px; background:var(--bg-deep); border-radius:4px;
               border:1px solid var(--line-faint); }
#clusterPack circle { stroke:var(--bg); stroke-width:1.2; cursor:pointer; }
#clusterPack circle:hover { stroke:var(--text-strong); stroke-width:2; }
#clusterPack text { fill:var(--text); pointer-events:none; font-size:11px;
                     text-anchor:middle; font-family: inherit; }
.tooltip { position:absolute; background:var(--panel-raised); color:var(--text); padding:8px 10px;
           border-radius:4px; border:1px solid var(--line); font-size:11px;
           max-width:340px; pointer-events:none; opacity:0;
           transition:opacity 0.12s; z-index:999; box-shadow:0 4px 16px #000a; }
.tooltip.show { opacity:1; }
.tooltip b { color:var(--text-strong); }
.tooltip .meta { color:var(--text2); font-size:10px; margin-top:4px; }
.view-toggle { display:inline-flex; gap:4px; }
.view-toggle button { background:var(--panel); color:var(--muted2); border:1px solid var(--line);
                      padding:3px 10px; font:inherit; font-size:11px; cursor:pointer;
                      border-radius:3px; }
.view-toggle button.active { background:var(--line-faint); color:var(--text); border-color:var(--blue); }
#clusterDetail { margin-top:12px; }
.legend { display:flex; gap:14px; font-size:11px; color:var(--text2); margin:8px 0; }
.legend span::before { content:"●"; margin-right:4px; }
.legend .active::before    { color:var(--green); }
.legend .recurring::before { color:var(--blue); }
.legend .stale::before     { color:var(--yellow); }
.legend .resolved::before  { color:var(--muted); }
.row { display:flex; gap:14px; align-items:center; }
.row label { color:var(--muted); font-size:11px; }
.row select, .row button { background:var(--panel); color:var(--text); border:1px solid var(--line);
        padding:4px 8px; font:inherit; border-radius:3px; }
.refresh-btn { cursor:pointer; }
</style></head>
<body>
<div id="themeToggle">
  <button data-t="auto">auto</button><button data-t="light">light</button><button data-t="dark">dark</button>
</div>
<script>
(function(){
  var KEY="dash-theme";
  function resolve(m){ if(m==="light"||m==="dark") return m;
    var h=new Date().getHours(); return (h>=19||h<7)?"dark":"light"; }
  function apply(m){ document.documentElement.setAttribute("data-theme", resolve(m));
    var bs=document.querySelectorAll("#themeToggle button");
    for(var i=0;i<bs.length;i++) bs[i].classList.toggle("on", bs[i].dataset.t===m); }
  var mode=localStorage.getItem(KEY)||"auto";
  apply(mode);
  document.addEventListener("click",function(e){
    var b=e.target.closest&&e.target.closest("#themeToggle button"); if(!b) return;
    mode=b.dataset.t; localStorage.setItem(KEY,mode); apply(mode); });
  setInterval(function(){ if((localStorage.getItem(KEY)||"auto")==="auto") apply("auto"); }, 600000);
})();
</script>
<h1>INGEST STATUS DASHBOARD</h1>
<div class="subtitle">
  <span id="when">loading…</span>
  &nbsp;·&nbsp; <span id="auto">auto-refresh 30min</span>
  &nbsp;·&nbsp; <a class="refresh-btn" onclick="refresh()">refresh now</a>
</div>

<div class="grid" id="lanes"></div>

<div class="chart-wrap">
  <h2 style="font-size:13px;margin:0 0 8px">IDENTITY SIGNALS · last 7d (hourly)</h2>
  <canvas id="sigChart" height="80"></canvas>
</div>

<div class="chart-wrap">
  <div class="row" style="margin-bottom:8px;justify-content:space-between">
    <h2 style="font-size:13px;margin:0">TOP CLUSTERS</h2>
    <div class="row">
      <div class="view-toggle">
        <button id="viewGraph" class="active" onclick="setView('graph')">graph</button>
        <button id="viewList" onclick="setView('list')">list</button>
      </div>
      <label>status:</label>
      <select id="clusterStatus" onchange="loadClusters()">
        <option value="">all</option>
        <option value="ACTIVE">active</option>
        <option value="RECURRING">recurring</option>
        <option value="STALE">stale</option>
        <option value="RESOLVED">resolved</option>
      </select>
      <label>top N:</label>
      <select id="clusterLimit" onchange="loadClusters()">
        <option>10</option><option>20</option>
        <option selected>50</option><option>100</option><option>200</option>
      </select>
    </div>
  </div>
  <div class="legend">
    <span class="active">ACTIVE</span>
    <span class="recurring">RECURRING</span>
    <span class="stale">STALE</span>
    <span class="resolved">RESOLVED</span>
    <span class="muted" style="margin-left:auto">circle area ∝ member_count · hover for detail · click to expand</span>
  </div>
  <svg id="clusterPack"></svg>
  <div id="clusters" style="display:none">…</div>
  <div id="clusterDetail"></div>
</div>
<div class="tooltip" id="tooltip"></div>

<div class="chart-wrap">
  <div class="row" style="margin-bottom:8px">
    <label>log tail:</label>
    <select id="logname" onchange="loadLog()">
      <option>identity_reconcile.log</option>
      <option>ingest.log</option>
      <option>rollup.log</option>
      <option>housekeeping.log</option>
      <option>session-reaper.log</option>
    </select>
    <a class="refresh-btn" onclick="loadLog()">reload</a>
  </div>
  <div class="tail" id="logtail">…</div>
</div>

<script>
let sigChart = null;

function _rel(iso) {
  if (!iso) return "—";
  try {
    const dt = new Date(iso);
    const secs = (Date.now() - dt.getTime()) / 1000;
    if (secs < 3600) return `${Math.floor(secs/60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs/3600)}h ago`;
    return `${Math.floor(secs/86400)}d ago`;
  } catch (e) { return "?"; }
}

function _worstState(...states) {
  if (states.includes("fail")) return "fail";
  if (states.includes("warn")) return "warn";
  return states.find(x => x) || "ok";
}
// Overrun badge + finding from snapshot.run_health[src] (or null).
function _runtimeBadge(rhv) {
  if (!rhv) return "";
  const sym = rhv.symbol === "x" ? "✗" : "⚠";
  return `<span>runtime</span><b><span class="pill ${rhv.level}">${sym} ${rhv.label}</span></b>`;
}
function _runtimeFinding(rhv) {
  if (!rhv) return "";
  return `<div class="finding ${rhv.level}" title="${_esc(CHECK_HELP.runtime)}">`
       + `<b>${rhv.level.toUpperCase()}</b> · runtime — `
       + `${rhv.label} (run duration vs gap to next fire)</div>`;
}
// Plain-English explanation for each validator check / lane state. Shown on
// hover over the WARN/FAIL badge and over each finding line so the terse
// "attribution — 4 unmapped" reads as "what it means + what to do".
const CHECK_HELP = {
  attribution: "Some contributors couldn't be matched to a known person (team/org/external). Their activity may be miscredited until they're added to the identity map (people.yaml).",
  freshness: "This source is stale — the last successful ingest is older than expected. The cron likely failed or hasn't fired in its window.",
  status_capture: "Jira status transitions weren't fully captured for some tickets, so in-progress / in-review state may be incomplete.",
  schema_nulls: "Some events are missing required columns (id / source / ts / actor) — the ingest wrote malformed rows.",
  ts_format: "Some event timestamps aren't valid ISO-8601, so date math and ordering can be wrong.",
  ts_future: "Some events are timestamped in the future — usually a timezone or parsing bug in the ingest.",
  source_vocab: "An unexpected 'source' value appeared — a new ingest path may be writing an unrecognized source tag.",
  type_vocab: "An unexpected event_type appeared — a new event kind isn't whitelisted yet.",
  orphan_refs: "Some references point to a parent event that doesn't exist — a thread/reply was ingested without its root.",
  fts_sync: "The full-text search index is out of sync with the events table — search may miss or duplicate rows.",
  raw_path_dupes: "Two events claim the same raw_path back-reference — a re-ingest probably double-wrote.",
  slack_channel_id: "A Slack event is missing or has a malformed channel_id.",
  null_actor_subject: "Some events have no actor or subject — they can't be attributed or clustered.",
  subject_shape: "Some subject ids don't match the expected shape — downstream linking may break.",
  ref_vocab: "An unexpected ref_type appeared in event_refs.",
  dangling_derived: "A derived row points to an event that no longer exists.",
  empty: "The table is empty — nothing has been derived yet.",
  empty_db: "The events table is empty — nothing has been ingested yet.",
  all_fields: "Informational: all clusters are fully populated.",
  schedule: "The scheduled ingest hasn't succeeded today yet — it may be waiting for its fire window, or the last run failed.",
  runtime: "The job's run time is close to (or past) the gap until its next scheduled fire — runs may overlap or back up.",
  stale_embedding: "Embeddings are more than 2 days old — re-run the embedding refresh to pick up new events.",
  stale_codegraph: "The code-graph rebuild hasn't succeeded today — the daily 18:00 job may have failed.",
  housekeeping_age: "The weekly cleanup prune hasn't run in over a week.",
  slack_lag: "Slack channels haven't been polled recently — the ingest may be lagging behind live messages.",
};
function _fhelp(check) { return CHECK_HELP[check] || ("Validation check: " + check); }
// One finding line, with a hover explanation derived from its check name.
function _finding(sev, check, msg) {
  const lvl = sev.toLowerCase();
  return `<div class="finding ${lvl}" title="${_esc(_fhelp(check))}">`
       + `<b>${sev}</b> · ${check} — ${msg}</div>`;
}

function laneFor(name, state, body, tip) {
  const stateClass = state || "ok";
  const t = tip ? ` title="${_esc(tip)}"` : "";
  const pill = (state === "fail" ? `<span class="pill fail"${t}>FAIL</span>`
               : state === "warn" ? `<span class="pill warn"${t}>WARN</span>`
               : `<span class="pill">OK</span>`);
  return `<div class="lane" data-state="${stateClass}">
    <h2>${name} ${pill}</h2>
    ${body}
  </div>`;
}

async function refresh() {
  const r = await fetch("/api/snapshot");
  const s = await r.json();
  const slack = await (await fetch("/api/slack-channels")).json();
  const leaves = await (await fetch("/api/leaves")).json();
  document.getElementById("when").textContent = s.now_ist || "?";

  const lanes = [];

  // Cron sources
  for (const src of ["github","jira","confluence"]) {
    const m = s.markers[src];
    const cur = s.cursors[src];
    const v = s.validate?.[src] || {};
    const findings = v.findings || [];
    const worstSev = findings.find(f => f[0] === "FAIL") ? "fail"
                   : findings.find(f => f[0] === "WARN") ? "warn"
                   : null;
    const cronState = m === s.today ? "ok" : (m ? "warn" : "fail");
    const rhv = s.run_health?.[src] || null;
    const state = _worstState(worstSev, rhv?.level, cronState);
    // If lane is WARN/FAIL purely from cron marker (not validate finding),
    // synthesize a reason so the message panel isn't empty.
    const synthReason = (!worstSev && cronState !== "ok")
      ? `<div class="finding ${cronState}" title="${_esc(CHECK_HELP.schedule)}"><b>${cronState.toUpperCase()}</b> · schedule — `
        + (m ? `last success ${m} · cron not yet fired today (next fire window 12-22 IST)`
             : `never ran — wrapper or LaunchAgent not invoked`)
        + `</div>`
      : "";
    const dbN = (s.db?.by_source?.[src] || 0).toLocaleString();
    const types = (s.db?.by_type?.[src] || []).slice(0,5);
    const typeRows = types.map(([t,n]) =>
      `<tr><td>${t}</td><td>${n.toLocaleString()}</td></tr>`).join("");

    // Render warnings inline so reasons are visible.
    const findingsHtml = findings
      .filter(f => f[0] !== "PASS")
      .map(([sev, check, msg]) => _finding(sev, check, msg))
      .join("");

    // Plain-English tip for the WARN/FAIL badge: worst real finding, else the
    // synthesized schedule/runtime reason that drove the lane state.
    const worstF = findings.find(f => f[0] === "FAIL") || findings.find(f => f[0] === "WARN");
    const laneTip = worstF ? _fhelp(worstF[1])
                  : synthReason ? CHECK_HELP.schedule
                  : rhv ? CHECK_HELP.runtime : "";

    const lastRun = s.last_run_ts?.[src];
    // Local log timestamps lack tz; treat as IST.
    const lastIso = lastRun ? lastRun.replace(" ", "T") + "+05:30" : null;
    lanes.push(laneFor(src.toUpperCase(), state, `
      <div class="kv">
        <span>last run</span><b>${lastRun || "—"} <span class="muted">(${_rel(lastIso)})</span></b>
        <span>success.date</span><b>${m || "—"}</b>
        <span>cursor</span><b>${cur || "—"} <span class="muted">(${_rel(cur)})</span></b>
        <span>db</span><b>${dbN} events</b>
        <span>mapped</span><b>${(v.n_actors_mapped || 0).toLocaleString()} actors · ${(v.n_actors_raw_unknown || 0)} unmapped</b>
        ${_runtimeBadge(rhv)}
      </div>
      ${synthReason}${_runtimeFinding(rhv)}${findingsHtml}
      <details><summary>event-type breakdown</summary>
        <table><tr><th>type</th><th>count</th></tr>${typeRows}</table>
      </details>`, laneTip));
  }

  // SLACK lane
  const slackState = slack.length ? "ok" : "warn";
  const FRESH_MS = 45 * 60 * 1000;
  function _statusCell(c) {
    if (!c.has_cursor) return `<span class="pill warn">no cursor</span>`;
    if (!c.checked_ts) return `<span class="muted">? unpolled</span>`;
    const lag = Date.now() - new Date(c.checked_ts).getTime();
    if (lag <= FRESH_MS) return `<span class="pill">✓ up-to-date</span>`;
    return `<span class="pill warn">⚠ lag ${_rel(c.checked_ts).replace(" ago","")}</span>`;
  }
  const slackRows = slack.slice(0,40).map(c => {
    const curAge = c.cursor_ts ? _rel(new Date(parseFloat(c.cursor_ts)*1000).toISOString()) : "—";
    return `<tr><td>${c.name}</td>
            <td>${c.events.toLocaleString()}</td>
            <td class="muted">${_rel(c.last_activity)}</td>
            <td class="muted">${c.checked_ts ? _rel(c.checked_ts) : "—"}</td>
            <td>${_statusCell(c)}</td></tr>`;
  }).join("");
  const slackV = s.validate?.slack || {};
  const slackFindings = (slackV.findings || [])
    .filter(f => f[0] !== "PASS")
    .map(([sev, check, msg]) => _finding(sev, check, msg))
    .join("");
  const slackValSev = (slackV.findings || []).find(f => f[0] === "FAIL") ? "fail"
                  : (slackV.findings || []).find(f => f[0] === "WARN") ? "warn"
                  : "ok";
  const slackRh = s.run_health?.slack || null;
  const slackWorst = _worstState(slackValSev, slackRh?.level);
  const slackWorstF = (slackV.findings || []).find(f => f[0] === "FAIL")
                   || (slackV.findings || []).find(f => f[0] === "WARN");
  const slackTip = slackWorstF ? _fhelp(slackWorstF[1])
                 : slackRh ? CHECK_HELP.runtime
                 : (slackWorst !== "ok" ? CHECK_HELP.slack_lag || "" : "");
  const slackLastRun = s.last_run_ts?.slack;
  const slackLastIso = slackLastRun ? slackLastRun.replace(" ", "T") + "+05:30" : null;
  const disc = s.discover || {};
  const discReady = (disc.n_full || 0) + (disc.n_team || 0) + (disc.n_owner || 0);
  const discOwnerStr = disc.n_owner ? ` · ${disc.n_owner} owner` : "";
  const discSilentStr = disc.n_silent ? ` · ${disc.n_silent} team-silent` : "";
  const discRow = disc.sched ? `
      <span>discover</span><b>${discReady} ready${discOwnerStr} · <span class="muted">${disc.n_review || 0} needs_review${discSilentStr}</span></b>
      <span>disc-sched</span><b>${disc.sched} IST · next ${disc.next}</b>` : "";
  const ug = s.discover_ug || {};
  const ugPending = (ug.n_mgr || 0) + (ug.n_team || 0) + (ug.n_amb || 0);
  const ugRow = ug.has_cache ? `
      <span>usergroups</span><b>${ugPending} pending · <span class="muted">${ug.n_mgr || 0} mgr · ${ug.n_team || 0} team · ${ug.n_amb || 0} ambiguous</span></b>` : "";
  const _ugRows = (rows, applyFlag) => (rows || []).map(g => {
    const reps = g.reports ? `${g.reports}` : "—";
    const layerCell = g.layer
        ? `<span class="muted">${g.layer}</span>`
        : (applyFlag
            ? `<code style="font-size:10px">--apply-${applyFlag} ${g.id}</code>`
            : "");
    const flags = [g.owner_in ? "owner" : "", g.broad ? "⚠ broad" : ""].filter(Boolean).join(" · ");
    return `<tr><td>@${g.handle}</td><td>${g.size}</td><td>${reps}</td>`
         + `<td><span class="muted">${flags}</span></td><td>${layerCell}</td></tr>`;
  }).join("");
  const _ugSection = (title, rows, applyFlag) =>
    (rows && rows.length)
      ? `<tr><td colspan="5" style="padding:10px 6px 4px;border-top:1px solid rgba(255,255,255,.12);background:rgba(77,142,255,.10);color:#4d8eff;text-transform:uppercase;font-size:10px;letter-spacing:.08em;font-weight:700">${title}</td></tr>${_ugRows(rows, applyFlag)}`
      : "";
  const ugDetails = ug.has_cache ? `
    <details><summary>discovered user-groups (${ug.n_mgr || 0} mgr · ${ug.n_team || 0} team · ${ug.n_amb || 0} ambiguous · ${ug.n_config || 0} configured) — click to expand</summary>
      <table><tr><th>handle</th><th>size</th><th>reports</th><th>flags</th><th>layer / apply</th></tr>
        ${_ugSection("manager → owner_member", ug.manager, "manager")}
        ${_ugSection("team → ingest filter", ug.team, "team")}
        ${_ugSection("ambiguous (you pick)", ug.ambiguous, "manager")}
        ${_ugSection("already configured", ug.configured, "")}
      </table>
      <div style="margin-top:6px"><span class="muted" style="font-size:11px">apply: python derive/slack_discover_usergroups.py --apply-manager &lt;ids&gt; | --apply-team &lt;ids&gt; | --skip &lt;ids&gt;</span></div>
    </details>` : "";
  lanes.push(laneFor("SLACK", slackWorst, `
    <div class="kv">
      <span>last run</span><b>${slackLastRun || "—"} <span class="muted">(${_rel(slackLastIso)})</span></b>
      <span>cursors</span><b>${Object.keys(s.slack_cursors).length}</b>
      <span>events</span><b>${(s.db?.by_source?.slack || 0).toLocaleString()}</b>
      ${_runtimeBadge(slackRh)}
      ${discRow}
      ${ugRow}
    </div>
    ${_runtimeFinding(slackRh)}${slackFindings}
    <details><summary>per-channel detail (top 40 of ${slack.length} by events) — click to expand</summary>
      <table><tr><th>name</th><th>events</th><th>last msg</th>
                  <th>checked</th><th>status</th></tr>
        ${slackRows}</table>
      ${slack.length > 40 ? `<div style="margin-top:6px"><a href="/channels" target="_blank" style="color:#4d8eff;font-size:11px">view all ${slack.length} channels →</a></div>` : ""}
    </details>
    <details><summary>discovered channels (${disc.n_owner ? disc.n_owner + " owner · " : ""}${disc.n_review || 0} needs_review${disc.n_silent ? " · " + disc.n_silent + " team-silent" : ""}) — click to expand</summary>
      <div id="discoverTable"><span class="muted">loading…</span></div>
    </details>
    ${ugDetails}`, slackTip));

  // LEAVES lane (team_leaves → Gantt page at /leaves)
  {
    const today = leaves.today;
    const dated = leaves.leaves || [];
    const ambig = leaves.ambiguous || [];
    const _end = l => l.date_end || l.date_start;
    const active   = dated.filter(l => l.date_start <= today && _end(l) >= today);
    const upcoming = dated.filter(l => l.date_start > today);
    const lvState = leaves.error ? "fail" : "ok";
    const peopleOut = [...new Set(active.map(l => l.actor))];
    const peopleStr = peopleOut.length
      ? peopleOut.slice(0, 6).join(", ") + (peopleOut.length > 6 ? ` +${peopleOut.length - 6}` : "")
      : "nobody";
    const nh = leaves.next_holiday;
    const nf = leaves.next_fixed_holiday;
    // Headline the soonest holiday; if it's optional, also note the next fixed
    // one (the day everyone's actually off).
    let holRow = "";
    if (nh) {
      const optTag = nh.type === "holiday" ? "" : ` <span class="muted">(optional)</span>`;
      const daysOut = Math.max(0, Math.round((new Date(nh.date) - new Date(today)) / 86400000));
      let line = `${nh.date} · ${_esc(nh.occasion)}${optTag} <span class="muted">(in ${daysOut}d)</span>`;
      if (nf && nf.date !== nh.date)
        line += `<br><span class="muted">next fixed: ${nf.date} · ${_esc(nf.occasion)}</span>`;
      holRow = `<span>next holiday</span><b>${line}</b>`;
    }
    lanes.push(laneFor("LEAVES", lvState, `
      <div class="kv">
        <span>out today</span><b>${active.length} ${active.length === 1 ? "person" : "people"} <span class="muted">${peopleStr}</span></b>
        <span>upcoming</span><b>${upcoming.length} <span class="muted">(next ${60}d)</span></b>
        <span>date TBD</span><b>${ambig.length} <span class="muted">ambiguous mention(s)</span></b>
        <span>tracked</span><b>${(leaves.total || 0).toLocaleString()} total rows</b>
        ${holRow}
      </div>
      ${leaves.error ? `<div class="finding fail"><b>FAIL</b> · ${_esc(leaves.error)}</div>` : ""}
      <div style="margin-top:8px"><a href="/leaves" target="_blank" style="color:#4d8eff;font-size:12px">📅 view leaves gantt →</a></div>`));
  }

  // IDENTITY lane
  if (s.identity) {
    const i = s.identity;
    const total = i.total_entries || 1;
    const cov = Object.entries(i.coverage || {}).map(([k,v]) =>
      `${k}=${Math.round(100*v/total)}%`).join(" · ");
    lanes.push(laneFor("IDENTITY", "ok", `
      <div class="kv">
        <span>signals</span><b>${(i.signals_total || 0).toLocaleString()} pairs</b>
        <span>scope</span><b>team=${i.by_scope?.team || 0} · org=${i.by_scope?.org || 0} · external=${i.by_scope?.external || 0}</b>
        <span>coverage</span><b>${cov}</b>
        <span>last fills</span><b>+${i.n_changes || 0} fills · ${i.n_orphans || 0} orphans</b>
        <span>computed_at</span><b>${i.computed_at} <span class="muted">(${_rel(i.computed_at)})</span></b>
      </div>`));
  }

  // EMBEDDING lane
  if (s.embedding) {
    const e = s.embedding;
    const dt = new Date(e.newest);
    const stale = (Date.now() - dt.getTime()) / 86400000 >= 2;
    const bySrc = Object.entries(e.by_source).map(([k,v]) =>
      `${k}=${v.toLocaleString()}`).join("  ");
    lanes.push(laneFor("EMBEDDING", stale ? "warn" : "ok", `
      <div class="kv">
        <span>total</span><b>${e.total.toLocaleString()} vectors</b>
        <span>newest</span><b>${e.newest} <span class="muted">(${_rel(e.newest)})</span></b>
        <span>by source</span><b>${bySrc}</b>
      </div>`, stale ? CHECK_HELP.stale_embedding : ""));
  }

  // CODE-GRAPH lane (daily 18:00 code-review-graph rebuild)
  if (s.codegraph) {
    const g = s.codegraph;
    const sd = g.success_date;
    const stale = sd && sd !== s.today;
    const gState = g.fail ? "fail" : (g.running ? "ok" : (stale ? "warn" : (sd ? "ok" : "fail")));
    const _k = (n) => n >= 1000 ? Math.round(n / 1000) + "k" : String(n);
    const repos = (g.repos || []).map(r => {
      const mark = r.ok ? "✓" : "✗";
      const t = r.totals
        ? ` <span class="muted">(${_k(r.totals.nodes)} nodes·${_k(r.totals.edges)} edges)</span>` : "";
      return `${r.name} ${mark}${t}`;
    }).join(" · ");
    const lastRunIso = g.done ? g.done.replace(" ", "T") + "+05:30" : null;
    const lastRunCell = g.running
      ? `<span class="muted">rebuilding since ${g.start || "?"}</span>`
      : (g.done ? `${g.done} <span class="muted">(${_rel(lastRunIso)})</span> · ok=${g.ok} fail=${g.fail}` : "—");
    const cgTip = g.fail ? "The code-graph rebuild reported failures on its last run — one or more repos didn't parse cleanly."
                : gState === "warn" ? CHECK_HELP.stale_codegraph
                : (!sd ? "The code-graph has never recorded a successful rebuild." : "");
    lanes.push(laneFor("CODE-GRAPH", gState, `
      <div class="kv">
        <span>schedule</span><b>daily ${g.sched || "18:00 IST"}${g.next && !g.running ? ` · next ${g.next}` : ""}</b>
        <span>last run</span><b>${lastRunCell}</b>
        <span>success.date</span><b>${sd || "—"}</b>
        <span>repos</span><b>${repos || "—"}</b>
      </div>`, cgTip));
  }

  // HOUSEKEEPING lane (weekly launchd prune — distinct from /schedule routines)
  if (s.housekeeping && (s.housekeeping.date || s.housekeeping.sched)) {
    const h = s.housekeeping;
    let hState = "fail", hAge = "never ran";
    if (h.date) {
      const ageDays = Math.floor(
        (new Date(s.today + "T00:00:00+05:30") - new Date(h.date + "T00:00:00+05:30"))
        / 86400000);
      hAge = ageDays <= 0 ? "ran today" : `ran ${ageDays}d ago`;
      hState = ageDays <= 8 ? "ok" : "warn";
    } else if (h.mtime) {
      hState = "warn"; hAge = `log ${_rel(h.mtime)}`;
    }
    const acts = h.actions || {};
    const order = ["bak>60d","verdict>15d","handoff>15d","dverdict>15d","log>60d",".DS_Store"];
    const actStr = order.filter(c => acts[c]).map(c => `${c}:${acts[c]}`).join("  ")
                 || Object.entries(acts).map(([c,n]) => `${c}:${n}`).join("  ") || "—";
    const lastCell = h.date
      ? `<b>${h.date}</b> <span class="muted">(${h.mode || "?"})</span> · `
        + `${h.files ?? "?"} files · ${h.bytes || "?"} <span class="muted">(${hAge})</span>`
      : `<span class="muted">${hAge}</span>`;
    const hkTip = hState === "fail" ? "The weekly cleanup prune has never run — the launchd job may not be installed."
                : hState === "warn" ? CHECK_HELP.housekeeping_age : "";
    lanes.push(laneFor("HOUSEKEEPING", hState, `
      <div class="kv">
        <span>schedule</span><b>${h.sched || "—"} IST${h.next ? ` · next ${h.next}` : ""}</b>
        <span>last run</span><b>${lastCell}</b>
        <span>policy</span><b class="muted">weekly · prune old bak/verdicts/handoffs/logs + .DS_Store</b>
        <span>pruned</span><b class="muted">${actStr}</b>
      </div>`, hkTip));
  }

  // ROUTINES lane (Claude Code /schedule agents — distinct from launchd crons)
  if (s.routines && s.routines.length) {
    const rs = s.routines;
    const nOn = rs.filter(r => r.enabled).length;
    const rState = nOn ? "ok" : "warn";
    const rows = rs.map(r => {
      const dot = r.enabled
        ? `<span class="pill">on</span>`
        : `<span class="pill warn">off</span>`;
      const next = r.enabled ? (r.next_fire_rel || "—") : "—";
      return `<tr><td>${dot}</td>
              <td><b>${r.id}</b></td>
              <td>${r.sched_human}</td>
              <td class="muted">${r.last_run_rel || "—"}</td>
              <td class="muted">${next}</td></tr>`;
    }).join("");
    lanes.push(laneFor("ROUTINES", rState, `
      <div class="kv">
        <span>active</span><b>${nOn} of ${rs.length} scheduled agent(s)</b>
        <span>policy</span><b class="muted">Claude Code /schedule · cron in IST · MCP-registered</b>
      </div>
      <table><tr><th></th><th>routine</th><th>cadence</th>
                  <th>last run</th><th>next fire</th></tr>
        ${rows}</table>`, rState === "warn" ? "No scheduled agents are enabled — none of the Claude /schedule routines will fire." : ""));
  }

  document.getElementById("lanes").innerHTML = lanes.join("");

  // #discoverTable lives inside the lanes HTML just injected above, so populate
  // it now — BEFORE the time-series/chart fetches below, which can stall on a
  // blocked Chart.js CDN and would otherwise leave the panel on "loading…".
  loadDiscover();

  // Time-series chart
  const tsResp = await fetch("/api/identity-timeseries");
  const ts = await tsResp.json();
  drawChart(ts);
}

function drawChart(ts) {
  // Chart.js comes from a CDN; if it failed to load (offline / blocked) skip
  // the chart rather than throwing and aborting the rest of refresh().
  if (typeof Chart === "undefined") return;
  const labels = ts.buckets;
  const colors = {github:"#5eb1ff", jira:"#9b8eff", confluence:"#d56dff", slack:"#f2c14e"};
  const datasets = Object.entries(ts.by_source).map(([src, data]) => ({
    label: src,
    data: labels.map(b => data[b] || 0),
    borderColor: colors[src] || "#48d597",
    backgroundColor: (colors[src] || "#48d597") + "33",
    tension: 0.2,
    pointRadius: 0,
  }));
  if (sigChart) sigChart.destroy();
  // Pull axis/legend colors from the active theme so the chart flips with it.
  const _cs = getComputedStyle(document.documentElement);
  const _muted = _cs.getPropertyValue("--muted").trim() || "#6e7681";
  const _text2 = _cs.getPropertyValue("--text2").trim() || "#a7afba";
  const _grid  = _cs.getPropertyValue("--line-faint").trim() || "#1a212b";
  sigChart = new Chart(document.getElementById("sigChart"), {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: _text2 }}},
      scales: {
        x: { ticks: { color: _muted, maxRotation:0, autoSkip:true, maxTicksLimit: 12 }},
        y: { ticks: { color: _muted }, grid: { color: _grid }, beginAtZero: true },
      },
    },
  });
}

async function loadLog() {
  const name = document.getElementById("logname").value;
  const r = await fetch(`/api/log-tail?name=${encodeURIComponent(name)}&n=60`);
  const j = await r.json();
  document.getElementById("logtail").textContent = (j.lines || []).join("\\n");
}

function _esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, ch =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
}

const STATUS_COLOR = {
  ACTIVE:    "#48d597",
  RECURRING: "#4d8eff",
  STALE:     "#d5b248",
  RESOLVED:  "#6e7681",
};

let _clusterCache = [];
let _view = "graph";

function setView(v) {
  _view = v;
  document.getElementById("viewGraph").classList.toggle("active", v === "graph");
  document.getElementById("viewList").classList.toggle("active", v === "list");
  document.getElementById("clusterPack").style.display = v === "graph" ? "" : "none";
  document.getElementById("clusters").style.display = v === "list" ? "" : "none";
  renderClusters();
}

function renderPack(rows) {
  const svgEl = document.getElementById("clusterPack");
  // Clear.
  while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);
  const width = svgEl.clientWidth || 1080;
  const height = 560;
  svgEl.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const root = d3.hierarchy({children: rows})
    .sum(d => Math.max(1, d.member_count || 1))
    .sort((a, b) => b.value - a.value);
  const pack = d3.pack().size([width - 4, height - 4]).padding(3);
  pack(root);

  const svg = d3.select(svgEl);
  const node = svg.selectAll("g")
    .data(root.leaves())
    .join("g")
    .attr("transform", d => `translate(${d.x + 2},${d.y + 2})`);

  node.append("circle")
    .attr("r", d => d.r)
    .attr("fill", d => STATUS_COLOR[d.data.status] || "#888")
    .attr("fill-opacity", 0.78)
    .on("mouseover", (e, d) => showTooltip(e, d.data))
    .on("mousemove", (e) => moveTooltip(e))
    .on("mouseout", hideTooltip)
    .on("click", (e, d) => showDetail(d.data));

  // Label clusters whose radius is large enough to fit text.
  node.filter(d => d.r > 26)
    .append("text")
    .attr("dy", "0.3em")
    .text(d => {
      const lbl = (d.data.label || `#${d.data.cluster_id}`);
      const maxChars = Math.floor(d.r / 4);
      return lbl.length > maxChars ? lbl.slice(0, maxChars - 1) + "…" : lbl;
    });

  // Tiny clusters get just the id.
  node.filter(d => d.r > 14 && d.r <= 26)
    .append("text")
    .attr("dy", "0.3em")
    .style("font-size", "9px")
    .text(d => `#${d.data.cluster_id}`);
}

function _esc2(s) {
  return String(s ?? "").replace(/[&<>"']/g, ch =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
}

function showTooltip(e, c) {
  const t = document.getElementById("tooltip");
  const parts = (c.participants || []).slice(0, 5)
    .map(p => `${_esc2(p.person)}(${p.contribution_count || 0})`).join(", ");
  const srcs = Object.entries(c.source_breakdown || {})
    .map(([k,v]) => `${k}:${v}`).join(" · ");
  t.innerHTML = `<b>#${c.cluster_id} · ${_esc2(c.label || "(no label)")}</b>
    <div class="meta">${_esc2(c.status)} · ${c.member_count || 0} members · conf ${(c.confidence ?? 0).toFixed(2)}</div>
    <div class="meta">${_esc2(c.summary || "").slice(0, 200)}</div>
    <div class="meta">sources: ${srcs}</div>
    <div class="meta">top: ${parts}</div>`;
  t.classList.add("show");
  moveTooltip(e);
}

function moveTooltip(e) {
  const t = document.getElementById("tooltip");
  const pad = 14;
  let x = e.clientX + pad, y = e.clientY + pad;
  // Flip if near right edge.
  if (x + 360 > window.innerWidth) x = e.clientX - 360 - pad;
  t.style.left = (x + window.scrollX) + "px";
  t.style.top  = (y + window.scrollY) + "px";
}

function hideTooltip() {
  document.getElementById("tooltip").classList.remove("show");
}

function showDetail(c) {
  const parts = (c.participants || []).slice(0, 12).map(p =>
    `<span>${_esc2(p.person)} (${p.contribution_count || 0})</span>`).join("");
  const srcs = Object.entries(c.source_breakdown || {}).map(([k,v]) =>
    `<span>${_esc2(k)}: ${v}</span>`).join("");
  const decisions = (c.decisions || []).slice(0, 8).map(d =>
    `<li><span class="muted">${_esc2(d.evidence_subject || "")}</span> — ${_esc2(d.text || "")}</li>`).join("");
  const blockers = (c.blockers || []).slice(0, 8).map(b =>
    `<li>${_esc2(typeof b === "string" ? b : (b.text || JSON.stringify(b)))}</li>`).join("");
  document.getElementById("clusterDetail").innerHTML = `<div class="cluster" data-status="${_esc2(c.status)}">
    <h3>#${c.cluster_id} · ${_esc2(c.label || "(no label)")}</h3>
    <div class="meta">
      ${_esc2(c.status)} · ${c.member_count || 0} members · confidence ${(c.confidence ?? 0).toFixed(2)} ·
      last activity <span class="muted">${_esc2((c.last_activity_ts || "").slice(0,19))}</span>
    </div>
    <div class="summary">${_esc2(c.summary || "")}</div>
    <div class="chips"><span class="muted">sources:</span> ${srcs}</div>
    <div class="chips"><span class="muted">participants:</span> ${parts}</div>
    ${decisions ? `<details open><summary>decisions (${(c.decisions||[]).length})</summary><ul>${decisions}</ul></details>` : ""}
    ${blockers ? `<details><summary>blockers (${(c.blockers||[]).length})</summary><ul>${blockers}</ul></details>` : ""}
  </div>`;
  document.getElementById("clusterDetail").scrollIntoView({behavior:"smooth", block:"nearest"});
}

function renderClusters() {
  const rows = _clusterCache;
  if (!rows || !rows.length) {
    document.getElementById("clusters").innerHTML = `<div class="muted">no clusters returned</div>`;
    document.getElementById("clusterPack").innerHTML = "";
    return;
  }
  if (_view === "graph") {
    renderPack(rows);
    return;
  }
  // List view
  const html = rows.map(c => {
    const parts = (c.participants || []).slice(0, 8).map(p =>
      `<span>${_esc(p.person)} (${p.contribution_count || 0})</span>`).join("");
    const srcs = Object.entries(c.source_breakdown || {}).map(([k,v]) =>
      `<span>${_esc(k)}: ${v}</span>`).join("");
    const decisions = (c.decisions || []).slice(0, 5).map(d =>
      `<li><span class="muted">${_esc(d.evidence_subject || "")}</span> — ${_esc(d.text || "")}</li>`).join("");
    const blockers = (c.blockers || []).slice(0, 5).map(b =>
      `<li>${_esc(typeof b === "string" ? b : (b.text || JSON.stringify(b)))}</li>`).join("");
    return `<div class="cluster" data-status="${_esc(c.status)}">
      <h3>#${c.cluster_id} · ${_esc(c.label || "(no label)")}</h3>
      <div class="meta">
        ${_esc(c.status)} · ${c.member_count || 0} members ·
        confidence ${(c.confidence ?? 0).toFixed(2)} ·
        last activity <span class="muted">${_esc((c.last_activity_ts || "").slice(0,19))}</span>
      </div>
      <div class="summary">${_esc(c.summary || "")}</div>
      <div class="chips"><span class="muted">sources:</span> ${srcs}</div>
      <div class="chips"><span class="muted">participants:</span> ${parts}</div>
      ${decisions ? `<details><summary>decisions (${(c.decisions||[]).length})</summary><ul>${decisions}</ul></details>` : ""}
      ${blockers ? `<details><summary>blockers (${(c.blockers||[]).length})</summary><ul>${blockers}</ul></details>` : ""}
    </div>`;
  }).join("");
  document.getElementById("clusters").innerHTML = html;
}

async function loadClusters() {
  const st = document.getElementById("clusterStatus").value;
  const lim = document.getElementById("clusterLimit").value;
  const q = new URLSearchParams({limit: lim});
  if (st) q.set("status", st);
  const r = await fetch(`/api/clusters?${q}`);
  _clusterCache = await r.json();
  renderClusters();
}

async function loadDiscover() {
  // #discoverTable is created by refresh() (it lives inside the lanes HTML), so
  // loadDiscover is invoked from there once the div exists. Guard defensively.
  const el = document.getElementById("discoverTable");
  if (!el) return;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 10000);
    const resp = await fetch("/api/discover", {signal: ctrl.signal});
    clearTimeout(timer);
    const d = await resp.json();
    const groups = [
      ["OWNER CHANNELS (announcement/manager rooms — you're a member)", d.owner_channels, "ok"],
      ["AUTO-FULL (ready)", d.auto_full, "ok"],
      ["AUTO-TEAM (ready)", d.auto_team_involved, "ok"],
      ["NEEDS REVIEW", d.needs_review, "warn"],
      ["TEAM-SILENT (active, but no team activity — hand-promote to full if useful)", d.team_silent, "muted"],
    ];
    let html = `<div class="muted" style="margin:4px 0">generated ${_esc(d.generated_at||"?")} · window ${d.days||"?"}d · schedule ${_esc(d.sched)} · next ${_esc(d.next)}</div>`;
    for (const [title, rows, lvl] of groups) {
      if (!rows || !rows.length) continue;
      const trs = rows.map(c =>
        `<tr><td>${_esc(c.name)}</td><td>${_esc(c.kind)}</td>
             <td>${c.team_members||0}</td><td>${c.team_msgs||0}</td>
             <td>${c.total_msgs||0}</td><td class="muted">${_esc(c.mode||"")}</td></tr>`).join("");
      html += `<div class="finding ${lvl}" style="margin-top:8px"><b>${title}</b> (${rows.length})</div>
        <table><tr><th>name</th><th>kind</th><th>team</th><th>t-msg</th><th>all-msg</th><th>mode</th></tr>${trs}</table>`;
    }
    el.innerHTML = html || `<span class="muted">no proposals</span>`;
  } catch (e) {
    el.innerHTML = `<span class="muted">discover load error</span>`;
  }
}

async function refreshAll() {
  await refresh();
}

// loadDiscover is driven from inside refresh() (right after it injects the
// lanes HTML that contains #discoverTable, and before the chart fetches that
// can stall). So it is NOT called standalone here — that would run before the
// div exists. refresh's own interval re-runs it.
refreshAll();
loadLog();
loadClusters();
setInterval(refreshAll, 1_800_000);
setInterval(loadLog, 1_800_000);
setInterval(loadClusters, 1_800_000);
</script>
</body></html>
"""


# Standalone full-list view for every Slack channel (linked from the SLACK
# lane's per-channel detail "view all" link). Reuses /api/slack-channels, which
# already returns the complete list — only the lane render caps at 40.
CHANNELS_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<title>slack channels · all</title>
<style>
:root{
  color-scheme: dark;
  --bg:#0b0f14; --bg-deep:#080c10; --panel:#11161d; --panel-raised:#161c25;
  --hover:#0e1319; --line:#2a313b; --line-faint:#1a212b;
  --text:#d5d9e0; --text2:#a7afba; --text-strong:#ffffff;
  --muted:#6e7681; --muted2:#7a8497; --ink:#0b0f14;
  --overlay:#ffffff0d; --overlay-faint:#ffffff09;
  --green:#48d597; --yellow:#d5b248; --red:#d54848; --blue:#4d8eff;
  --purple:#9b8eff; --amber:#f2c14e; --accent:#f2c14e;
  --pill-ok-bg:#193b25; --pill-warn-bg:#3b2e19; --pill-fail-bg:#3b1919;
}
html[data-theme="light"]{
  color-scheme: light;
  --bg:#f6f7f9; --bg-deep:#eceef2; --panel:#ffffff; --panel-raised:#ffffff;
  --hover:#eef1f5; --line:#d3d8e0; --line-faint:#e6e9ee;
  --text:#1a1f26; --text2:#3f4753; --text-strong:#000000;
  --muted:#6b7480; --muted2:#8a929c; --ink:#0b0f14;
  --overlay:#0000000f; --overlay-faint:#00000008;
  --green:#1f9d63; --yellow:#9a7a16; --red:#cf3b3b; --blue:#2f6fe0;
  --purple:#6f5fe0; --amber:#c98a12; --accent:#c98a12;
  --pill-ok-bg:#d7f2e3; --pill-warn-bg:#f3ebcf; --pill-fail-bg:#f7dcdc;
}
html{ transition: background-color .15s ease, color .15s ease; }
#themeToggle{ position:fixed; top:10px; right:14px; z-index:1000; display:flex;
  background:var(--panel); border:1px solid var(--line); border-radius:6px;
  overflow:hidden; font:11px ui-monospace,Menlo,monospace; box-shadow:0 2px 8px #00000026; }
#themeToggle button{ background:transparent; color:var(--muted); border:0;
  padding:4px 10px; cursor:pointer; font:inherit; }
#themeToggle button:hover{ color:var(--text); }
#themeToggle button.on{ background:var(--blue); color:#fff; }
body { font: 13px ui-monospace,SFMono-Regular,Menlo,monospace; background:var(--bg);
       color:var(--text); max-width: 1100px; margin: 16px auto; padding: 0 16px; }
h1 { font-size:18px; margin:0 0 4px; letter-spacing:1px; }
.subtitle { color:var(--muted); margin-bottom:14px; font-size:11px; }
a { color:var(--blue); text-decoration:none; }
a:hover { text-decoration:underline; }
input { background:var(--panel); color:var(--text); border:1px solid var(--line); border-radius:3px;
        padding:5px 8px; font:inherit; width:260px; margin-bottom:10px; }
table { border-collapse:collapse; width:100%; font-size:12px; }
th { color:var(--muted); font-weight:normal; text-align:left; padding:4px 8px;
     border-bottom:1px solid var(--line); cursor:pointer; user-select:none; position:sticky; top:0; background:var(--bg); }
th:hover { color:var(--text); }
td { padding:4px 8px; }
tr:nth-child(even) td { background:var(--hover); }
.muted { color:var(--muted); }
.pill { font-size:10px; padding:1px 6px; border-radius:8px; background:var(--pill-ok-bg); color:var(--green); }
.pill.warn { background:var(--pill-warn-bg); color:var(--yellow); }
.tag { font-size:10px; color:var(--muted2); border:1px solid var(--line); border-radius:8px; padding:0 5px; margin-left:4px; }
</style></head>
<body>
<div id="themeToggle">
  <button data-t="auto">auto</button><button data-t="light">light</button><button data-t="dark">dark</button>
</div>
<script>
(function(){
  var KEY="dash-theme";
  function resolve(m){ if(m==="light"||m==="dark") return m;
    var h=new Date().getHours(); return (h>=19||h<7)?"dark":"light"; }
  function apply(m){ document.documentElement.setAttribute("data-theme", resolve(m));
    var bs=document.querySelectorAll("#themeToggle button");
    for(var i=0;i<bs.length;i++) bs[i].classList.toggle("on", bs[i].dataset.t===m); }
  var mode=localStorage.getItem(KEY)||"auto";
  apply(mode);
  document.addEventListener("click",function(e){
    var b=e.target.closest&&e.target.closest("#themeToggle button"); if(!b) return;
    mode=b.dataset.t; localStorage.setItem(KEY,mode); apply(mode); });
  setInterval(function(){ if((localStorage.getItem(KEY)||"auto")==="auto") apply("auto"); }, 600000);
})();
</script>
<h1>SLACK channels — all</h1>
<div class="subtitle"><a href="/">← back to dashboard</a> · <span id="count">loading…</span></div>
<input id="filter" placeholder="filter by name…" autocomplete="off">
<table id="tbl"><thead><tr>
  <th data-k="name">name</th>
  <th data-k="events">events</th>
  <th data-k="last_activity">last msg</th>
  <th data-k="checked_ts">checked</th>
  <th data-k="status">status</th>
  <th data-k="kind">kind</th>
</tr></thead><tbody id="rows"><tr><td colspan="6" class="muted">loading…</td></tr></tbody></table>
<script>
const FRESH_MS = 45 * 60 * 1000;
function _rel(iso) {
  if (!iso) return "—";
  try {
    const dt = new Date(iso);
    const secs = (Date.now() - dt.getTime()) / 1000;
    if (secs < 3600) return `${Math.floor(secs/60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs/3600)}h ago`;
    return `${Math.floor(secs/86400)}d ago`;
  } catch (e) { return "?"; }
}
function _esc(s){ return String(s==null?"":s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
function _status(c) {
  if (!c.has_cursor) return `<span class="pill warn">no cursor</span>`;
  if (!c.checked_ts) return `<span class="muted">? unpolled</span>`;
  const lag = Date.now() - new Date(c.checked_ts).getTime();
  if (lag <= FRESH_MS) return `<span class="pill">✓ up-to-date</span>`;
  return `<span class="pill warn">⚠ lag ${_rel(c.checked_ts).replace(" ago","")}</span>`;
}
function _kind(c){ return c.is_archived ? "archived" : (c.is_private ? "private" : "public"); }
let DATA = [], sortKey = "events", sortDir = -1;
function render() {
  const f = (document.getElementById("filter").value || "").toLowerCase();
  let rows = DATA.filter(c => !f || (c.name||"").toLowerCase().includes(f));
  rows.sort((a,b) => {
    let av, bv;
    if (sortKey === "status") { av = a.has_cursor?1:0; bv = b.has_cursor?1:0; }
    else if (sortKey === "kind") { av = _kind(a); bv = _kind(b); }
    else { av = a[sortKey]; bv = b[sortKey]; }
    if (av == null) av = ""; if (bv == null) bv = "";
    return (av < bv ? -1 : av > bv ? 1 : 0) * sortDir;
  });
  document.getElementById("count").textContent =
    `${rows.length}${f ? " of " + DATA.length : ""} channels`;
  document.getElementById("rows").innerHTML = rows.map(c =>
    `<tr><td>${_esc(c.name)}</td>
         <td>${(c.events||0).toLocaleString()}</td>
         <td class="muted">${_rel(c.last_activity)}</td>
         <td class="muted">${c.checked_ts ? _rel(c.checked_ts) : "—"}</td>
         <td>${_status(c)}</td>
         <td class="muted">${_kind(c)}</td></tr>`).join("")
    || `<tr><td colspan="6" class="muted">no matches</td></tr>`;
}
document.querySelectorAll("th").forEach(th => th.onclick = () => {
  const k = th.dataset.k;
  if (sortKey === k) sortDir = -sortDir; else { sortKey = k; sortDir = (k==="name"||k==="kind")?1:-1; }
  render();
});
document.getElementById("filter").oninput = render;
(async () => {
  try {
    DATA = await (await fetch("/api/slack-channels")).json();
    render();
  } catch (e) {
    document.getElementById("rows").innerHTML = `<tr><td colspan="6" class="muted">load error</td></tr>`;
  }
})();
</script>
</body></html>
"""


# Team-leaves Gantt view (linked from the LEAVES lane on the main dashboard).
# Reads /api/leaves and lays out a horizontal timeline: one row per person,
# one bar per leave, coloured by reason. Today is marked; weekends shaded.
LEAVES_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<title>team leaves · gantt</title>
<style>
:root{
  color-scheme: dark;
  --bg:#0b0f14; --bg-deep:#080c10; --panel:#11161d; --panel-raised:#161c25;
  --hover:#0e1319; --line:#2a313b; --line-faint:#1a212b;
  --text:#d5d9e0; --text2:#a7afba; --text-strong:#ffffff;
  --muted:#6e7681; --muted2:#7a8497; --ink:#0b0f14;
  --overlay:#ffffff0d; --overlay-faint:#ffffff09;
  --green:#48d597; --yellow:#d5b248; --red:#d54848; --blue:#4d8eff;
  --purple:#9b8eff; --amber:#f2c14e; --accent:#f2c14e;
  --pill-ok-bg:#193b25; --pill-warn-bg:#3b2e19; --pill-fail-bg:#3b1919;
}
html[data-theme="light"]{
  color-scheme: light;
  --bg:#f6f7f9; --bg-deep:#eceef2; --panel:#ffffff; --panel-raised:#ffffff;
  --hover:#eef1f5; --line:#d3d8e0; --line-faint:#e6e9ee;
  --text:#1a1f26; --text2:#3f4753; --text-strong:#000000;
  --muted:#6b7480; --muted2:#8a929c; --ink:#0b0f14;
  --overlay:#0000000f; --overlay-faint:#00000008;
  --green:#1f9d63; --yellow:#9a7a16; --red:#cf3b3b; --blue:#2f6fe0;
  --purple:#6f5fe0; --amber:#c98a12; --accent:#c98a12;
  --pill-ok-bg:#d7f2e3; --pill-warn-bg:#f3ebcf; --pill-fail-bg:#f7dcdc;
}
html{ transition: background-color .15s ease, color .15s ease; }
#themeToggle{ position:fixed; top:10px; right:14px; z-index:1000; display:flex;
  background:var(--panel); border:1px solid var(--line); border-radius:6px;
  overflow:hidden; font:11px ui-monospace,Menlo,monospace; box-shadow:0 2px 8px #00000026; }
#themeToggle button{ background:transparent; color:var(--muted); border:0;
  padding:4px 10px; cursor:pointer; font:inherit; }
#themeToggle button:hover{ color:var(--text); }
#themeToggle button.on{ background:var(--blue); color:#fff; }
body { font: 13px ui-monospace,SFMono-Regular,Menlo,monospace; background:var(--bg);
       color:var(--text); max-width: 1400px; margin: 16px auto; padding: 0 16px; }
h1 { font-size:18px; margin:0 0 4px; letter-spacing:1px; }
h2 { font-size:13px; margin:18px 0 6px; color:var(--text2); }
.subtitle { color:var(--muted); margin-bottom:14px; font-size:11px; }
a { color:var(--blue); text-decoration:none; }
a:hover { text-decoration:underline; }
.legend { display:flex; gap:14px; flex-wrap:wrap; font-size:11px; color:var(--text2); margin:10px 0; }
.legend span::before { content:"■"; margin-right:5px; }
.legend .vacation::before { color:var(--blue); }
.legend .wfh::before      { color:var(--green); }
.legend .sick::before     { color:var(--red); }
.legend .holiday::before  { color:var(--purple); }
.legend .ooo::before      { color:var(--yellow); }
.legend .other::before    { color:var(--muted); }
.gantt { border:1px solid var(--line-faint); border-radius:4px; overflow-x:auto; background:var(--bg-deep); }
.axis-month, .axis-day, .grow { display:flex; }
.axis-month div, .axis-day div { flex:0 0 auto; box-sizing:border-box; text-align:center;
       color:var(--muted); font-size:10px; border-left:1px solid var(--panel-raised); }
.axis-month { border-bottom:1px solid var(--line-faint); }
.axis-month div { padding:3px 0; color:var(--text2); border-left:1px solid var(--line); }
.axis-day { border-bottom:1px solid var(--line-faint); }
.axis-day div { padding:2px 0; line-height:1.25; }
.axis-day .wd { display:block; font-size:8px; color:var(--muted2); letter-spacing:0; }
.axis-day .we { background:var(--overlay); }
.axis-day .we, .axis-day .we .wd { color:var(--muted2); }
.spacer { flex:0 0 auto; position:sticky; left:0; z-index:3; background:var(--bg-deep);
          border-right:1px solid var(--line); }
.row { display:flex; align-items:stretch; border-top:1px solid var(--panel); }
.row:hover { background:var(--hover); }
.label { flex:0 0 auto; position:sticky; left:0; z-index:2; background:inherit;
         border-right:1px solid var(--line); padding:0 8px; display:flex; align-items:center;
         font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.row:hover .label { background:var(--hover); }
.track { flex:0 0 auto; position:relative; height:26px; }
.bar { position:absolute; top:4px; bottom:4px; border-radius:3px; font-size:10px;
       color:var(--ink); display:flex; align-items:center; padding:0 5px; overflow:hidden;
       white-space:nowrap; cursor:default; box-shadow:inset 0 0 0 1px #0006; }
.bar.open-l { border-top-left-radius:0; border-bottom-left-radius:0; }
.bar.open-r { border-top-right-radius:0; border-bottom-right-radius:0; }
.bar a { color:inherit; text-decoration:none; }
.weekend { position:absolute; top:0; bottom:0; background:var(--overlay); pointer-events:none; }
.todayline { position:absolute; top:0; bottom:0; width:2px; background:var(--amber);
             pointer-events:none; z-index:1; }
.todaylabel { position:absolute; top:0; font-size:9px; color:var(--amber);
              transform:translateX(-50%); pointer-events:none; }
table { border-collapse:collapse; width:100%; font-size:12px; margin-top:4px; }
th { color:var(--muted); font-weight:normal; text-align:left; padding:4px 8px; border-bottom:1px solid var(--line); }
td { padding:4px 8px; }
tr:nth-child(even) td { background:var(--hover); }
.muted { color:var(--muted); }
.empty { color:var(--muted); padding:18px; }
</style></head>
<body>
<div id="themeToggle">
  <button data-t="auto">auto</button><button data-t="light">light</button><button data-t="dark">dark</button>
</div>
<script>
(function(){
  var KEY="dash-theme";
  function resolve(m){ if(m==="light"||m==="dark") return m;
    var h=new Date().getHours(); return (h>=19||h<7)?"dark":"light"; }
  function apply(m){ document.documentElement.setAttribute("data-theme", resolve(m));
    var bs=document.querySelectorAll("#themeToggle button");
    for(var i=0;i<bs.length;i++) bs[i].classList.toggle("on", bs[i].dataset.t===m); }
  var mode=localStorage.getItem(KEY)||"auto";
  apply(mode);
  document.addEventListener("click",function(e){
    var b=e.target.closest&&e.target.closest("#themeToggle button"); if(!b) return;
    mode=b.dataset.t; localStorage.setItem(KEY,mode); apply(mode); });
  setInterval(function(){ if((localStorage.getItem(KEY)||"auto")==="auto") apply("auto"); }, 600000);
})();
</script>
<h1>TEAM LEAVES — gantt</h1>
<div class="subtitle"><a href="/">← back to dashboard</a> · <span id="count">loading…</span></div>
<div class="legend">
  <span class="vacation">vacation</span><span class="wfh">wfh</span>
  <span class="sick">sick</span><span class="holiday">holiday</span>
  <span class="ooo">ooo</span><span class="other">other / untagged</span>
</div>
<div id="gantt" class="gantt"><div class="empty">loading…</div></div>
<h2>Ambiguous — date TBD</h2>
<div id="ambig"><div class="muted">loading…</div></div>

<script>
const DAY_W = 22, LABEL_W = 150;
const COLOR = { vacation:"#4d8eff", wfh:"#48d597", sick:"#d54848",
                holiday:"#9b8eff", ooo:"#d5b248", other:"#6e7681" };
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const WD = ["S","M","T","W","T","F","S"];

function _esc(s){ return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function _d(s){ const [y,m,d] = s.split("-").map(Number); return Date.UTC(y, m-1, d); }
function _diffDays(a, b){ return Math.round((_d(b) - _d(a)) / 86400000); }
function _color(reason){ return COLOR[(reason||"other").toLowerCase()] || COLOR.other; }
function _fmtRange(s, e){ if(!e || e===s) return s; return `${s} → ${e}`; }

async function load() {
  let data;
  try { data = await (await fetch("/api/leaves")).json(); }
  catch(e){ document.getElementById("gantt").innerHTML = `<div class="empty">load error</div>`; return; }

  const today = data.today, wStart = data.window_start, wEnd = data.window_end;
  const nDays = _diffDays(wStart, wEnd) + 1;
  const trackW = nDays * DAY_W;
  const leaves = data.leaves || [];

  document.getElementById("count").textContent =
    `${leaves.length} dated leave(s) · window ${wStart} → ${wEnd} · today ${today}`;

  // Group by person; sort active-today first, then by earliest start.
  const byPerson = {};
  for (const l of leaves) (byPerson[l.actor] = byPerson[l.actor] || []).push(l);
  const _end = l => l.date_end || l.date_start;
  const people = Object.keys(byPerson).sort((a, b) => {
    const aAct = byPerson[a].some(l => l.date_start <= today && _end(l) >= today);
    const bAct = byPerson[b].some(l => l.date_start <= today && _end(l) >= today);
    if (aAct !== bAct) return aAct ? -1 : 1;
    const aMin = byPerson[a].reduce((m, l) => l.date_start < m ? l.date_start : m, "9999");
    const bMin = byPerson[b].reduce((m, l) => l.date_start < m ? l.date_start : m, "9999");
    return aMin < bMin ? -1 : aMin > bMin ? 1 : a.localeCompare(b);
  });

  // ── axis (month row + day row) ──
  let monthRow = `<div class="spacer" style="width:${LABEL_W}px"></div>`;
  let dayRow   = `<div class="spacer" style="width:${LABEL_W}px"></div>`;
  let curMonth = -1, monthSpan = 0, monthLabel = "";
  const flushMonth = () => { if (monthSpan) monthRow +=
      `<div style="width:${monthSpan*DAY_W}px">${monthLabel}</div>`; };
  for (let i = 0; i < nDays; i++) {
    const dt = new Date(_d(wStart) + i*86400000);
    const mo = dt.getUTCMonth();
    if (mo !== curMonth) { flushMonth(); curMonth = mo; monthSpan = 0;
      monthLabel = `${MONTHS[mo]} ${dt.getUTCFullYear()}`; }
    monthSpan++;
    const wd = dt.getUTCDay();
    const we = (wd === 0 || wd === 6) ? " we" : "";
    dayRow += `<div class="${we}" style="width:${DAY_W}px"><span class="wd">${WD[wd]}</span>${dt.getUTCDate()}</div>`;
  }
  flushMonth();

  // ── background layer (weekend shading + today line) ──
  let bg = "";
  for (let i = 0; i < nDays; i++) {
    const dt = new Date(_d(wStart) + i*86400000);
    const wd = dt.getUTCDay();
    if (wd === 0 || wd === 6)
      bg += `<div class="weekend" style="left:${i*DAY_W}px;width:${DAY_W}px"></div>`;
  }
  const todayOff = _diffDays(wStart, today);
  if (todayOff >= 0 && todayOff < nDays) {
    const x = todayOff*DAY_W + DAY_W/2;
    bg += `<div class="todayline" style="left:${x}px"></div>`;
  }

  // ── person rows ──
  let rowsHtml = "";
  for (const p of people) {
    let bars = "";
    for (const l of byPerson[p]) {
      const s = l.date_start, e = l.date_end || l.date_start;
      let startOff = _diffDays(wStart, s);
      let endOff   = _diffDays(wStart, e);
      const openL = startOff < 0, openR = endOff > nDays - 1;
      startOff = Math.max(0, startOff);
      endOff = Math.min(nDays - 1, endOff);
      if (endOff < startOff) continue;
      const left = startOff * DAY_W;
      const width = (endOff - startOff + 1) * DAY_W - 2;
      const days = _diffDays(s, e) + 1;
      const tip = `${p} · ${_fmtRange(s, l.date_end)} · ${days}d`
                + `${l.reason ? " · " + l.reason : ""}`
                + `${l.channel_name ? " · #" + l.channel_name : ""}`
                + `${l.body_excerpt ? "\\n" + l.body_excerpt : ""}`;
      const lbl = (l.reason || "leave") + (days > 1 ? ` ${days}d` : "");
      const inner = l.url
        ? `<a href="${_esc(l.url)}" target="_blank" title="${_esc(tip)}">${_esc(lbl)}</a>`
        : `<span title="${_esc(tip)}">${_esc(lbl)}</span>`;
      bars += `<div class="bar ${openL?"open-l":""} ${openR?"open-r":""}"
                 style="left:${left}px;width:${Math.max(width,6)}px;background:${_color(l.reason)}"
                 title="${_esc(tip)}">${inner}</div>`;
    }
    rowsHtml += `<div class="row">
        <div class="label" style="width:${LABEL_W}px">${_esc(p)}</div>
        <div class="track" style="width:${trackW}px">${bars}</div>
      </div>`;
  }

  const g = document.getElementById("gantt");
  if (!people.length) { g.innerHTML = `<div class="empty">no dated leaves in window</div>`; }
  else {
    g.innerHTML =
      `<div class="axis-month">${monthRow}</div>` +
      `<div class="axis-day">${dayRow}</div>` +
      `<div style="position:relative">` +
        `<div style="position:absolute;left:${LABEL_W}px;top:0;bottom:0;width:${trackW}px">${bg}</div>` +
        rowsHtml +
      `</div>`;
    // Scroll so today sits ~1/3 in from the left.
    g.scrollLeft = Math.max(0, todayOff*DAY_W - g.clientWidth/3);
  }

  // ── ambiguous table ──
  const ambig = data.ambiguous || [];
  const ae = document.getElementById("ambig");
  if (!ambig.length) ae.innerHTML = `<div class="muted">none</div>`;
  else ae.innerHTML = `<table><tr><th>person</th><th>mentioned</th><th>reason</th>
      <th>channel</th><th>excerpt</th><th>link</th></tr>` +
    ambig.map(l => `<tr><td>${_esc(l.actor)}</td>
        <td class="muted">${_esc((l.mentioned_at||"").slice(0,10))}</td>
        <td>${_esc(l.reason||"-")}</td>
        <td class="muted">${_esc(l.channel_name ? "#"+l.channel_name : "-")}</td>
        <td class="muted">${_esc((l.body_excerpt||"").slice(0,80))}</td>
        <td>${l.url ? `<a href="${_esc(l.url)}" target="_blank">view</a>` : "-"}</td></tr>`).join("")
    + `</table>`;
}
load();
setInterval(load, 1_800_000);
</script>
</body></html>
"""


# Team-leaves Gantt — v2. Cleaner layout: weekly axis ticks (not 74 daily
# numbers), taller rows with avatars + "out now" markers, weekly gridlines,
# shaded today column, and a custom hover card. Same /api/leaves backend.
LEAVES_V2_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<title>team leaves · gantt v2</title>
<style>
:root{
  color-scheme: dark;
  --bg:#0b0f14; --bg-deep:#080c10; --panel:#11161d; --panel-raised:#161c25;
  --hover:#0e1319; --line:#2a313b; --line-faint:#1a212b;
  --text:#d5d9e0; --text2:#a7afba; --text-strong:#ffffff;
  --muted:#6e7681; --muted2:#7a8497; --ink:#0b0f14;
  --overlay:#ffffff0d; --overlay-faint:#ffffff09;
  --green:#48d597; --yellow:#d5b248; --red:#d54848; --blue:#4d8eff;
  --purple:#9b8eff; --amber:#f2c14e; --accent:#f2c14e;
  --pill-ok-bg:#193b25; --pill-warn-bg:#3b2e19; --pill-fail-bg:#3b1919;
}
html[data-theme="light"]{
  color-scheme: light;
  --bg:#f6f7f9; --bg-deep:#eceef2; --panel:#ffffff; --panel-raised:#ffffff;
  --hover:#eef1f5; --line:#d3d8e0; --line-faint:#e6e9ee;
  --text:#1a1f26; --text2:#3f4753; --text-strong:#000000;
  --muted:#6b7480; --muted2:#8a929c; --ink:#0b0f14;
  --overlay:#0000000f; --overlay-faint:#00000008;
  --green:#1f9d63; --yellow:#9a7a16; --red:#cf3b3b; --blue:#2f6fe0;
  --purple:#6f5fe0; --amber:#c98a12; --accent:#c98a12;
  --pill-ok-bg:#d7f2e3; --pill-warn-bg:#f3ebcf; --pill-fail-bg:#f7dcdc;
}
html{ transition: background-color .15s ease, color .15s ease; }
#themeToggle{ position:fixed; top:10px; right:14px; z-index:1000; display:flex;
  background:var(--panel); border:1px solid var(--line); border-radius:6px;
  overflow:hidden; font:11px ui-monospace,Menlo,monospace; box-shadow:0 2px 8px #00000026; }
#themeToggle button{ background:transparent; color:var(--muted); border:0;
  padding:4px 10px; cursor:pointer; font:inherit; }
#themeToggle button:hover{ color:var(--text); }
#themeToggle button.on{ background:var(--blue); color:#fff; }
* { box-sizing:border-box; }
body { font: 13px ui-monospace,SFMono-Regular,Menlo,monospace; background:var(--bg);
       color:var(--text); max-width:1500px; margin:18px auto; padding:0 18px; }
h1 { font-size:17px; margin:0 0 4px; letter-spacing:1px; }
h2 { font-size:13px; margin:22px 0 8px; color:var(--text2); }
.subtitle { color:var(--muted); margin-bottom:14px; font-size:11px; }
a { color:var(--blue); text-decoration:none; } a:hover { text-decoration:underline; }
.toprow { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }
.legend { display:flex; gap:16px; flex-wrap:wrap; font-size:11px; color:var(--text2); margin:12px 0; }
.legend span { display:inline-flex; align-items:center; gap:6px; }
.legend i { width:11px; height:11px; border-radius:3px; display:inline-block; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px;
        overflow:hidden; }
.gantt { overflow-x:auto; }
.inner { position:relative; min-width:max-content; }
/* header */
.hdr { position:sticky; top:0; z-index:4; background:var(--panel);
       border-bottom:1px solid var(--line); }
.hdr-month, .hdr-week { display:flex; }
.corner { flex:0 0 auto; position:sticky; left:0; z-index:5; background:var(--panel);
          border-right:1px solid var(--line); }
.hdr-month .mo { flex:0 0 auto; text-align:left; padding:6px 0 4px 10px; color:var(--text);
                 font-size:12px; border-left:1px solid var(--line); letter-spacing:.5px; }
.hdr-day { display:flex; }
.hdr-day .dn { flex:0 0 auto; text-align:center; font-size:10px; color:var(--muted2);
               padding:3px 0; line-height:1.3; }
.hdr-day .dn .wd { display:block; font-size:8px; color:var(--muted2); letter-spacing:0; }
.hdr-day .dn.we { color:var(--muted2); background:var(--overlay-faint); }
.hdr-day .dn.we .wd { color:var(--muted2); }
.hdr-day .dn.mon { box-shadow:inset 1px 0 0 var(--line); }
.hdr-day .dn.td { color:var(--ink); background:var(--accent); font-weight:bold; border-radius:4px; }
.hdr-day .dn.td .wd { color:var(--ink); }
/* body */
.rowwrap { position:relative; }
.bg { position:absolute; top:0; bottom:0; pointer-events:none; }
.weekend { background:var(--overlay-faint); }
.weekgrid { width:1px; background:var(--line); }
.todaycol { background:#f2c14e14; }
.todayline { width:2px; background:var(--accent); z-index:1; }
.todaytag { position:absolute; top:0; transform:translateX(-50%); background:var(--accent);
            color:var(--ink); font-size:9px; padding:1px 5px; border-radius:0 0 4px 4px;
            font-weight:bold; z-index:6; }
/* company holidays — calendar-driven column shading (purple). fixed = solid,
   optional/restricted = fainter. */
.holcol { background:#9b8eff24; }
/* optional/restricted: faint diagonal hatch instead of a flat fill, so it
   reads as "not a full company holiday" at a glance. */
.holcol.opt { background:repeating-linear-gradient(45deg,#9b8eff14 0,#9b8eff14 3px,
              transparent 3px,transparent 7px); }
.holline { width:2px; background:var(--purple); opacity:.6; z-index:1; }
.holline.opt { width:0; border-left:2px dashed var(--purple); background:none; opacity:.7; }
.holtag { position:absolute; top:0; transform:translateX(-50%); background:var(--purple);
          color:var(--ink); font-size:9px; padding:1px 5px; border-radius:0 0 4px 4px;
          font-weight:bold; z-index:6; white-space:nowrap; cursor:help; max-width:130px;
          overflow:hidden; text-overflow:ellipsis; }
/* optional: hollow/outlined tag (transparent fill, purple text+border). */
.holtag.opt { background:var(--bg); color:var(--purple); border:1px dashed var(--purple);
              border-top:0; font-weight:normal; }
.holtag .opt-mark { opacity:.75; font-weight:normal; }
.row { display:flex; align-items:center; height:46px; border-top:1px solid var(--line-faint);
       position:relative; }
.row:hover { background:var(--hover); }
.row.active { background:var(--overlay-faint); }
.person { flex:0 0 auto; position:sticky; left:0; z-index:3; background:inherit;
          border-right:1px solid var(--line); display:flex; align-items:center; gap:9px;
          padding:0 12px; height:100%; }
.row:hover .person, .row.active .person { background:var(--hover); }
.avatar { width:26px; height:26px; border-radius:50%; flex:0 0 auto; display:flex;
          align-items:center; justify-content:center; font-size:10px; color:var(--ink);
          font-weight:bold; }
.pname { font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.pname .dot { color:var(--green); margin-right:4px; }
.psub { font-size:9px; color:var(--muted); }
.track { flex:0 0 auto; position:relative; height:100%; }
.bar { position:absolute; height:26px; top:10px; border-radius:6px; font-size:11px;
       color:var(--ink); display:flex; align-items:center; padding:0 9px; overflow:hidden;
       white-space:nowrap; cursor:pointer; box-shadow:0 1px 3px #0007, inset 0 0 0 1px #ffffff22;
       transition:filter .1s; }
.bar:hover { filter:brightness(1.12); }
.bar.open-l { border-top-left-radius:0; border-bottom-left-radius:0; }
.bar.open-r { border-top-right-radius:0; border-bottom-right-radius:0; }
.bar b { font-weight:600; }
.bar .d { opacity:.7; margin-left:5px; font-weight:400; }
.bar-ext { position:absolute; height:26px; top:10px; display:flex; align-items:center;
           font-size:11px; white-space:nowrap; pointer-events:none; }
.bar-ext b { font-weight:600; }
.bar-ext .d { opacity:.6; margin-left:4px; }
/* tooltip */
#tt { position:fixed; z-index:99; background:var(--panel-raised); border:1px solid var(--line);
      border-radius:6px; padding:9px 11px; font-size:11px; max-width:340px; opacity:0;
      pointer-events:none; transition:opacity .1s; box-shadow:0 6px 20px #000b; }
#tt.show { opacity:1; }
#tt .t { color:var(--text-strong); font-size:12px; margin-bottom:3px; }
#tt .m { color:var(--text2); margin-top:3px; }
#tt .ex { color:var(--muted2); margin-top:5px; font-style:italic; }
.empty { color:var(--muted); padding:22px; }
table { border-collapse:collapse; width:100%; font-size:12px; margin-top:4px; }
th { color:var(--muted); font-weight:normal; text-align:left; padding:5px 9px; border-bottom:1px solid var(--line); }
td { padding:5px 9px; } tr:nth-child(even) td { background:var(--hover); }
.muted { color:var(--muted); }
</style></head>
<body>
<div id="themeToggle">
  <button data-t="auto">auto</button><button data-t="light">light</button><button data-t="dark">dark</button>
</div>
<script>
(function(){
  var KEY="dash-theme";
  function resolve(m){ if(m==="light"||m==="dark") return m;
    var h=new Date().getHours(); return (h>=19||h<7)?"dark":"light"; }
  function apply(m){ document.documentElement.setAttribute("data-theme", resolve(m));
    var bs=document.querySelectorAll("#themeToggle button");
    for(var i=0;i<bs.length;i++) bs[i].classList.toggle("on", bs[i].dataset.t===m); }
  var mode=localStorage.getItem(KEY)||"auto";
  apply(mode);
  document.addEventListener("click",function(e){
    var b=e.target.closest&&e.target.closest("#themeToggle button"); if(!b) return;
    mode=b.dataset.t; localStorage.setItem(KEY,mode); apply(mode); });
  setInterval(function(){ if((localStorage.getItem(KEY)||"auto")==="auto") apply("auto"); }, 600000);
})();
</script>
<div class="toprow">
  <h1>TEAM LEAVES</h1>
  <span class="subtitle"><a href="/">← dashboard</a> · <a href="/leaves-v1">old view</a> · <span id="count">loading…</span></span>
</div>
<div class="legend">
  <span><i style="background:#4d8eff"></i>vacation</span>
  <span><i style="background:#48d597"></i>wfh</span>
  <span><i style="background:#e0564f"></i>sick</span>
  <span><i style="background:#9b8eff"></i>holiday</span>
  <span><i style="background:#d5b248"></i>ooo</span>
  <span><i style="background:#7a8497"></i>other</span>
  <span style="margin-left:8px"><i style="background:#9b8eff"></i>holiday · fixed</span>
  <span><i style="background:transparent;box-shadow:inset 0 0 0 1.5px #9b8eff"></i>holiday · optional</span>
</div>
<div class="card gantt"><div id="inner" class="inner"><div class="empty">loading…</div></div></div>
<h2>Ambiguous — date TBD</h2>
<div id="ambig"><div class="muted">loading…</div></div>
<div id="tt"></div>

<script>
const DAY_W = 24, PERSON_W = 184, ROW_H = 46;
const COLOR = { vacation:"#4d8eff", wfh:"#48d597", sick:"#e0564f",
                holiday:"#9b8eff", ooo:"#d5b248", other:"#7a8497" };
const AV = ["#4d8eff","#48d597","#e0564f","#9b8eff","#d5b248","#5ec8d8","#e08a4f","#c77dff"];
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const WD3 = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];

function _esc(s){ return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function _d(s){ const [y,m,d]=s.split("-").map(Number); return Date.UTC(y,m-1,d); }
function _diff(a,b){ return Math.round((_d(b)-_d(a))/86400000); }
function _color(r){ return COLOR[(r||"other").toLowerCase()]||COLOR.other; }
function _hash(s){ let h=0; for(const c of s) h=(h*31+c.charCodeAt(0))|0; return Math.abs(h); }
function _initials(s){ const p=s.split(/[-_ ]+/).filter(Boolean);
  return ((p[0]?.[0]||"")+(p[1]?.[0]||"")).toUpperCase()||"?"; }
function _fmtRange(s,e){ if(!e||e===s) return s; return `${s} → ${e}`; }
function _human(s){ const [y,m,d]=s.split("-"); return `${+d} ${MONTHS[+m-1]}`; }

let BARS = [];
const tt = document.getElementById("tt");
function showTip(ev, l, p){
  const days = _diff(l.date_start, l.date_end||l.date_start)+1;
  tt.innerHTML = `<div class="t">${_esc(p)}</div>`
    + `<div class="m">${_esc(_fmtRange(l.date_start, l.date_end))} · ${days}d`
    + `${l.reason?" · "+_esc(l.reason):""}</div>`
    + `${l.channel_name?`<div class="m">#${_esc(l.channel_name)}</div>`:""}`
    + `${l.body_excerpt?`<div class="ex">"${_esc(l.body_excerpt.slice(0,160))}"</div>`:""}`
    + `${l.url?`<div class="m">click to open in slack ↗</div>`:""}`;
  tt.classList.add("show"); moveTip(ev);
}
function moveTip(ev){ let x=ev.clientX+14, y=ev.clientY+14;
  if(x+350>window.innerWidth) x=ev.clientX-350; tt.style.left=x+"px"; tt.style.top=y+"px"; }
function hideTip(){ tt.classList.remove("show"); }

async function load(){
  let data;
  try { data = await (await fetch("/api/leaves")).json(); }
  catch(e){ document.getElementById("inner").innerHTML=`<div class="empty">load error</div>`; return; }
  const today=data.today, wStart=data.window_start, wEnd=data.window_end;
  const nDays=_diff(wStart,wEnd)+1, trackW=nDays*DAY_W;
  const leaves=data.leaves||[];
  const _end=l=>l.date_end||l.date_start;

  document.getElementById("count").textContent =
    `${leaves.length} dated leave(s) · ${_human(wStart)} → ${_human(wEnd)} · today ${_human(today)}`;

  const byP={}; for(const l of leaves)(byP[l.actor]=byP[l.actor]||[]).push(l);
  const people=Object.keys(byP).sort((a,b)=>{
    const aA=byP[a].some(l=>l.date_start<=today&&_end(l)>=today);
    const bA=byP[b].some(l=>l.date_start<=today&&_end(l)>=today);
    if(aA!==bA) return aA?-1:1;
    const am=byP[a].reduce((m,l)=>l.date_start<m?l.date_start:m,"9999");
    const bm=byP[b].reduce((m,l)=>l.date_start<m?l.date_start:m,"9999");
    return am<bm?-1:am>bm?1:a.localeCompare(b);
  });

  // month band
  let monthBand=`<div class="corner mo-corner" style="width:${PERSON_W}px"></div>`;
  let curMo=-1, span=0, lbl="";
  const flush=()=>{ if(span) monthBand+=`<div class="mo" style="width:${span*DAY_W}px">${lbl}</div>`; };
  for(let i=0;i<nDays;i++){ const dt=new Date(_d(wStart)+i*86400000); const mo=dt.getUTCMonth();
    if(mo!==curMo){ flush(); curMo=mo; span=0; lbl=`${MONTHS[mo]} ${dt.getUTCFullYear()}`; } span++; }
  flush();

  // Company holidays keyed by ISO date (fixed vs optional drives shade).
  const holByDate = {};
  for(const h of (data.holidays||[])) holByDate[h.date] = h;

  // day-number axis + background layers (faint daily gridlines + weekend shade)
  const tOff=_diff(wStart,today);
  let dayNums="", holTags="", bg=`<div class="bg" style="left:0;width:${trackW}px;`
    + `background-image:repeating-linear-gradient(to right,#ffffff08 0,#ffffff08 1px,`
    + `transparent 1px,transparent ${DAY_W}px)"></div>`;
  for(let i=0;i<nDays;i++){
    const dt=new Date(_d(wStart)+i*86400000), wd=dt.getUTCDay(), x=i*DAY_W;
    const we=(wd===0||wd===6);
    if(we) bg+=`<div class="bg weekend" style="left:${x}px;width:${DAY_W}px"></div>`;
    // ISO date for this column (UTC-safe).
    const iso=dt.toISOString().slice(0,10);
    const hol=holByDate[iso];
    if(hol){
      const opt=hol.type!=="holiday";
      bg+=`<div class="bg holcol${opt?" opt":""}" style="left:${x}px;width:${DAY_W}px"></div>`;
      bg+=`<div class="bg holline${opt?" opt":""}" style="left:${x+DAY_W/2}px"></div>`;
      const tip=`${hol.occasion} · ${opt?"optional / restricted holiday":"fixed company holiday"} · ${iso}`;
      const optMark=opt?` <span class="opt-mark">⚬ opt</span>`:"";
      holTags+=`<div class="holtag${opt?" opt":""}" style="left:${x+DAY_W/2}px" title="${_esc(tip)}">${_esc(hol.occasion)}${optMark}</div>`;
    }
    const cls=`dn${we?" we":""}${i===tOff?" td":""}${wd===1?" mon":""}`;
    dayNums+=`<div class="${cls}" style="width:${DAY_W}px"><span class="wd">${WD3[wd]}</span>${dt.getUTCDate()}</div>`;
  }
  if(tOff>=0&&tOff<nDays){
    bg+=`<div class="bg todaycol" style="left:${tOff*DAY_W}px;width:${DAY_W}px"></div>`;
    bg+=`<div class="bg todayline" style="left:${tOff*DAY_W+DAY_W/2}px"></div>`;
  }

  // rows
  BARS = [];
  let rows="";
  for(const p of people){
    const items=byP[p];
    const activeLeave=items.find(l=>l.date_start<=today&&_end(l)>=today);
    const isActive=!!activeLeave;
    const next=items.filter(l=>l.date_start>today).sort((a,b)=>a.date_start<b.date_start?-1:1)[0];
    let sub="", dotColor="#48d597";
    if(activeLeave){
      const r=(activeLeave.reason||"").toLowerCase();
      sub = r==="wfh" ? "wfh today" : (r==="ooo" ? "ooo today" : "out now");
      dotColor=_color(activeLeave.reason);
    } else if(next){ sub=`next ${_human(next.date_start)}`; }
    const av=AV[_hash(p)%AV.length];
    let bars="";
    for(const l of items){
      const s=l.date_start, e=l.date_end||l.date_start;
      let so=_diff(wStart,s), eo=_diff(wStart,e);
      const oL=so<0, oR=eo>nDays-1; so=Math.max(0,so); eo=Math.min(nDays-1,eo);
      if(eo<so) continue;
      const left=so*DAY_W, width=Math.max((eo-so+1)*DAY_W-3,8);
      const days=_diff(s,e)+1;
      const dur=days>1?`<span class="d">${days}d</span>`:"";
      const txt=`<b>${_esc(l.reason||"leave")}</b>${dur}`;
      const narrow=width<54;
      const idx=BARS.length; BARS.push({l,p});
      bars+=`<div class="bar ${oL?"open-l":""} ${oR?"open-r":""}" data-idx="${idx}"
              style="left:${left}px;width:${width}px;background:${_color(l.reason)}">${narrow?"":txt}</div>`;
      if(narrow) bars+=`<div class="bar-ext" style="left:${left+width+6}px;color:${_color(l.reason)}">${txt}</div>`;
    }
    rows+=`<div class="row ${isActive?"active":""}">
        <div class="person" style="width:${PERSON_W}px">
          <span class="avatar" style="background:${av}">${_esc(_initials(p))}</span>
          <span><div class="pname">${isActive?`<span class="dot" style="color:${dotColor}">●</span>`:""}${_esc(p)}</div>
                <div class="psub">${_esc(sub)}</div></span>
        </div>
        <div class="track" style="width:${trackW}px">${bars}</div>
      </div>`;
  }

  const inner=document.getElementById("inner");
  if(!people.length){ inner.innerHTML=`<div class="empty">no dated leaves in window</div>`; }
  else {
    inner.innerHTML =
      `<div class="hdr"><div class="hdr-month">${monthBand}</div>`
      + `<div class="hdr-day"><div class="corner" style="width:${PERSON_W}px"></div>${dayNums}</div></div>`
      + `<div class="rowwrap">`
      + `<div style="position:absolute;left:${PERSON_W}px;top:0;bottom:0;width:${trackW}px">${bg}`
      + `${tOff>=0&&tOff<nDays?`<div class="todaytag" style="left:${tOff*DAY_W+DAY_W/2}px">TODAY</div>`:""}`
      + holTags + `</div>`
      + rows + `</div>`;
    // wire bar interactions
    inner.querySelectorAll(".bar").forEach(b=>{
      const {l,p}=BARS[+b.dataset.idx];
      b.addEventListener("mouseenter",e=>showTip(e,l,p));
      b.addEventListener("mousemove",moveTip);
      b.addEventListener("mouseleave",hideTip);
      if(l.url) b.addEventListener("click",()=>window.open(l.url,"_blank"));
    });
    const g=document.querySelector(".gantt");
    g.scrollLeft=Math.max(0,tOff*DAY_W-g.clientWidth/3);
  }

  const ambig=data.ambiguous||[], ae=document.getElementById("ambig");
  if(!ambig.length) ae.innerHTML=`<div class="muted">none</div>`;
  else ae.innerHTML=`<table><tr><th>person</th><th>mentioned</th><th>reason</th>
      <th>channel</th><th>excerpt</th><th>link</th></tr>`+
    ambig.map(l=>`<tr><td>${_esc(l.actor)}</td>
        <td class="muted">${_esc((l.mentioned_at||"").slice(0,10))}</td>
        <td>${_esc(l.reason||"-")}</td>
        <td class="muted">${_esc(l.channel_name?"#"+l.channel_name:"-")}</td>
        <td class="muted">${_esc((l.body_excerpt||"").slice(0,80))}</td>
        <td>${l.url?`<a href="${_esc(l.url)}" target="_blank">view</a>`:"-"}</td></tr>`).join("")
    +`</table>`;
}
load();
setInterval(load, 1_800_000);
</script>
</body></html>
"""


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a, **kw):  # silence default access log
        pass

    def _send_json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str) -> None:
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")  # never serve a stale dashboard page
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)
        if path == "/" or path == "/index.html":
            self._send_html(INDEX_HTML)
        elif path == "/channels":
            self._send_html(CHANNELS_HTML)
        elif path == "/leaves" or path == "/leaves-v2":
            self._send_html(LEAVES_V2_HTML)
        elif path == "/leaves-v1":
            self._send_html(LEAVES_HTML)
        elif path == "/api/leaves":
            self._send_json(get_leaves())
        elif path == "/api/holidays":
            year = int(q.get("year", [str(datetime.now(IST).year)])[0])
            self._send_json(_holidays.load(year))
        elif path == "/api/snapshot":
            self._send_json(get_snapshot())
        elif path == "/api/slack-channels":
            self._send_json(get_slack_channels())
        elif path == "/api/identity-timeseries":
            days = int(q.get("days", ["7"])[0])
            self._send_json(get_identity_timeseries(days))
        elif path == "/api/discover":
            self._send_json(get_discover())
        elif path == "/api/clusters":
            limit = int(q.get("limit", ["10"])[0])
            st = q.get("status", [None])[0]
            self._send_json(get_clusters(limit, st))
        elif path == "/api/log-tail":
            name = q.get("name", ["identity_reconcile.log"])[0]
            n = int(q.get("n", ["80"])[0])
            self._send_json({"name": name, "lines": get_log_tail(name, n)})
        else:
            self.send_error(404, "not found")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"  dashboard listening on http://{args.host}:{args.port}")
    print(f"  open: open http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    main()

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
import os
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
import _v3_insights as v3i

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
    snap["routines"] = rt.load_routines(state_dir=STATE)
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


def get_cadence() -> dict:
    """Today's scheduled fires per lane (ingest LaunchAgents + Claude routines).

    Feeds the v3 "Today's Cadence" rail. Each mark is a scheduled fire today;
    past fires (<= now) render solid, future fires hollow. This reflects the
    SCHEDULE + now — per-source health lives in the source cards / verdict.
    """
    now = datetime.now(IST)
    now_min = now.hour * 60 + now.minute

    def _bucket(pairs: dict[int, list]) -> list:
        # Collapse fine-grained fires (retry-every-5min, etc.) into 30-min slots
        # so the rail reads as a cadence, not noise. One mark per slot that has
        # any fire; job = distinct labels in the slot.
        slots: dict[int, list] = {}
        for m, labels in pairs.items():
            slots.setdefault((m // 30) * 30, [])
            for x in labels:
                if x not in slots[(m // 30) * 30]:
                    slots[(m // 30) * 30].append(x)
        out = []
        for sm in sorted(slots):
            hh, mm = divmod(sm, 60)
            labels = slots[sm]
            job = (" · ".join(labels) if len(labels) <= 3
                   else f"{len(labels)} · " + ", ".join(labels[:3]) + " …")
            out.append({"t": f"{hh:02d}:{mm:02d}", "job": job,
                        "st": "ok" if sm <= now_min else "due"})
        return out

    # Ingest lane: union of every source's LaunchAgent fire-minutes.
    fires: dict[int, list] = {}
    for s in SOURCES:
        for fm in _plist_fire_minutes(AGENT_MAP[s]):
            fires.setdefault(fm, []).append(s)
    ingest = _bucket(fires)

    # Routines lane: every cron fire of an enabled routine across today.
    rs = rt.load_routines(now=now, state_dir=STATE)
    rmarks: dict[int, list] = {}
    for r in rs:
        if not r.get("enabled"):
            continue
        c = rt.parse_cron(r.get("cron", ""))
        if not c:
            continue
        for minute in range(0, 1440):
            dt = now.replace(hour=minute // 60, minute=minute % 60,
                             second=0, microsecond=0)
            if rt._cron_matches(c, dt):
                rmarks.setdefault(minute, []).append(r["id"])
    routines = _bucket(rmarks)

    return {"now_min": now_min,
            "lanes": [{"name": "ingest", "marks": ingest},
                      {"name": "routines", "marks": routines}]}


def get_insights() -> dict:
    """Aggregate DB analytics for the v3 Insights deck (see bin/_v3_insights.py)."""
    if not DB_PATH.exists():
        return {}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        out = v3i.build(conn)
        conn.close()
        return out
    except Exception as e:
        return {"error": str(e)}


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


def get_log_list() -> list[str]:
    """Every *.log the dashboard can tail — all of LOGS/ plus external logs."""
    names = {p.name for p in LOGS.glob("*.log")} if LOGS.exists() else set()
    names |= set(LOGS_EXTERNAL)
    # Surface the most-watched logs first, then the rest alphabetically.
    pref = ["ingest.log", "identity_reconcile.log", "rollup.log",
            "codegraph.log", "housekeeping.log", "session-reaper.log"]
    head = [n for n in pref if n in names]
    return head + sorted(names - set(head))


def get_log_tail(name: str, n: int = 80) -> list[str]:
    # Allow any real *.log in LOGS/ or a known external log; reject anything
    # with path separators (no traversal) and confirm it resolves inside LOGS/.
    if "/" in name or "\\" in name or not name.endswith(".log"):
        return [f"refused: {name}"]
    if name in LOGS_EXTERNAL:
        p = LOGS_EXTERNAL[name]
    else:
        p = LOGS / name
        try:
            if p.resolve().parent != LOGS.resolve():
                return [f"refused: {name}"]
        except Exception:
            return [f"refused: {name}"]
    if not p.exists():
        return ["no such log"]
    return p.read_text(errors="replace").splitlines()[-n:]


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
#discoverTable tr.tiprow { cursor:help; }
#discoverTable tr.tiprow:hover td { background:var(--panel-raised); }
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
  // Re-evaluate the time-based auto theme. The setInterval alone is unreliable:
  // Chrome throttles/pauses timers in background tabs, so a tab left open across
  // the 7pm boundary never flips. Re-apply whenever the tab regains focus /
  // becomes visible so it's always correct the moment the user looks at it.
  function recheck(){ if((localStorage.getItem(KEY)||"auto")==="auto") apply("auto"); }
  setInterval(recheck, 60000);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden) recheck(); });
  window.addEventListener("focus", recheck);
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
      <option>ingest.log</option>
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
    // WFH = working remotely, NOT out — count it separately from actual leave.
    const _isWfh = l => (l.reason || "").toLowerCase() === "wfh";
    const _names = arr => arr.slice(0, 6).join(", ") + (arr.length > 6 ? ` +${arr.length - 6}` : "");
    const peopleOut = [...new Set(active.filter(l => !_isWfh(l)).map(l => l.actor))];
    const wfhPeople = [...new Set(active.filter(_isWfh).map(l => l.actor))];
    const peopleStr = peopleOut.length ? _names(peopleOut) : "nobody";
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
        <span>out today</span><b>${peopleOut.length} ${peopleOut.length === 1 ? "person" : "people"} <span class="muted">${peopleStr}</span></b>
        ${wfhPeople.length ? `<span>wfh today</span><b>${wfhPeople.length} <span class="muted">${_names(wfhPeople)}</span></b>` : ""}
        <span>upcoming</span><b>${upcoming.length} <span class="muted">(next ${60}d)</span></b>
        <span>date TBD</span><b>${ambig.length} <span class="muted">ambiguous mention(s)</span></b>
        <span>tracked</span><b>${(leaves.total || 0).toLocaleString()} total rows</b>
        ${holRow}
      </div>
      ${leaves.error ? `<div class="finding fail"><b>FAIL</b> · ${_esc(leaves.error)}</div>` : ""}
      <div class="leaveslink" style="margin-top:8px"><a href="/leaves" target="_blank" style="color:var(--blue);font-size:12px;font-weight:600">view leaves gantt →</a></div>`));
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

// Populate the log dropdown from whatever *.log files actually exist, so new
// logs (codegraph, leaves, slack-discover, relay-bot, …) show up automatically.
async function loadLogList() {
  const sel = document.getElementById("logname");
  try {
    const j = await (await fetch("/api/logs")).json();
    const cur = sel.value;
    sel.innerHTML = (j.logs || []).map(n =>
      `<option${n === cur ? " selected" : ""}>${_esc(n)}</option>`).join("");
  } catch (e) {}
  loadLog();
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

function showTextTip(e, txt) {
  const t = document.getElementById("tooltip");
  t.innerHTML = `<b>promotion criteria</b><div class="meta">${_esc(txt || "no rationale recorded")}</div>`;
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
      const trs = rows.map(c => {
        const tip = c.rationale || "no rationale recorded";
        return `<tr class="tiprow" data-tip="${_esc(tip)}" title="${_esc(tip)}">
             <td>${_esc(c.name)}</td><td>${_esc(c.kind)}</td>
             <td>${c.team_members||0}</td><td>${c.team_msgs||0}</td>
             <td>${c.total_msgs||0}</td><td class="muted">${_esc(c.mode||"")}</td></tr>`;
      }).join("");
      html += `<div class="finding ${lvl}" style="margin-top:8px"><b>${title}</b> (${rows.length})</div>
        <table><tr><th>name</th><th>kind</th><th>team</th><th>t-msg</th><th>all-msg</th><th>mode</th></tr>${trs}</table>`;
    }
    el.innerHTML = html || `<span class="muted">no proposals</span>`;
    // Wire the dashboard's instant popup tooltip onto each row — native title=
    // is slow to appear and easy to miss. Shows the promotion criteria/rationale.
    el.querySelectorAll("tr.tiprow").forEach(tr => {
      tr.addEventListener("mouseenter", e => showTextTip(e, tr.dataset.tip));
      tr.addEventListener("mousemove", moveTooltip);
      tr.addEventListener("mouseleave", hideTooltip);
    });
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
loadLogList();
loadClusters();
setInterval(refreshAll, 1_800_000);
setInterval(loadLog, 1_800_000);
setInterval(loadClusters, 1_800_000);
</script>
</body></html>
"""


# ── v2: modern re-skin of the main page (served at /v2) ───────────────────────
# Built by transforming INDEX_HTML so the render JS stays BYTE-IDENTICAL — every
# field, finding, tooltip, the identity chart, cluster pack/list, discover
# promotion-tooltips and user-group apply commands are preserved exactly. Only
# the CSS, the app-bar header, and an added KPI summary strip differ. To adopt
# v2 as the default, point the "/" route at INDEX_V2_HTML.
_V2_CSS = """
:root{
  color-scheme: dark;
  --bg:#0a0d14; --bg-deep:#070a10; --panel:#141a26; --panel-raised:#1a2130;
  --hover:#1b2230; --line:#222b3b; --line2:#2c3850; --line-faint:#19202c;
  --text:#e7ecf4; --text2:#9aa6bd; --text-strong:#ffffff;
  --muted:#6b7891; --muted2:#7d8aa3; --ink:#0a0d14;
  --overlay:#ffffff0d; --overlay-faint:#ffffff09;
  --green:#34d399; --yellow:#fbbf24; --red:#fb7185; --blue:#7c8cff;
  --purple:#9a7cff; --amber:#fbbf24; --accent:#7c8cff; --accent2:#9a7cff;
  --pill-ok-bg:#0f2e26; --pill-warn-bg:#392c0a; --pill-fail-bg:#3a1620;
  --radius:16px; --radius-sm:10px;
  --shadow:0 1px 2px #00000055, 0 10px 26px -14px #00000099;
  --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
html[data-theme="light"]{
  color-scheme: light;
  --bg:#f4f6fa; --bg-deep:#e9edf3; --panel:#ffffff; --panel-raised:#ffffff;
  --hover:#eef1f7; --line:#e4e8f0; --line2:#d3dae6; --line-faint:#eef1f6;
  --text:#1c2533; --text2:#566077; --text-strong:#0b1220;
  --muted:#8a93a6; --muted2:#7a8497; --ink:#0b0f14;
  --overlay:#0000000f; --overlay-faint:#00000008;
  --green:#10b981; --yellow:#d97706; --red:#e11d48; --blue:#5566f0;
  --purple:#7c5cf0; --amber:#d97706; --accent:#5566f0; --accent2:#7c5cf0;
  --pill-ok-bg:#e7f7f0; --pill-warn-bg:#fdf3e2; --pill-fail-bg:#fdeaee;
  --shadow:0 1px 2px #0000000a, 0 10px 30px -16px #0000002e;
}
*{box-sizing:border-box}
html{transition:background-color .15s,color .15s;}
body{font:14px/1.5 var(--sans);
  background:radial-gradient(1100px 540px at 82% -8%, color-mix(in srgb,var(--accent) 9%, transparent), transparent 60%), var(--bg);
  color:var(--text); max-width:1300px; margin:0 auto; padding:20px 22px 72px; -webkit-font-smoothing:antialiased;}

#themeToggle{position:fixed; top:16px; right:20px; z-index:1000; display:flex;
  background:var(--panel); border:1px solid var(--line); border-radius:999px; padding:3px;
  font:12px var(--sans); box-shadow:var(--shadow);}
#themeToggle button{background:transparent; color:var(--muted); border:0; font:inherit; font-weight:600;
  padding:5px 12px; border-radius:999px; cursor:pointer; transition:.15s;}
#themeToggle button:hover{color:var(--text);}
#themeToggle button.on{background:linear-gradient(135deg,var(--accent),var(--accent2)); color:#fff;}

.appbar{display:flex; align-items:center; gap:13px; margin-bottom:22px;}
.brand{display:flex; align-items:center; gap:12px;}
.logo{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;color:#fff;font-size:15px;
  background:linear-gradient(135deg,var(--accent),var(--accent2)); box-shadow:0 6px 18px -6px var(--accent);}
h1{font:700 18px/1.1 var(--sans); margin:0; letter-spacing:-.01em;}
.subtitle{color:var(--muted); font-size:11.5px; margin-top:3px; font-family:var(--mono);}
.subtitle .refresh-btn{color:var(--accent); cursor:pointer; text-decoration:none;}
.live{margin-left:auto; margin-right:128px; display:flex; align-items:center; gap:8px; font-size:12px; color:var(--text2);
  background:var(--panel); border:1px solid var(--line); padding:6px 12px; border-radius:999px;}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 2.4s infinite;}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--green) 55%,transparent)}70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}

.kpis{display:grid; grid-template-columns:repeat(5,1fr); gap:13px; margin-bottom:6px;}
.kpi{background:linear-gradient(180deg,var(--panel-raised),var(--panel)); border:1px solid var(--line);
  border-radius:var(--radius); padding:14px 15px; box-shadow:var(--shadow);}
.kpi .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600;}
.kpi .val{font:750 24px/1.1 var(--mono); letter-spacing:-.02em; margin-top:7px; color:var(--text-strong);}
.kpi .hint{font-size:11.5px;color:var(--text2);margin-top:4px; min-height:14px;}

.sec{display:flex; align-items:center; gap:10px; margin:26px 0 14px;}
.sec h2{font:700 13px/1 var(--sans); text-transform:uppercase; letter-spacing:.08em; color:var(--text2); margin:0;}
.sec .rule{flex:1; height:1px; background:linear-gradient(90deg,var(--line),transparent);}

.grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:16px;}
.lane{position:relative; background:var(--panel); border:1px solid var(--line);
  border-radius:var(--radius); padding:15px 16px 14px; box-shadow:var(--shadow);
  transition:transform .16s,border-color .16s,box-shadow .16s; overflow:hidden;}
.lane:hover{transform:translateY(-3px); border-color:var(--line2); box-shadow:0 1px 2px #00000055,0 18px 40px -20px #000000b0;}
.lane::before{content:"";position:absolute;inset:0 0 auto 0;height:3px;background:var(--blue);}
.lane[data-state="ok"]::before{background:linear-gradient(90deg,var(--green),transparent);}
.lane[data-state="warn"]::before{background:linear-gradient(90deg,var(--yellow),transparent);}
.lane[data-state="fail"]::before{background:linear-gradient(90deg,var(--red),transparent);}
.lane h2{font:700 14.5px/1 var(--sans); margin:2px 0 12px; display:flex; align-items:center;
  justify-content:space-between; gap:10px; letter-spacing:.02em;}

.pill{display:inline-flex; align-items:center; gap:6px; font:700 10.5px/1 var(--sans); text-transform:uppercase;
  letter-spacing:.05em; padding:4px 9px; border-radius:999px; background:var(--pill-ok-bg); color:var(--green); white-space:nowrap;}
.pill::before{content:""; width:7px; height:7px; border-radius:50%; background:currentColor; flex:0 0 auto;}
.pill.warn{background:var(--pill-warn-bg); color:var(--yellow);}
.pill.fail{background:var(--pill-fail-bg); color:var(--red);}
.pill[title]{cursor:help;}

.kv{display:grid; grid-template-columns:auto 1fr; gap:7px 14px; align-items:baseline; font-size:12.5px;}
.kv>span{color:var(--muted); font-size:12px;}
.kv b{color:var(--text); font-weight:600; font-family:var(--mono); font-size:12px; text-align:right; word-break:break-word;}
.kv b .pill{font-family:var(--sans);}
.kv .muted{color:var(--muted);}

.finding{font-size:11.5px; line-height:1.45; padding:9px 11px; margin:10px 0 0; border-radius:var(--radius-sm);
  background:var(--hover); border:1px solid var(--line); cursor:help;}
.finding b{color:var(--text-strong); font-weight:700;}
.finding.warn{background:var(--pill-warn-bg); border-color:color-mix(in srgb,var(--yellow) 24%,transparent);}
.finding.fail{background:var(--pill-fail-bg); border-color:color-mix(in srgb,var(--red) 26%,transparent);}
.finding.muted{background:var(--hover); border-color:var(--line); opacity:.75;}

details{margin-top:12px;}
summary{cursor:pointer; color:var(--accent); font-size:11.5px; font-weight:600; padding:5px 0; list-style:none;}
summary::-webkit-details-marker{display:none;}
summary::before{content:"\\25B8  "; color:var(--muted);}
details[open] summary::before{content:"\\25BE  ";}
table{border-collapse:collapse; width:100%; margin-top:8px; font-size:12px;}
th{color:var(--muted); font-weight:600; text-align:left; padding:6px 8px; border-bottom:1px solid var(--line);
  text-transform:uppercase; font-size:10.5px; letter-spacing:.04em; cursor:pointer; user-select:none;}
th:hover{color:var(--text);}
td{padding:6px 8px; border-bottom:1px solid var(--line-faint); font-family:var(--mono); font-size:11.5px;}
tr:last-child td{border-bottom:0;}
table tr:hover td{background:var(--hover);}
.muted{color:var(--muted);}

/* ROUTINES is the only lane whose table is a DIRECT child (others nest theirs
   inside <details>). It's wide, so let it span the full grid row instead of
   being squeezed into one auto-fill column, and stop its cells wrapping. */
#lanes .lane:has(> table){grid-column:1 / -1;}
#lanes .lane:has(> table) th,#lanes .lane:has(> table) td{white-space:nowrap;}
#lanes .lane:has(> table) .kv{max-width:520px;}

.chart-wrap{background:var(--panel); border:1px solid var(--line); padding:16px; border-radius:var(--radius);
  margin-top:24px; box-shadow:var(--shadow);}
.chart-wrap h2{font:700 13px/1 var(--sans); text-transform:uppercase; letter-spacing:.08em; color:var(--text2); margin:0 0 12px;}
.tail{background:var(--bg-deep); padding:12px; border-radius:var(--radius-sm); max-height:300px; overflow:auto;
  white-space:pre; font:11px var(--mono); color:var(--muted2); border:1px solid var(--line-faint);}

.cluster{background:var(--panel-raised); border:1px solid var(--line); border-left:3px solid var(--blue);
  padding:12px 14px; margin:10px 0; border-radius:var(--radius-sm);}
.cluster h3{font:600 13.5px/1.2 var(--sans); margin:0 0 5px; color:var(--text);}
.cluster .meta{color:var(--muted); font-size:11px; margin-bottom:5px;}
.cluster .summary{color:var(--text2); font-size:12.5px; margin:7px 0;}
.cluster .chips span{display:inline-block; padding:2px 8px; margin:2px; font-size:10.5px;
  background:var(--hover); border:1px solid var(--line); border-radius:999px; color:var(--text2);}
.cluster .json-block{background:var(--bg-deep); padding:8px; margin:5px 0; border-radius:var(--radius-sm);
  font:11px var(--mono); color:var(--muted2); white-space:pre-wrap; word-break:break-word;}
.cluster[data-status="ACTIVE"]{border-left-color:var(--green);}
.cluster[data-status="RECURRING"]{border-left-color:var(--blue);}
.cluster[data-status="STALE"]{border-left-color:var(--yellow);}
.cluster[data-status="RESOLVED"]{border-left-color:var(--muted);}
#clusterPack{width:100%; height:560px; background:var(--bg-deep); border-radius:var(--radius-sm); border:1px solid var(--line-faint);}
#clusterPack circle{stroke:var(--bg); stroke-width:1.2; cursor:pointer;}
#clusterPack circle:hover{stroke:var(--text-strong); stroke-width:2;}
#clusterPack text{fill:var(--text); pointer-events:none; font-size:11px; text-anchor:middle; font-family:var(--sans);}

.tooltip{position:absolute; background:var(--panel-raised); color:var(--text); padding:9px 11px;
  border-radius:var(--radius-sm); border:1px solid var(--line); font-size:11.5px; max-width:360px;
  pointer-events:none; opacity:0; transition:opacity .12s; z-index:999; box-shadow:0 12px 32px -8px #000000b0;}
.tooltip.show{opacity:1;}
.tooltip b{color:var(--text-strong);}
.tooltip .meta{color:var(--text2); font-size:10.5px; margin-top:4px;}
#discoverTable tr.tiprow{cursor:help;}
#discoverTable tr.tiprow:hover td{background:var(--panel-raised);}

.view-toggle{display:inline-flex; gap:0; background:var(--panel); border:1px solid var(--line); border-radius:999px; padding:3px;}
.view-toggle button{background:transparent; color:var(--muted); border:0; padding:5px 13px; font:600 11.5px var(--sans);
  cursor:pointer; border-radius:999px; transition:.15s;}
.view-toggle button:hover{color:var(--text);}
.view-toggle button.active{background:linear-gradient(135deg,var(--accent),var(--accent2)); color:#fff;}
.legend{display:flex; gap:14px; flex-wrap:wrap; font-size:11px; color:var(--text2); margin:10px 0;}
.legend span::before{content:"\\25CF"; margin-right:5px;}
.legend .active::before{color:var(--green);}
.legend .recurring::before{color:var(--blue);}
.legend .stale::before{color:var(--yellow);}
.legend .resolved::before{color:var(--muted);}
.row{display:flex; gap:14px; align-items:center;}
.row label{color:var(--muted); font-size:11px;}
.row select{background:var(--panel); color:var(--text); border:1px solid var(--line);
  padding:6px 10px; font:inherit; font-size:12px; border-radius:8px;}
.refresh-btn{cursor:pointer;}
@media(max-width:1000px){.kpis{grid-template-columns:repeat(2,1fr)} .grid{grid-template-columns:1fr} .live{display:none}}
"""

_V2_OLD_HEADER = """<h1>INGEST STATUS DASHBOARD</h1>
<div class="subtitle">
  <span id="when">loading…</span>
  &nbsp;·&nbsp; <span id="auto">auto-refresh 30min</span>
  &nbsp;·&nbsp; <a class="refresh-btn" onclick="refresh()">refresh now</a>
</div>

<div class="grid" id="lanes"></div>"""

_V2_NEW_HEADER = """<header class="appbar">
  <div class="brand">
    <div class="logo">◆</div>
    <div>
      <h1>Ingest Status</h1>
      <div class="subtitle"><span id="when">loading…</span> &nbsp;·&nbsp; <span id="auto">auto-refresh 30min</span> &nbsp;·&nbsp; <a class="refresh-btn" onclick="refresh()">refresh now</a></div>
    </div>
  </div>
  <div class="live"><span class="dot"></span> live</div>
</header>

<div class="kpis" id="kpis"></div>
<div class="sec"><h2>Sources</h2><span class="rule"></span></div>
<div class="grid" id="lanes"></div>"""

# Added KPI summary strip — derived from the same snapshot the lanes use; the
# healthy/warn/down counts are read off the just-rendered lane DOM so they can
# never diverge from the per-source verdicts.
_V2_KPI_JS = """function _kfmt(n){ n=+n||0; if(n>=1e6) return (n/1e6).toFixed(1)+"M"; if(n>=1e3) return Math.round(n/1e3)+"K"; return ""+n; }
function _relFut(d){ const s=(d.getTime()-Date.now())/1000; if(s<60) return "now"; if(s<3600) return "~"+Math.floor(s/60)+"m"; if(s<86400) return "~"+Math.floor(s/3600)+"h"; return "~"+Math.floor(s/86400)+"d"; }
function renderKpis(s, slack, leaves){
  const el=document.getElementById("kpis"); if(!el) return;
  const c=sel=>document.querySelectorAll("#lanes .lane"+sel).length;
  const ok=c('[data-state="ok"]'), warn=c('[data-state="warn"]'), fail=c('[data-state="fail"]'); const tot=ok+warn+fail||1;
  const bySrc=(s.db&&s.db.by_source)||{};
  const ev=Object.values(bySrc).reduce((a,b)=>a+(+b||0),0);
  const evTop=Object.entries(bySrc).sort((a,b)=>b[1]-a[1]).slice(0,3).map(kv=>kv[0]+" "+_kfmt(kv[1])).join(" · ");
  const disc=s.discover||{}; const ready=(disc.n_full||0)+(disc.n_team||0)+(disc.n_owner||0), rev=disc.n_review||0;
  const today=leaves.today; const out=new Set((leaves.leaves||[]).filter(l=>l.date_start<=today&&(l.date_end||l.date_start)>=today&&(l.reason||"").toLowerCase()!=="wfh").map(l=>l.actor)).size;
  const nh=leaves.next_holiday; const hol=nh?("next holiday "+nh.date):"no upcoming holiday";
  let soon=null, soonId=""; (s.routines||[]).forEach(r=>{ if(r.enabled&&r.next_fire_iso){ const t=new Date(r.next_fire_iso); if(!soon||t<soon){soon=t;soonId=r.id;} }});
  const tiles=[
    ["Sources healthy", ok+" / "+tot, warn+" warn · "+fail+" down"],
    ["Events ingested", _kfmt(ev), evTop],
    ["Discover queue", (ready+rev), ready+" ready · "+rev+" review"],
    ["Out today", out, hol],
    ["Next routine", soon?_relFut(soon):"—", soonId||"—"],
  ];
  el.innerHTML=tiles.map(t=>'<div class="kpi"><div class="lbl">'+_esc(t[0])+'</div><div class="val">'+_esc(""+t[1])+'</div><div class="hint">'+_esc(t[2]||"")+'</div></div>').join("");
}

async function refresh() {"""


# ── STACKED TRANSFORMS — read before editing INDEX_HTML ───────────────────────
# v2..v5 are NOT separate templates: each is a chain of `.replace()` calls over the
# version below it (v2/v3 ← INDEX_HTML, v4 ← v3, v5 ← v4). The anchors are literal
# fragments of v1's header / laneFor / lanes-grid / insights / bootstrap strings.
# Editing any of those strings in INDEX_HTML SILENTLY breaks the downstream anchors —
# the `.replace()` no-ops and the builder falls back to the un-transformed markup
# (tabs / health grid / verdict just vanish, no error). After any INDEX_HTML edit,
# rebuild and confirm the markers exist: vtabs / vStatus / vInsights / healthgrid /
# renderHealth in INDEX_V5_HTML (and renderVerdict/renderCadence in v3).
def _build_index_v2() -> str:
    """Modern re-skin of INDEX_HTML with identical render JS (see note above)."""
    import re as _re
    html = INDEX_HTML
    html = html.replace("<title>cron-status · dashboard</title>",
                        "<title>Ingest Status · dashboard</title>")
    html = _re.sub(r"<style>.*?</style>",
                   lambda _m: "<style>" + _V2_CSS + "</style>",
                   html, count=1, flags=_re.S)
    html = html.replace(_V2_OLD_HEADER, _V2_NEW_HEADER)
    html = html.replace('document.getElementById("lanes").innerHTML = lanes.join("");',
                        'document.getElementById("lanes").innerHTML = lanes.join("");\n  renderKpis(s, slack, leaves);')
    html = html.replace("async function refresh() {", _V2_KPI_JS)
    return html


INDEX_V2_HTML = _build_index_v2()


# ── v3: "telemetry / observatory" console (served at /v3) ─────────────────────
# Like _build_index_v2, this transforms INDEX_HTML so ALL the per-source data
# logic stays byte-identical (findings, discover/usergroup tables, channel
# detail, run-health, cluster pack, signals chart, log tail). v3 changes the
# PRESENTATION: a new shell (appbar + system verdict + the signature "Today's
# Cadence" rail + KPI strip), card-chrome lanes (icon/subtitle/spine), a
# last-success column on routines, and a live Insights deck (/api/insights).
_V3_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700'
    '&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">'
)

_V3_CSS = r"""
:root{
  color-scheme: dark;
  --bg:#080d11; --bg2:#0b1217; --well:#06090c; --bg-deep:#06090c;
  --panel:#0f171d; --panel2:#132027; --panel-raised:#16242c;
  --line:#1e2c34; --line2:#284049; --line-faint:#16222a;
  --text:#dde6e9; --text2:#8ea0a8; --text3:#6a7e86; --muted:#566a72; --muted2:#6a7e86; --ink:#05080a;
  --beacon:#2dd4cf; --beacon-dim:#2dd4cf24; --beacon-faint:#2dd4cf12;
  --green:#47d182; --yellow:#edb23c; --red:#f15873; --blue:#5eb1ff; --purple:#9b8eff; --amber:#edb23c; --accent:#2dd4cf;
  --ok:#47d182; --warn:#edb23c; --bad:#f15873;
  --ok-bg:#0e2a1d; --warn-bg:#2e2410; --bad-bg:#2e151c;
  --pill-ok-bg:#0e2a1d; --pill-warn-bg:#2e2410; --pill-fail-bg:#2e151c;
  --ok-line:#47d18238; --warn-line:#edb23c38; --bad-line:#f1587340;
  --overlay:#ffffff0d; --overlay-faint:#ffffff09;
  --radius:14px; --radius-sm:9px;
  --shadow:0 1px 1px #00000055, 0 14px 34px -22px #000000cc;
  --grot:"Space Grotesk",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
html[data-theme="light"]{
  color-scheme: light;
  --bg:#eef2f3; --bg2:#e6ecee; --well:#dfe6e8; --bg-deep:#dfe6e8;
  --panel:#ffffff; --panel2:#f7fafb; --panel-raised:#ffffff;
  --line:#dde5e8; --line2:#c6d3d7; --line-faint:#e8eef0;
  --text:#13242a; --text2:#47585f; --text3:#6b7c83; --muted:#869399; --muted2:#6b7c83; --ink:#ffffff;
  --beacon:#0c968f; --beacon-dim:#0c968f1f; --beacon-faint:#0c968f10;
  --green:#0f9d57; --yellow:#b87708; --red:#d13a55; --blue:#2f6fe0; --purple:#6f5fe0; --amber:#b87708; --accent:#0c968f;
  --ok:#0f9d57; --warn:#b87708; --bad:#d13a55;
  --ok-bg:#dcf3e7; --warn-bg:#f6ecd3; --bad-bg:#f8dee4;
  --pill-ok-bg:#dcf3e7; --pill-warn-bg:#f6ecd3; --pill-fail-bg:#f8dee4;
  --ok-line:#0f9d5733; --warn-line:#b8770833; --bad-line:#d13a5533;
  --overlay:#0000000f; --overlay-faint:#00000008;
  --shadow:0 1px 1px #0000000a, 0 12px 30px -20px #00000026;
}
*{box-sizing:border-box}
html{transition:background-color .18s,color .18s}
body{font:14px/1.5 var(--sans); background:
    radial-gradient(900px 460px at 88% -12%, var(--beacon-faint), transparent 62%), var(--bg);
  color:var(--text); max-width:1280px; margin:0 auto; padding:0 22px 70px; -webkit-font-smoothing:antialiased;}
a{color:var(--beacon);text-decoration:none} a:hover{text-decoration:underline}
::selection{background:var(--beacon-dim)}

#themeToggle{position:fixed;top:14px;right:18px;z-index:1000;display:flex;
  background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:3px;
  font:600 11px var(--grot);box-shadow:var(--shadow);overflow:hidden;}
#themeToggle button{background:transparent;color:var(--muted);border:0;padding:5px 11px;border-radius:999px;cursor:pointer;font:inherit;}
#themeToggle button:hover{color:var(--text);}
#themeToggle button.on{background:var(--beacon);color:var(--ink);}

.appbar{display:flex;align-items:center;gap:12px;padding:16px 0 4px;}
.brand{display:flex;align-items:center;gap:11px;}
.mark{width:30px;height:30px;border-radius:8px;position:relative;flex:0 0 auto;
  background:radial-gradient(circle at 32% 30%, var(--beacon), #0b6f7a 96%);box-shadow:0 0 0 1px #2dd4cf30,0 6px 16px -6px var(--beacon);}
.mark::after{content:"";position:absolute;inset:0;border-radius:8px;background:repeating-linear-gradient(0deg,#ffffff14 0 1px,transparent 1px 4px);}
.appbar h1{font:600 16px/1 var(--grot);margin:0;letter-spacing:.2px;}
.subtitle{color:var(--muted);font:400 11.5px/1.3 var(--mono);margin-top:4px;}
.subtitle .refresh-btn{color:var(--beacon);cursor:pointer;}

.verdict{display:flex;align-items:center;gap:16px;margin:14px 0 4px;padding:15px 18px;
  border:1px solid var(--line);border-radius:var(--radius);background:linear-gradient(100deg,var(--panel2),var(--panel));
  box-shadow:var(--shadow);position:relative;overflow:hidden;}
.verdict::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--vc,var(--ok));}
.verdict[data-s="ok"]{--vc:var(--ok)} .verdict[data-s="warn"]{--vc:var(--warn)} .verdict[data-s="bad"]{--vc:var(--bad)}
.beat{width:40px;height:40px;flex:0 0 auto;border-radius:50%;display:grid;place-items:center;background:color-mix(in srgb,var(--vc) 16%,transparent);}
.beat span{width:12px;height:12px;border-radius:50%;background:var(--vc);box-shadow:0 0 0 0 var(--vc);animation:beat 2.6s infinite;}
@keyframes beat{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--vc) 55%,transparent)}70%{box-shadow:0 0 0 12px transparent}100%{box-shadow:0 0 0 0 transparent}}
.vmain{font:600 18px/1.15 var(--grot);color:var(--text);}
.vsub{font:400 12px/1.4 var(--mono);color:var(--text2);margin-top:4px;}
.vsub b{color:var(--text);font-weight:500;}
.vmeta{margin-left:auto;display:flex;gap:22px;text-align:right;}
.vmeta .m .n{font:700 20px/1 var(--mono);color:var(--text);}
.vmeta .m .l{font:600 10px/1 var(--grot);text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-top:6px;}
.vmeta .m.ok .n{color:var(--ok)} .vmeta .m.warn .n{color:var(--warn)} .vmeta .m.bad .n{color:var(--bad)}

.cadence{margin:14px 0 8px;border:1px solid var(--line);border-radius:var(--radius);background:var(--bg2);box-shadow:var(--shadow);overflow:hidden;}
.cadence .ch{display:flex;align-items:center;gap:10px;padding:12px 18px 10px;}
.cadence .ch h2{font:600 11px/1 var(--grot);text-transform:uppercase;letter-spacing:.12em;color:var(--text2);margin:0;}
.cadence .ch .leg{margin-left:auto;display:flex;gap:14px;font:500 10.5px var(--mono);color:var(--muted);align-items:center;}
.cadence .ch .leg i{display:inline-flex;align-items:center;gap:5px;}
.cadence .ch .leg .s{width:9px;height:9px;border-radius:50%;background:var(--ok);}
.cadence .ch .leg .h{width:9px;height:9px;border-radius:50%;border:1.5px solid var(--muted);}
.cadence .ch .leg .nd{width:2px;height:11px;background:var(--beacon);box-shadow:0 0 6px var(--beacon);}
.rail{position:relative;padding:0 18px 14px;}
.railscale{position:relative;height:16px;margin-left:96px;border-bottom:1px solid var(--line-faint);}
.railscale .t{position:absolute;transform:translateX(-50%);font:500 9.5px var(--mono);color:var(--muted);bottom:2px;}
.railscale .gl{position:absolute;top:0;bottom:0;width:1px;background:var(--line-faint);}
#cadLanes{position:relative;}
.cadlane{position:relative;height:34px;display:flex;align-items:center;}
.cadlane .lname{width:96px;flex:0 0 auto;font:600 10.5px var(--grot);text-transform:uppercase;letter-spacing:.06em;color:var(--text3);padding-right:8px;text-align:right;}
.cadlane .ltrack{position:relative;flex:1;height:100%;}
.cadlane .ltrack .gl{position:absolute;top:0;bottom:0;width:1px;background:var(--line-faint);opacity:.6;}
.tick{position:absolute;top:50%;transform:translate(-50%,-50%);width:11px;height:11px;border-radius:50%;cursor:pointer;transition:transform .1s;}
.tick:hover{transform:translate(-50%,-50%) scale(1.45);z-index:4;}
.tick.ok{background:var(--ok);box-shadow:0 0 0 3px var(--ok-bg);}
.tick.fail{background:var(--bad);box-shadow:0 0 0 3px var(--bad-bg);}
.tick.due{background:transparent;border:1.5px solid var(--text3);}
.tick.due.soon{border-color:var(--beacon);box-shadow:0 0 7px -1px var(--beacon);}
.nowfull{position:absolute;top:8px;bottom:8px;width:2px;background:var(--beacon);z-index:3;box-shadow:0 0 9px var(--beacon);}
.nowfull::before{content:"NOW";position:absolute;top:-15px;left:50%;transform:translateX(-50%);font:700 8.5px var(--grot);letter-spacing:.1em;color:var(--ink);background:var(--beacon);padding:2px 5px;border-radius:4px;white-space:nowrap;}
.nowfull .pulse{position:absolute;top:-3px;left:50%;transform:translateX(-50%);width:6px;height:6px;border-radius:50%;background:var(--beacon);box-shadow:0 0 8px var(--beacon);animation:beat 2.6s infinite;}

.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:13px;margin:14px 0 6px;}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:13px 15px;box-shadow:var(--shadow);position:relative;overflow:hidden;}
.kpi .l{font:600 10.5px/1 var(--grot);text-transform:uppercase;letter-spacing:.07em;color:var(--muted);}
.kpi .v{font:700 24px/1.05 var(--mono);margin-top:9px;color:var(--text);}
.kpi .h{font:500 11px/1.3 var(--mono);color:var(--text2);margin-top:5px;min-height:14px;}
.kpi.flag .v{color:var(--warn);}
.kpi .edge{position:absolute;left:0;right:0;bottom:0;height:2px;background:linear-gradient(90deg,var(--beacon),transparent);}

.sec{display:flex;align-items:center;gap:12px;margin:28px 0 14px;}
.sec h2{font:600 12px/1 var(--grot);text-transform:uppercase;letter-spacing:.12em;color:var(--text2);margin:0;}
.sec .dot{width:5px;height:5px;border-radius:50%;background:var(--beacon);}
.sec .count{font:500 11px var(--mono);color:var(--muted);}
.sec .rule{flex:1;height:1px;background:linear-gradient(90deg,var(--line),transparent);}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:15px;}
.lane{position:relative;background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow);overflow:hidden;transition:transform .15s,border-color .15s,box-shadow .15s;padding:0 0 4px;}
.lane:hover{transform:translateY(-2px);border-color:var(--line2);}
.lane[data-state="ok"]{--sc:var(--ok)} .lane[data-state="warn"]{--sc:var(--warn)} .lane[data-state="fail"]{--sc:var(--bad)}
.lane .spine{position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--sc,var(--ok));}
.chd{display:flex;align-items:center;gap:11px;padding:14px 16px 8px 18px;}
.chd .ic{width:28px;height:28px;flex:0 0 auto;border-radius:7px;display:grid;place-items:center;
  font:700 11px var(--grot);text-transform:uppercase;color:var(--text2);background:var(--bg2);border:1px solid var(--line);}
.chd .nm{font:600 14px/1.15 var(--grot);letter-spacing:.2px;}
.chd .nm small{display:block;font:500 10.5px var(--mono);color:var(--muted);letter-spacing:0;margin-top:2px;}
.grid .lane:has(> table){grid-column:1 / -1;}
.grid .lane:has(> table) th,.grid .lane:has(> table) td{white-space:nowrap;}
/* full-width lane (ROUTINES): keep the meta kv compact on the left and let the
   table use the whole width. */
.grid .lane:has(> table) .kv{max-width:560px;}
.grid .lane:has(> table) > table{width:100%;}
/* plain card links that aren't inside .kv/details (e.g. the LEAVES "view gantt"
   link) need the same left indent as the card content. */
.lane .leaveslink{padding:0 16px 8px 18px;}

.pill{margin-left:auto;display:inline-flex;align-items:center;gap:6px;font:700 9.5px var(--grot);text-transform:uppercase;
  letter-spacing:.06em;padding:5px 9px;border-radius:999px;background:var(--pill-ok-bg);color:var(--ok);white-space:nowrap;}
.pill .pd{width:6px;height:6px;border-radius:50%;background:currentColor;}
.pill.warn{background:var(--pill-warn-bg);color:var(--warn);}
.pill.fail{background:var(--pill-fail-bg);color:var(--bad);}
.pill[title]{cursor:help;}

.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;align-items:baseline;padding:4px 16px 10px 18px;font-size:12.5px;}
.kv>span{font:500 11.5px var(--grot);color:var(--muted);}
.kv b{color:var(--text);font-weight:500;font-family:var(--mono);font-size:12px;text-align:right;min-width:0;overflow-wrap:anywhere;}
.kv b .pill{font-family:var(--grot);}
.kv .muted{color:var(--muted);}

details{margin:8px 16px 0 18px;}
summary{cursor:pointer;font:600 11px var(--grot);color:var(--beacon);padding:6px 0;list-style:none;user-select:none;}
summary::-webkit-details-marker{display:none;}
.muted{color:var(--muted);}

table{border-collapse:collapse;width:100%;margin-top:8px;font-size:12px;}
th{font:600 9.5px var(--grot);text-transform:uppercase;letter-spacing:.05em;color:var(--muted);text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none;}
th:hover{color:var(--text2);}
td{font:500 11.5px var(--mono);color:var(--text2);padding:6px 8px;border-bottom:1px solid var(--line-faint);}
tr:last-child td{border-bottom:0;}
tbody tr:hover td, table tr:hover td{background:var(--bg2);}
td b{color:var(--text);font-weight:600;}
/* Tables inside a card's expander (per-channel, discover, user-groups) can be
   wide AND long. Cap them to a scroll panel so columns never clip and a long
   list scrolls in place instead of ballooning the card. The full sortable
   list lives on the dedicated /channels page. */
.lane details table{display:block;max-height:320px;overflow:auto;white-space:nowrap;}
.lane details #discoverTable{display:block;max-height:340px;overflow:auto;}
.lane details #discoverTable table{max-height:none;}

.finding{margin:8px 16px 0 18px;padding:9px 11px;border-radius:var(--radius-sm);font:500 11.5px/1.45 var(--mono);border:1px solid var(--line);background:var(--bg2);cursor:help;}
.finding b{font-family:var(--grot);font-weight:700;color:var(--text);}
.finding.warn{background:var(--warn-bg);border-color:var(--warn-line);} .finding.warn b{color:var(--warn);}
.finding.fail{background:var(--bad-bg);border-color:var(--bad-line);} .finding.fail b{color:var(--bad);}
.finding.muted{opacity:.72;}

.chart-wrap{background:var(--panel);border:1px solid var(--line);padding:16px;border-radius:var(--radius);margin-top:20px;box-shadow:var(--shadow);}
.chart-wrap h2{font:600 12px/1 var(--grot);text-transform:uppercase;letter-spacing:.1em;color:var(--text2);margin:0 0 12px;}
.tail{background:var(--well);border:1px solid var(--line-faint);padding:12px;border-radius:var(--radius-sm);max-height:300px;overflow:auto;white-space:pre;font:11px/1.7 var(--mono);color:var(--text3);}

.cluster{background:var(--bg2);border:1px solid var(--line);border-left:3px solid var(--blue);padding:12px 14px;margin:10px 0;border-radius:var(--radius-sm);}
.cluster h3{font:600 13.5px var(--grot);margin:0 0 5px;color:var(--text);}
.cluster .meta{color:var(--muted);font:500 10.5px var(--mono);margin-bottom:5px;}
.cluster .summary{color:var(--text2);font-size:12.5px;margin:7px 0;}
.cluster .chips span{display:inline-block;padding:2px 8px;margin:2px;font:500 10px var(--mono);background:var(--panel);border:1px solid var(--line);border-radius:999px;color:var(--text2);}
.cluster .json-block{background:var(--well);padding:8px;margin:5px 0;border-radius:var(--radius-sm);font:11px var(--mono);color:var(--muted2);white-space:pre-wrap;word-break:break-word;}
.cluster[data-status="ACTIVE"]{border-left-color:var(--green);} .cluster[data-status="RECURRING"]{border-left-color:var(--blue);}
.cluster[data-status="STALE"]{border-left-color:var(--yellow);} .cluster[data-status="RESOLVED"]{border-left-color:var(--muted);}
#clusterPack{width:100%;height:560px;background:var(--well);border-radius:var(--radius-sm);border:1px solid var(--line-faint);}
#clusterPack circle{stroke:var(--bg);stroke-width:1.2;cursor:pointer;} #clusterPack circle:hover{stroke:var(--text);stroke-width:2;}
#clusterPack text{fill:var(--text);pointer-events:none;font-size:11px;text-anchor:middle;font-family:var(--mono);}

.tooltip{position:absolute;background:var(--panel-raised);color:var(--text);padding:9px 11px;border-radius:var(--radius-sm);border:1px solid var(--line2);font:500 11.5px var(--mono);max-width:360px;pointer-events:none;opacity:0;transition:opacity .12s;z-index:999;box-shadow:0 16px 40px -10px #000000cc;}
.tooltip.show{opacity:1;} .tooltip b{color:var(--text);font-family:var(--grot);} .tooltip .meta{color:var(--text2);font-size:10.5px;margin-top:4px;}
#discoverTable tr.tiprow{cursor:help;} #discoverTable tr.tiprow:hover td{background:var(--panel-raised);}

.view-toggle{display:inline-flex;background:var(--bg2);border:1px solid var(--line);border-radius:999px;padding:3px;}
.view-toggle button{background:transparent;color:var(--muted);border:0;padding:5px 13px;font:600 11px var(--grot);cursor:pointer;border-radius:999px;}
.view-toggle button.active{background:var(--beacon);color:var(--ink);}
.legend{display:flex;gap:14px;flex-wrap:wrap;font:500 11px var(--mono);color:var(--text2);margin:10px 0;}
.legend span::before{content:"\25CF";margin-right:5px;}
.legend .active::before{color:var(--green);} .legend .recurring::before{color:var(--blue);}
.legend .stale::before{color:var(--yellow);} .legend .resolved::before{color:var(--muted);}
.row{display:flex;gap:14px;align-items:center;} .row label{color:var(--muted);font:500 11px var(--grot);}
.row select,.row button{background:var(--panel);color:var(--text);border:1px solid var(--line);padding:6px 10px;font:500 11.5px var(--mono);border-radius:8px;cursor:pointer;}
.refresh-btn{cursor:pointer;}

.totstrip{display:flex;gap:24px;flex-wrap:wrap;font:500 12px var(--mono);color:var(--text2);margin:-2px 0 16px;}
.totstrip b{color:var(--text);font-weight:700;font-size:14px;}
.totstrip .lbl{color:var(--muted);font:600 10px var(--grot);text-transform:uppercase;letter-spacing:.06em;display:block;margin-top:2px;}
.insights{display:grid;grid-template-columns:1fr 1fr;gap:15px;}
.insights .span2{grid-column:1 / -1;} .insights .panel{margin:0;}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:15px 17px;}
.panel .ph{display:flex;align-items:center;gap:10px;margin-bottom:12px;}
.panel .ph h3{font:600 11px var(--grot);text-transform:uppercase;letter-spacing:.1em;color:var(--text2);margin:0;}
.panel .ph .sub{font:500 10.5px var(--mono);color:var(--muted);margin-left:auto;}
.chartbox{background:var(--well);border:1px solid var(--line-faint);border-radius:var(--radius-sm);padding:8px;}
.ig-legend{display:flex;gap:16px;flex-wrap:wrap;font:500 10.5px var(--mono);color:var(--text2);margin-top:11px;}
.ig-legend i{display:inline-flex;align-items:center;gap:6px;} .ig-legend i::before{content:"";width:10px;height:10px;border-radius:3px;background:var(--c,var(--beacon));}
svg text{font-family:var(--mono);}
@media(max-width:1080px){.kpis{grid-template-columns:repeat(2,1fr)} .insights{grid-template-columns:1fr} .vmeta{display:none}}
@media(prefers-reduced-motion:reduce){*{animation:none!important}}
:focus-visible{outline:2px solid var(--beacon);outline-offset:2px;border-radius:4px;}
"""

_V3_OLD_HEADER = """<h1>INGEST STATUS DASHBOARD</h1>
<div class="subtitle">
  <span id="when">loading…</span>
  &nbsp;·&nbsp; <span id="auto">auto-refresh 30min</span>
  &nbsp;·&nbsp; <a class="refresh-btn" onclick="refresh()">refresh now</a>
</div>

<div class="grid" id="lanes"></div>"""

_V3_NEW_HEADER = """<header class="appbar">
  <div class="brand"><div class="mark"></div>
    <div><h1>ingest console</h1>
      <div class="subtitle">work-context · <span id="when">loading…</span> &nbsp;·&nbsp; <a class="refresh-btn" onclick="refresh()">refresh</a> &nbsp;·&nbsp; <span id="auto">auto 30min</span></div>
    </div>
  </div>
</header>

<div class="verdict" id="verdict" data-s="ok">
  <div class="beat"><span></span></div>
  <div><div class="vmain" id="vmain">loading…</div><div class="vsub" id="vsub"></div></div>
  <div class="vmeta" id="vmeta"></div>
</div>

<div class="cadence">
  <div class="ch"><h2>Today's cadence</h2>
    <div class="leg"><i><span class="s"></span>ran</i><i><span class="h"></span>upcoming</i><i><span class="nd"></span>now</i></div>
  </div>
  <div class="rail"><div class="railscale" id="railscale"></div><div id="cadLanes"></div></div>
</div>

<div class="kpis" id="kpis"></div>

<div class="sec"><span class="dot"></span><h2>Sources &amp; pipelines</h2><span class="count" id="srcCount"></span><span class="rule"></span></div>
<div class="grid" id="lanes"></div>"""

_V3_OLD_LANEFOR = """function laneFor(name, state, body, tip) {
  const stateClass = state || "ok";
  const t = tip ? ` title="${_esc(tip)}"` : "";
  const pill = (state === "fail" ? `<span class="pill fail"${t}>FAIL</span>`
               : state === "warn" ? `<span class="pill warn"${t}>WARN</span>`
               : `<span class="pill">OK</span>`);
  return `<div class="lane" data-state="${stateClass}">
    <h2>${name} ${pill}</h2>
    ${body}
  </div>`;
}"""

_V3_NEW_LANEFOR = r"""const LANE_META={
  "GITHUB":{ic:"gh",sub:"code · PRs · reviews"}, "JIRA":{ic:"jr",sub:"issues · sprints"},
  "CONFLUENCE":{ic:"cf",sub:"docs · pages"}, "SLACK":{ic:"sl",sub:"channels · threads"},
  "LEAVES":{ic:"lv",sub:"team availability"}, "IDENTITY":{ic:"id",sub:"actor reconcile"},
  "EMBEDDING":{ic:"em",sub:"vectors · clusters"}, "CODE-GRAPH":{ic:"cg",sub:"repos · graph"},
  "HOUSEKEEPING":{ic:"hk",sub:"prune · tidy"}, "ROUTINES":{ic:"rt",sub:"scheduled agents"},
};
function laneFor(name, state, body, tip) {
  const st = state || "ok";
  const meta = LANE_META[name] || {ic:"•", sub:""};
  const t = tip ? ` title="${_esc(tip)}"` : "";
  const pillTxt = st === "fail" ? "FAIL" : st === "warn" ? "WARN" : "OK";
  const pillCls = st === "fail" ? "pill fail" : st === "warn" ? "pill warn" : "pill";
  return `<div class="lane" data-state="${st}"><div class="spine"></div>
    <div class="chd"><div class="ic">${meta.ic}</div>
      <div class="nm">${name}<small>${meta.sub}</small></div>
      <span class="${pillCls}"${t}><span class="pd"></span>${pillTxt}</span></div>
    ${body}</div>`;
}
function _kfmt(n){n=+n||0;if(n>=1e6)return (n/1e6).toFixed(1)+"M";if(n>=1e3)return Math.round(n/1e3)+"K";return ""+n;}
function _relFut(d){const s=(d.getTime()-Date.now())/1000;if(s<60)return"now";if(s<3600)return"~"+Math.floor(s/60)+"m";if(s<86400)return"~"+Math.floor(s/3600)+"h";return"~"+Math.floor(s/86400)+"d";}
function renderVerdict(s){
  const c=sel=>document.querySelectorAll("#lanes .lane"+sel).length;
  const ok=c('[data-state="ok"]'),warn=c('[data-state="warn"]'),fail=c('[data-state="fail"]');
  const v=document.getElementById("verdict"); if(!v)return;
  v.setAttribute("data-s", fail?"bad":warn?"warn":"ok");
  document.getElementById("vmain").textContent = fail?(fail+" source"+(fail>1?"s":"")+" down — needs attention")
    : warn?(warn+" source"+(warn>1?"s":"")+" stale — everything else nominal") : "All systems nominal";
  const lr=Object.values(s.last_run_ts||{}).sort().pop();
  const nr=(s.routines||[]).filter(r=>r.enabled&&r.next_fire_iso).sort((a,b)=>a.next_fire_iso<b.next_fire_iso?-1:1)[0];
  document.getElementById("vsub").innerHTML="last ingest <b>"+(lr?_rel(lr.replace(" ","T")+"+05:30"):"—")
    +"</b> · next routine <b>"+(nr?_esc(nr.id)+" "+(nr.next_fire_rel||""):"—")+"</b> · auto-refresh 30min";
  document.getElementById("vmeta").innerHTML='<div class="m ok"><div class="n">'+ok+'</div><div class="l">healthy</div></div>'
    +'<div class="m warn"><div class="n">'+warn+'</div><div class="l">warn</div></div>'
    +'<div class="m bad"><div class="n">'+fail+'</div><div class="l">down</div></div>';
}
function renderKpis(s, slack, leaves){
  const el=document.getElementById("kpis"); if(!el)return;
  const c=sel=>document.querySelectorAll("#lanes .lane"+sel).length;
  const ok=c('[data-state="ok"]'),warn=c('[data-state="warn"]'),fail=c('[data-state="fail"]'),tot=ok+warn+fail||1;
  const bySrc=(s.db&&s.db.by_source)||{}, ev=Object.values(bySrc).reduce((a,b)=>a+(+b||0),0);
  const evTop=Object.entries(bySrc).sort((a,b)=>b[1]-a[1]).slice(0,3).map(kv=>kv[0]+" "+_kfmt(kv[1])).join(" · ");
  const disc=s.discover||{}, ready=(disc.n_full||0)+(disc.n_team||0)+(disc.n_owner||0), rev=disc.n_review||0;
  const today=leaves.today, out=new Set((leaves.leaves||[]).filter(l=>l.date_start<=today&&(l.date_end||l.date_start)>=today&&(l.reason||"").toLowerCase()!=="wfh").map(l=>l.actor)).size;
  const nh=leaves.next_holiday, hol=nh?("next holiday "+nh.date):"no upcoming holiday";
  let soon=null,soonId=""; (s.routines||[]).forEach(r=>{if(r.enabled&&r.next_fire_iso){const t=new Date(r.next_fire_iso);if(!soon||t<soon){soon=t;soonId=r.id;}}});
  const tiles=[
    ["Sources healthy",ok+" / "+tot, warn+" warn · "+fail+" down", warn||fail],
    ["Events ingested",_kfmt(ev), evTop, 0],
    ["Discover queue",(ready+rev), ready+" ready · "+rev+" review", rev>0],
    ["Out today",out, hol, 0],
    ["Next routine",soon?_relFut(soon):"—", soonId||"—", 0],
  ];
  el.innerHTML=tiles.map(t=>'<div class="kpi'+(t[3]?' flag':'')+'"><div class="l">'+_esc(t[0])+'</div><div class="v">'+_esc(""+t[1])+'</div><div class="h">'+_esc(t[2]||"")+'</div><div class="edge"></div></div>').join("");
}
function _cadPast(d){return d<1?"just now":d<60?d+"m ago":Math.floor(d/60)+"h "+(d%60)+"m ago";}
function _cadFut(d){return d<60?"~"+d+"m":"~"+Math.floor(d/60)+"h "+(d%60)+"m";}
async function renderCadence(){
  let c; try{ c=await (await fetch("/api/cadence")).json(); }catch(e){ return; }
  const nowMin=c.now_min, pct=m=>m/1440*100;
  const sc=document.getElementById("railscale"); if(!sc)return;
  let s=""; for(let h=0;h<=24;h+=3){const x=pct(h*60); s+=`<div class="gl" style="left:${x}%"></div><div class="t" style="left:${x}%">${("0"+h).slice(-2)}</div>`;}
  sc.innerHTML=s;
  const lanes=document.getElementById("cadLanes"); let html="";
  for(const lane of (c.lanes||[])){
    let gl=""; for(let h=0;h<=24;h+=3) gl+=`<div class="gl" style="left:${pct(h*60)}%"></div>`;
    let tk="";
    for(const m of lane.marks){ const [hh,mm]=m.t.split(":").map(Number), mins=hh*60+mm;
      let cls=m.st==="ok"?"ok":m.st==="fail"?"fail":"due";
      if(m.st==="due"&&mins-nowMin<=20&&mins>=nowMin)cls+=" soon";
      const rel=mins<=nowMin?_cadPast(nowMin-mins):"in "+_cadFut(mins-nowMin);
      tk+=`<div class="tick ${cls}" style="left:${pct(mins)}%" data-t="${m.t} · ${_esc(m.job)}" data-m="${m.st==="due"?"upcoming":"ran"} · ${rel}"></div>`;
    }
    html+=`<div class="cadlane"><div class="lname">${_esc(lane.name)}</div><div class="ltrack">${gl}${tk}</div></div>`;
  }
  html+=`<div class="nowfull" style="left:calc(96px + (100% - 96px) * ${nowMin/1440})"><div class="pulse"></div></div>`;
  lanes.innerHTML=html;
  lanes.querySelectorAll(".tick").forEach(t=>{
    t.addEventListener("mouseenter",e=>{const tt=document.getElementById("tooltip");tt.innerHTML=`<b>${_esc(t.dataset.t)}</b><div class="meta">${_esc(t.dataset.m)}</div>`;tt.classList.add("show");moveTooltip(e);});
    t.addEventListener("mousemove",moveTooltip); t.addEventListener("mouseleave",hideTooltip);
  });
}"""

_V3_INSIGHTS_HTML = """  <div class="tail" id="logtail">…</div>
</div>

<div class="sec"><span class="dot"></span><h2>Insights</h2><span class="count" id="insSpan"></span><span class="rule"></span></div>
<div class="totstrip" id="insTotals"></div>
<div class="insights" id="insights"><div class="panel span2" style="text-align:center;color:var(--muted)">loading insights…</div></div>"""

_V3_INSIGHTS_JS = r"""(function(){
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const SRC_COL={slack:"#edb23c",github:"#5eb1ff",jira:"#9b8eff",confluence:"#d56dff"};
const fmtK=n=>n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?Math.round(n/1e3)+"K":""+n;
function panel(title,sub,body,span2){return `<div class="panel${span2?" span2":""}"><div class="ph"><h3>${esc(title)}</h3>${sub?`<span class="sub">${esc(sub)}</span>`:""}</div>${body}</div>`;}
function legend(items){return `<div class="ig-legend">`+items.map(([c,l])=>`<i style="--c:${c}">${esc(l)}</i>`).join("")+`</div>`;}
function vPunch(pc){
  const W=720,H=196,padL=40,padT=6,bot=22,cw=(W-padL-6)/24,ch=(H-padT-bot)/7;
  const order=[1,2,3,4,5,6,0],dn=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"],g={};let max=0;
  pc.forEach(d=>{g[d.wd+"_"+d.hr]=d.n;if(d.n>max)max=d.n;});let s="";
  order.forEach((wd,ri)=>{for(let hr=0;hr<24;hr++){const n=g[wd+"_"+hr]||0,t=max?Math.sqrt(n/max):0,x=padL+hr*cw,y=padT+ri*ch;
    const fill=n?`rgba(45,212,207,${(0.08+0.9*t).toFixed(3)})`:"rgba(127,127,127,0.06)";
    s+=`<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(cw-1.5).toFixed(1)}" height="${(ch-1.5).toFixed(1)}" rx="2" fill="${fill}"><title>${dn[ri]} ${("0"+hr).slice(-2)}:00 — ${n.toLocaleString()} events</title></rect>`;}});
  order.forEach((wd,ri)=>s+=`<text x="${padL-7}" y="${(padT+ri*ch+ch/2+3).toFixed(1)}" text-anchor="end" style="fill:var(--text3)" font-size="9">${dn[ri]}</text>`);
  [0,6,12,18,23].forEach(h=>s+=`<text x="${(padL+h*cw+cw/2).toFixed(1)}" y="${H-6}" text-anchor="middle" style="fill:var(--muted)" font-size="9">${("0"+h).slice(-2)}</text>`);
  return `<div class="chartbox"><svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${s}</svg></div>`;
}
function vStream(rows){
  const W=720,H=190,padL=8,padT=10,bot=20,srcs=["slack","github","jira","confluence"],n=rows.length;if(!n)return"";
  let max=0;rows.forEach(r=>{let t=0;srcs.forEach(k=>t+=r[k]||0);if(t>max)max=t;});
  const xw=(W-padL*2)/(n-1||1),Y=v=>padT+(1-v/max)*(H-padT-bot);let cum=rows.map(()=>0),areas="";
  srcs.forEach(k=>{const top=rows.map((r,i)=>cum[i]+(r[k]||0));
    let d="M "+top.map((v,i)=>`${(padL+i*xw).toFixed(1)},${Y(v).toFixed(1)}`).join(" L ");
    for(let i=n-1;i>=0;i--)d+=` L ${(padL+i*xw).toFixed(1)},${Y(cum[i]).toFixed(1)}`;
    areas+=`<path d="${d} Z" fill="${SRC_COL[k]}" fill-opacity="0.5" stroke="${SRC_COL[k]}" stroke-width="0.9"/>`;cum=top;});
  let xl="";rows.forEach((r,i)=>{if(i%3===0||i===n-1){const an=i===0?"start":i===n-1?"end":"middle",lx=i===0?padL:i===n-1?W-padL:padL+i*xw;
    xl+=`<text x="${lx.toFixed(1)}" y="${H-5}" text-anchor="${an}" style="fill:var(--muted)" font-size="9">${r.month.slice(2)}</text>`;}});
  return `<div class="chartbox"><svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${areas}${xl}</svg></div>`+legend(srcs.map(k=>[SRC_COL[k],k]));
}
function hBars(items){
  const W=480,rowH=Math.max(20,Math.min(28,220/Math.max(items.length,1))),padT=4,labelW=124,max=Math.max(...items.map(i=>i.n),1),barW=W-labelW-54,H=padT*2+items.length*rowH;
  let s="";items.forEach((it,i)=>{const y=padT+i*rowH,w=Math.max(2,it.n/max*barW);
    s+=`<text x="0" y="${y+rowH/2+3}" style="fill:var(--text2)" font-size="11">${esc(it.label)}</text>`
     +`<rect x="${labelW}" y="${y+3}" width="${w.toFixed(1)}" height="${rowH-9}" rx="3" fill="${it.color||"var(--beacon)"}" fill-opacity="0.9"/>`
     +`<text x="${(labelW+w+6).toFixed(1)}" y="${y+rowH/2+3}" style="fill:var(--text)" font-size="11" font-weight="700">${it.n.toLocaleString()}<tspan style="fill:var(--muted)" font-weight="400">${it.sub?" "+esc(it.sub):""}</tspan></text>`;});
  return `<div class="chartbox"><svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${s}</svg></div>`;
}
const scoreCol=s=>s<3?"#47d182":s<10?"#7bcf8e":s<20?"#edb23c":"#f15873";
function vDiverge(rows){
  const W=520,rowH=27,padT=20,mid=W/2,gut=66,max=Math.max(...rows.flatMap(r=>[r.human,r.ai]),1),half=(W-2*gut-60)/2,H=padT+rows.length*rowH+4;
  let s=`<text x="${mid-gut}" y="13" text-anchor="end" style="fill:#5eb1ff" font-size="10" font-weight="700">◀ HUMAN</text><text x="${mid+gut}" y="13" style="fill:#2dd4cf" font-size="10" font-weight="700">AI ▶</text><line x1="${mid}" y1="17" x2="${mid}" y2="${H}" style="stroke:var(--line)"/>`;
  rows.forEach((r,i)=>{const y=padT+i*rowH,hw=r.human/max*half,aw=r.ai/max*half;
    s+=`<rect x="${(mid-gut-hw).toFixed(1)}" y="${y+3}" width="${hw.toFixed(1)}" height="${rowH-10}" rx="3" fill="#5eb1ff" fill-opacity="0.85"/>`
     +`<rect x="${mid+gut}" y="${y+3}" width="${aw.toFixed(1)}" height="${rowH-10}" rx="3" fill="#2dd4cf" fill-opacity="0.85"/>`
     +`<text x="${mid}" y="${y+rowH/2+3}" text-anchor="middle" style="fill:var(--text2)" font-size="9.5">${esc(r.category)}</text>`;
    if(r.human)s+=`<text x="${(mid-gut-hw-5).toFixed(1)}" y="${y+rowH/2+3}" text-anchor="end" style="fill:var(--muted)" font-size="9">${r.human}</text>`;
    if(r.ai)s+=`<text x="${(mid+gut+aw+5).toFixed(1)}" y="${y+rowH/2+3}" style="fill:var(--muted)" font-size="9">${r.ai}</text>`;});
  return `<div class="chartbox"><svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${s}</svg></div>`;
}
function vScatter(pts){
  const W=520,H=240,padL=34,bot=26,padT=10,xmax=Math.max(...pts.map(p=>p.size),10),lx=v=>Math.log10(Math.max(1,v)),lxm=lx(xmax)||1;
  const X=v=>padL+lx(v)/lxm*(W-padL-12),Y=s=>padT+(1-Math.min(s,100)/100)*(H-padT-bot);
  const repos=[...new Set(pts.map(p=>p.repo))],pal=["#5eb1ff","#9b8eff","#47d182","#edb23c"],rc={};repos.forEach((r,i)=>rc[r]=pal[i%pal.length]);
  let ax=`<line x1="${padL}" y1="${padT}" x2="${padL}" y2="${H-bot}" style="stroke:var(--line)"/><line x1="${padL}" y1="${H-bot}" x2="${W-12}" y2="${H-bot}" style="stroke:var(--line)"/>`;
  [0,50,100].forEach(t=>{const y=Y(t);ax+=`<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W-12}" y2="${y.toFixed(1)}" style="stroke:var(--line-faint)" stroke-dasharray="2 3"/><text x="${padL-5}" y="${(y+3).toFixed(1)}" text-anchor="end" style="fill:var(--muted)" font-size="9">${t}</text>`;});
  [10,100,1000,10000].forEach(v=>{if(v<=xmax*1.3){const x=X(v);ax+=`<text x="${x.toFixed(1)}" y="${H-9}" text-anchor="middle" style="fill:var(--muted)" font-size="9">${v>=1000?(v/1000)+"k":v}</text>`;}});
  ax+=`<text x="${W-12}" y="${padT+2}" text-anchor="end" style="fill:var(--text3)" font-size="9">lines changed →</text><text transform="rotate(-90 9 ${((padT+(H-bot))/2).toFixed(1)})" x="9" y="${((padT+(H-bot))/2).toFixed(1)}" text-anchor="middle" style="fill:var(--text3)" font-size="9">friction ↑</text>`;
  let dots="";pts.forEach(p=>dots+=`<circle cx="${X(p.size).toFixed(1)}" cy="${Y(p.score).toFixed(1)}" r="3" fill="${rc[p.repo]}" fill-opacity="0.45"><title>${esc(p.repo)} · ${p.size.toLocaleString()} LOC · friction ${p.score} · ${esc(p.cat)}</title></circle>`);
  return `<div class="chartbox"><svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${ax}${dots}</svg></div>`+legend(repos.map(r=>[rc[r],r]));
}
function vCadence(rows){
  const W=520,H=190,padL=8,bot=20,padT=8,n=rows.length,max=Math.max(...rows.map(r=>r.total),1),gap=(W-padL*2)/n,bw=gap*0.62,Y=v=>padT+(1-v/max)*(H-padT-bot);
  let s="";rows.forEach((r,i)=>{const x=padL+i*gap+(gap-bw)/2,h=(H-bot)-Y(r.total),safe=r.total-r.emergency-r.rolled_back;
    let yy=H-bot;const seg=(val,col)=>{if(val<=0)return;const hh=h*val/r.total;yy-=hh;s+=`<rect x="${x.toFixed(1)}" y="${yy.toFixed(1)}" width="${bw.toFixed(1)}" height="${hh.toFixed(1)}" fill="${col}"/>`;};
    seg(safe,"#47d182");seg(r.emergency,"#edb23c");seg(r.rolled_back,"#f15873");
    if(i%2===0||i===n-1)s+=`<text x="${(x+bw/2).toFixed(1)}" y="${H-5}" text-anchor="middle" style="fill:var(--muted)" font-size="8.5">${r.month.slice(2)}</text>`;});
  return `<div class="chartbox"><svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${s}</svg></div>`+legend([["#47d182","released ok"],["#edb23c","emergency"],["#f15873","rolled back"]]);
}
function squarify(data,W,H){
  const total=data.reduce((s,d)=>s+d.n,0);if(!total)return[];const area=data.map(d=>d.n/total*W*H),res=[];let rect={x:0,y:0,w:W,h:H},i=0;
  const worst=(row,len)=>{const s=row.reduce((a,b)=>a+b,0),mx=Math.max(...row),mn=Math.min(...row);return Math.max(len*len*mx/(s*s),s*s/(len*len*mn));};
  while(i<area.length){let row=[],len=Math.min(rect.w,rect.h),j=i;
    while(j<area.length){const cand=row.concat(area[j]);if(row.length===0||worst(cand,len)<=worst(row,len)){row=cand;j++;}else break;}
    const sum=row.reduce((a,b)=>a+b,0);
    if(rect.w>=rect.h){const cw=sum/rect.h;let y=rect.y;for(let k=0;k<row.length;k++){const hh=row[k]/sum*rect.h;res.push({...data[i+k],x:rect.x,y,w:cw,h:hh});y+=hh;}rect.x+=cw;rect.w-=cw;}
    else{const rh=sum/rect.w;let x=rect.x;for(let k=0;k<row.length;k++){const ww=row[k]/sum*rect.w;res.push({...data[i+k],x,y:rect.y,w:ww,h:rh});x+=ww;}rect.y+=rh;rect.h-=rh;}
    i+=row.length;}
  return res;
}
function vTreemap(data){
  const W=720,H=240,cells=squarify(data,W,H),maxN=Math.max(...data.map(d=>d.n),1);let s="";
  cells.forEach(c=>{const t=c.n/maxN,fill=`rgba(45,212,207,${(0.14+0.66*t).toFixed(3)})`;
    s+=`<rect x="${c.x.toFixed(1)}" y="${c.y.toFixed(1)}" width="${(c.w-2).toFixed(1)}" height="${(c.h-2).toFixed(1)}" rx="4" fill="${fill}" stroke="var(--well)" stroke-width="1.5"><title>${esc(c.slug)} — ${c.n} clusters</title></rect>`;
    if(c.w>54&&c.h>26){const tc=t<0.42?"#cdeeec":"#05201f",tc2=t<0.42?"#cdeeec99":"#05201f99";
      s+=`<text x="${(c.x+7).toFixed(1)}" y="${(c.y+16).toFixed(1)}" style="fill:${tc};font-weight:700" font-size="${c.w>120?12:10.5}">${esc(c.slug.length>Math.floor(c.w/7)?c.slug.slice(0,Math.floor(c.w/7)-1)+"…":c.slug)}</text>`;
      if(c.h>40)s+=`<text x="${(c.x+7).toFixed(1)}" y="${(c.y+31).toFixed(1)}" style="fill:${tc2}" font-size="9.5">${c.n}</text>`;}});
  return `<div class="chartbox"><svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${s}</svg></div>`;
}
function vStack(rows,colmap){
  const W=480,H=34,total=rows.reduce((s,r)=>s+r.n,0)||1;let x=0,s="";
  rows.forEach(r=>{const w=r.n/total*W;s+=`<rect x="${x.toFixed(1)}" y="0" width="${Math.max(0,w-1.5).toFixed(1)}" height="${H}" rx="3" fill="${colmap[r.status]||"var(--muted)"}" fill-opacity="0.9"><title>${esc(r.status)} — ${r.n}</title></rect>`;
    if(w>40)s+=`<text x="${(x+w/2).toFixed(1)}" y="21" text-anchor="middle" style="fill:#05181a;font-weight:700" font-size="11">${r.n}</text>`;x+=w;});
  return `<div class="chartbox" style="padding:10px 8px"><svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${s}</svg></div>`+legend(rows.map(r=>[colmap[r.status]||"var(--muted)",r.status.toLowerCase()]));
}
function vOps(rows){
  const W=520,H=180,padL=8,bot=20,padT=8,pats=["incident","drill","rca","rollback","year_end"];
  const col={incident:"#f15873",drill:"#edb23c",rca:"#5eb1ff",rollback:"#9b8eff",year_end:"#566a72"};
  const n=rows.length,max=Math.max(...rows.map(r=>pats.reduce((s,p)=>s+(r[p]||0),0)),1),gap=(W-padL*2)/n,bw=gap*0.62,H0=H-bot;let s="";
  rows.forEach((r,i)=>{const x=padL+i*gap+(gap-bw)/2,tot=pats.reduce((s,p)=>s+(r[p]||0),0),h=tot/max*(H0-padT);let yy=H0;
    pats.forEach(p=>{const v=r[p]||0;if(!v)return;const hh=h*v/tot;yy-=hh;s+=`<rect x="${x.toFixed(1)}" y="${yy.toFixed(1)}" width="${bw.toFixed(1)}" height="${hh.toFixed(1)}" fill="${col[p]}"><title>${r.month} · ${p}: ${v}</title></rect>`;});
    if(i%2===0||i===n-1)s+=`<text x="${(x+bw/2).toFixed(1)}" y="${H-5}" text-anchor="middle" style="fill:var(--muted)" font-size="8.5">${r.month.slice(2)}</text>`;});
  return `<div class="chartbox"><svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${s}</svg></div>`+legend(pats.map(p=>[col[p],p]));
}
const STATUS_COL={ACTIVE:"#47d182",RECURRING:"#5eb1ff",STALE:"#edb23c",RESOLVED:"#566a72"};
const OUTCOME_COL={released:"#47d182",emergency:"#edb23c",cancelled:"#566a72",rolled_back:"#f15873",pending:"#5eb1ff"};
function renderInsights(D){
  if(!D||D.error){document.getElementById("insights").innerHTML=`<div class="panel span2" style="color:var(--warn)">insights unavailable${D&&D.error?": "+esc(D.error):""}</div>`;return;}
  const t=D.totals||{};
  const span=document.getElementById("insSpan"); if(span)span.textContent=t.span?`${t.span[0]} → ${t.span[1]}`:"";
  document.getElementById("insTotals").innerHTML=[["events",fmtK(t.events)],["pull requests",t.prs],["releases",t.releases],["topic clusters",t.clusters],["slack threads",fmtK(t.threads)]]
    .map(([l,v])=>`<span><b>${esc(""+v)}</b><span class="lbl">${esc(l)}</span></span>`).join("");
  const fr=(D.friction||[]).map(f=>({label:f.category,n:f.n,sub:f.avg_score?`avg ${f.avg_score}`:"",color:scoreCol(f.avg_score)}));
  const ro=(D.release_outcomes||[]).map(o=>({label:o.outcome,n:o.n,color:OUTCOME_COL[o.outcome],sub:o.features?`${o.features} feat`:""}));
  const cc=(D.channel_class||[]).map(c=>({label:c.cls,n:c.msgs,sub:`${fmtK(c.threads)} thr`,color:"var(--beacon)"}));
  const panels=[
    panel("Work rhythm","hour × weekday · IST · darker = busier", vPunch(D.punchcard||[]), true),
    panel("Activity stream","monthly volume by source", vStream(D.stream||[]), true),
    panel("PR friction","dominant category · color = avg score", hBars(fr)),
    panel("Who flags what","review comments · human vs AI", vDiverge(D.review_taxonomy||[])),
    panel("PR size vs friction","each dot = one PR (log scale)", vScatter(D.pr_scatter||[]), true),
    panel("Release outcomes",(t.releases||"")+" change requests", hBars(ro)),
    panel("Release cadence","monthly · emergency + rollback highlighted", vCadence(D.release_cadence||[])),
    panel("Projects by cluster volume","topic clusters mapped to projects", vTreemap(D.projects||[]), true),
    panel("Cluster lifecycle",(t.clusters||"")+" topic clusters by status", vStack(D.cluster_status||[],STATUS_COL)),
    panel("Channel mix","slack threads by channel class", hBars(cc)),
    panel("Incidents & ops","monthly · incident/drill/rca/rollback", vOps(D.ops_timeline||[]), true),
  ];
  document.getElementById("insights").innerHTML=panels.join("");
}
fetch("/api/insights").then(r=>r.json()).then(renderInsights).catch(e=>{document.getElementById("insights").innerHTML=`<div class="panel span2" style="color:var(--warn)">insights data unavailable</div>`;});
})();
"""


def _build_index_v3() -> str:
    """v3 console — transforms INDEX_HTML (reuses all per-source data logic)."""
    import re as _re
    html = INDEX_HTML
    html = html.replace("<title>cron-status · dashboard</title>",
                        "<title>ingest console · v3</title>" + _V3_FONTS)
    html = _re.sub(r"<style>.*?</style>",
                   lambda _m: "<style>" + _V3_CSS + "</style>", html, count=1, flags=_re.S)
    html = html.replace(_V3_OLD_HEADER, _V3_NEW_HEADER)
    html = html.replace(_V3_OLD_LANEFOR, _V3_NEW_LANEFOR)
    html = html.replace(
        'document.getElementById("lanes").innerHTML = lanes.join("");',
        'document.getElementById("lanes").innerHTML = lanes.join("");\n'
        '  renderVerdict(s); renderKpis(s, slack, leaves); renderCadence();')
    # Routines lane: add a last-success column next to last-run.
    html = html.replace(
        '<table><tr><th></th><th>routine</th><th>cadence</th>\n'
        '                  <th>last run</th><th>next fire</th></tr>',
        '<table><tr><th></th><th>routine</th><th>cadence</th>\n'
        '                  <th>last run</th><th>last ok</th><th>next fire</th></tr>')
    html = html.replace(
        '              <td class="muted">${r.last_run_rel || "—"}</td>\n'
        '              <td class="muted">${next}</td></tr>`;',
        '              <td class="muted">${r.last_run_rel || "—"}</td>\n'
        '              <td class="muted">${r.last_success_rel || "—"}</td>\n'
        '              <td class="muted">${next}</td></tr>`;')
    # Insights deck after the log-tail panel.
    html = html.replace('  <div class="tail" id="logtail">…</div>\n</div>',
                        _V3_INSIGHTS_HTML)
    # Insights JS just before the bootstrap calls.
    html = html.replace(
        'refreshAll();\nloadLogList();\nloadClusters();\nsetInterval(refreshAll, 1_800_000);',
        _V3_INSIGHTS_JS + '\nrefreshAll();\nloadLogList();\nloadClusters();\nsetInterval(refreshAll, 1_800_000);')
    return html


INDEX_V3_HTML = _build_index_v3()


# ── v4: v3 console split into Status / Insights tabs ──────────────────────────
# Pure transform of INDEX_V3_HTML. The always-on glance (appbar + verdict +
# cadence + KPIs) stays pinned; the long scroll below is split into two focused
# views — Status (source lanes, routines, signal chart, clusters, logs) and
# Insights (the analytics deck, promoted out of the bottom of the page). Every
# lane, viz, the cluster pack and logs are preserved, just reorganised.
_V4_TABS = """<nav class="vtabs" id="vtabs">
  <button class="vtab on" data-v="vStatus">Status</button>
  <button class="vtab" data-v="vInsights">Insights</button>
</nav>"""
_V4_CSS = """
.vtabs{display:flex;gap:4px;margin:20px 0 2px;border-bottom:1px solid var(--line-faint);}
.vtab{background:none;border:0;padding:9px 16px;font:600 13px var(--sans);color:var(--muted);cursor:pointer;position:relative;}
.vtab:hover{color:var(--text);}
.vtab.on{color:var(--accent);}
.vtab.on::after{content:"";position:absolute;left:14px;right:14px;bottom:-1px;height:2px;background:var(--accent);border-radius:2px;}
.vpanel{display:none;} .vpanel.vpanel-on{display:block;}
"""
_V4_JS = """<script>
(function(){var t=document.getElementById('vtabs');if(!t)return;
 t.addEventListener('click',function(e){var b=e.target.closest('.vtab');if(!b)return;
  document.querySelectorAll('.vtab').forEach(function(x){x.classList.toggle('on',x===b);});
  document.querySelectorAll('.vpanel').forEach(function(p){p.classList.toggle('vpanel-on',p.id===b.dataset.v);});
  window.scrollTo({top:0,behavior:'auto'});});})();
</script>"""


def _build_index_v4() -> str:
    """v3 visuals + a Status/Insights tab split. Falls back to v3 on any miss."""
    html = INDEX_V3_HTML
    html = html.replace("<title>ingest console · v3</title>", "<title>ingest console · v4</title>")
    html = html.replace("</style>", _V4_CSS + "</style>", 1)
    html = html.replace(
        '<div class="kpis" id="kpis"></div>\n\n<div class="sec"><span class="dot"></span><h2>Sources &amp; pipelines</h2>',
        '<div class="kpis" id="kpis"></div>\n' + _V4_TABS
        + '\n<div id="vStatus" class="vpanel vpanel-on">\n<div class="sec"><span class="dot"></span><h2>Sources &amp; pipelines</h2>')
    html = html.replace(
        '<div class="sec"><span class="dot"></span><h2>Insights</h2>',
        '</div><!--/vStatus-->\n<div id="vInsights" class="vpanel">\n<div class="sec"><span class="dot"></span><h2>Insights</h2>')
    html = html.replace(
        '<div class="insights" id="insights"><div class="panel span2" style="text-align:center;color:var(--muted)">loading insights…</div></div>',
        '<div class="insights" id="insights"><div class="panel span2" style="text-align:center;color:var(--muted)">loading insights…</div></div>\n</div><!--/vInsights-->')
    html = html.replace("</body>", _V4_JS + "\n</body>", 1)
    return html


INDEX_V4_HTML = _build_index_v4()


# ── v5: exception-first health view ───────────────────────────────────────────
# Builds on v4 (tabs). Adds a compact health grid at the top of Status — one chip
# per source, problems first, colour = state — and COLLAPSES healthy lane cards to
# just their header so anomalies are the only thing drawing detail. Click a chip
# (or a collapsed card's header) to drill in. Reads the rendered `data-state` on
# each `.lane`, so it needs no new data — every field survives in the drill-down.
_V5_CSS = """
.healthgrid{display:flex;flex-wrap:wrap;gap:7px;margin:14px 0 16px;}
.hchip{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line-faint);background:var(--panel);
  border-radius:999px;padding:6px 13px;font:600 11.5px var(--sans);color:var(--muted);cursor:pointer;transition:border-color .12s,color .12s,opacity .12s;}
.hchip:hover{border-color:var(--muted);color:var(--text);opacity:1;}
.hchip .hd{width:8px;height:8px;border-radius:50%;background:var(--green);flex:none;}
.hchip b{font:500 10px var(--mono);text-transform:uppercase;opacity:.6;}
.hchip.warn{color:var(--yellow);border-color:var(--warn-line);} .hchip.warn .hd{background:var(--yellow);}
.hchip.fail{color:var(--red);border-color:var(--bad-line);} .hchip.fail .hd{background:var(--red);}
.hchip.ok{opacity:.58;}
#lanes .lane .chd{cursor:pointer;}
#lanes .lane.collapsed > *:not(.spine):not(.chd){display:none!important;}
"""
_V5_RENDERHEALTH = """function _laneName(l){var n=l.querySelector('.nm');if(!n)return '';
  return (n.childNodes[0]?n.childNodes[0].textContent:n.textContent).trim();}
function renderHealth(){
  var lanes=[].slice.call(document.querySelectorAll('#lanes .lane'));
  var rank={fail:0,warn:1,ok:2};
  var chips=lanes.map(function(l){return {st:(l.getAttribute('data-state')||'ok'),nm:_laneName(l),
    ic:((l.querySelector('.ic')||{}).textContent||'')};})
    .sort(function(a,b){return (rank[a.st]==null?2:rank[a.st])-(rank[b.st]==null?2:rank[b.st]);});
  var hg=document.getElementById('healthgrid');
  if(hg)hg.innerHTML=chips.map(function(c){return '<button class="hchip '+c.st+'" data-nm="'+c.nm+
    '"><span class="hd"></span><b>'+c.ic+'</b> '+c.nm+'</button>';}).join('');
  lanes.forEach(function(l){l.classList.toggle('collapsed',(l.getAttribute('data-state')||'ok')==='ok');});
}
"""
_V5_CLICK_JS = """<script>
document.addEventListener('click',function(e){
  var chip=e.target.closest&&e.target.closest('.hchip');
  if(chip){var nm=chip.getAttribute('data-nm');
    var lane=[].slice.call(document.querySelectorAll('#lanes .lane')).filter(function(l){return _laneName(l)===nm;})[0];
    if(lane){lane.classList.remove('collapsed');lane.scrollIntoView({behavior:'smooth',block:'center'});
      lane.style.transition='box-shadow .2s';lane.style.boxShadow='0 0 0 2px var(--accent)';
      setTimeout(function(){lane.style.boxShadow='';},1100);}
    return;}
  var chd=e.target.closest&&e.target.closest('#lanes .lane .chd');
  if(chd){var l=chd.closest('.lane');if(l)l.classList.toggle('collapsed');}
});
</script>"""


def _build_index_v5() -> str:
    """v4 + exception-first health grid & collapsed healthy lanes."""
    html = INDEX_V4_HTML
    html = html.replace("<title>ingest console · v4</title>", "<title>ingest console · v5</title>")
    html = html.replace("</style>", _V5_CSS + "</style>", 1)
    html = html.replace('<div class="grid" id="lanes"></div>',
                        '<div class="healthgrid" id="healthgrid"></div>\n<div class="grid" id="lanes"></div>')
    html = html.replace('function renderVerdict(s){', _V5_RENDERHEALTH + 'function renderVerdict(s){')
    html = html.replace('renderVerdict(s); renderKpis(s, slack, leaves); renderCadence();',
                        'renderVerdict(s); renderKpis(s, slack, leaves); renderCadence(); renderHealth();')
    html = html.replace("</body>", _V5_CLICK_JS + "\n</body>", 1)
    return html


INDEX_V5_HTML = _build_index_v5()


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
  // Re-evaluate the time-based auto theme. The setInterval alone is unreliable:
  // Chrome throttles/pauses timers in background tabs, so a tab left open across
  // the 7pm boundary never flips. Re-apply whenever the tab regains focus /
  // becomes visible so it's always correct the moment the user looks at it.
  function recheck(){ if((localStorage.getItem(KEY)||"auto")==="auto") apply("auto"); }
  setInterval(recheck, 60000);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden) recheck(); });
  window.addEventListener("focus", recheck);
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
  // Re-evaluate the time-based auto theme. The setInterval alone is unreliable:
  // Chrome throttles/pauses timers in background tabs, so a tab left open across
  // the 7pm boundary never flips. Re-apply whenever the tab regains focus /
  // becomes visible so it's always correct the moment the user looks at it.
  function recheck(){ if((localStorage.getItem(KEY)||"auto")==="auto") apply("auto"); }
  setInterval(recheck, 60000);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden) recheck(); });
  window.addEventListener("focus", recheck);
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
  // Re-evaluate the time-based auto theme. The setInterval alone is unreliable:
  // Chrome throttles/pauses timers in background tabs, so a tab left open across
  // the 7pm boundary never flips. Re-apply whenever the tab regains focus /
  // becomes visible so it's always correct the moment the user looks at it.
  function recheck(){ if((localStorage.getItem(KEY)||"auto")==="auto") apply("auto"); }
  setInterval(recheck, 60000);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden) recheck(); });
  window.addEventListener("focus", recheck);
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


# ── v3 re-skin for the standalone subpages (/channels, /leaves) ───────────────
# These pages share v1's cron palette. Rather than rewrite them, append an
# override <style> (CSS vars win by cascade order) that maps them onto the v3
# teal-ink + cyan-beacon palette and Space Grotesk / JetBrains Mono fonts.
_V3_RESKIN = _V3_FONTS + """<style>
:root{
  --bg:#080d11; --bg-deep:#06090c; --panel:#0f171d; --panel-raised:#16242c;
  --hover:#0f1a20; --line:#1e2c34; --line-faint:#16222a;
  --text:#dde6e9; --text2:#8ea0a8; --text-strong:#ffffff;
  --muted:#566a72; --muted2:#6a7e86; --ink:#05080a;
  --overlay:#ffffff0d; --overlay-faint:#ffffff09;
  --green:#47d182; --yellow:#edb23c; --red:#f15873; --blue:#2dd4cf;
  --purple:#9b8eff; --amber:#edb23c; --accent:#2dd4cf;
  --pill-ok-bg:#0e2a1d; --pill-warn-bg:#2e2410; --pill-fail-bg:#2e151c;
}
html[data-theme="light"]{
  --bg:#eef2f3; --bg-deep:#dfe6e8; --panel:#ffffff; --panel-raised:#ffffff;
  --hover:#eef1f5; --line:#dde5e8; --line-faint:#e8eef0;
  --text:#13242a; --text2:#47585f; --text-strong:#000000;
  --muted:#869399; --muted2:#6b7c83; --ink:#ffffff;
  --overlay:#0000000f; --overlay-faint:#00000008;
  --green:#0f9d57; --yellow:#b87708; --red:#d13a55; --blue:#0c968f;
  --purple:#6f5fe0; --amber:#b87708; --accent:#0c968f;
  --pill-ok-bg:#dcf3e7; --pill-warn-bg:#f6ecd3; --pill-fail-bg:#f8dee4;
}
body{font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;}
h1,h2{font-family:"Space Grotesk",system-ui,sans-serif;letter-spacing:.2px;}
#themeToggle{border-radius:999px;}
#themeToggle button.on{background:var(--blue);color:var(--ink);}
</style>"""


def _v3_reskin(html: str) -> str:
    return html.replace("</head>", _V3_RESKIN + "</head>", 1)


CHANNELS_HTML = _v3_reskin(CHANNELS_HTML)
LEAVES_HTML = _v3_reskin(LEAVES_HTML)
LEAVES_V2_HTML = _v3_reskin(LEAVES_V2_HTML)


# ── shared nav sidebar ────────────────────────────────────────────────────────
# One source of truth for the whole ecosystem lives in derive/synapse_nav.py.
from derive import synapse_nav  # noqa: E402


def _inject_nav(html: str, path: str) -> str:
    return synapse_nav.inject_html(html, synapse_nav.active_from_path(path))


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
        b = _inject_nav(body, getattr(self, "path", "")).encode()
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
        if path == "/" or path == "/index.html" or path == "/v5":
            self._send_html(INDEX_V5_HTML)   # v5 (exception-first) is the default
        elif path == "/v3":
            self._send_html(INDEX_V3_HTML)
        elif path == "/v4":
            self._send_html(INDEX_V4_HTML)
        elif path == "/v2":
            self._send_html(INDEX_V2_HTML)
        elif path == "/v1":
            self._send_html(INDEX_HTML)
        elif path == "/channels":
            self._send_html(CHANNELS_HTML)
        elif path == "/api/cadence":
            self._send_json(get_cadence())
        elif path == "/api/insights":
            self._send_json(get_insights())
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
        elif path == "/api/logs":
            self._send_json({"logs": get_log_list()})
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

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


def get_log_tail(name: str, n: int = 80) -> list[str]:
    safe = {"identity_reconcile.log", "ingest.log", "rollup.log",
            "housekeeping.log", "github-reset.log"}
    if name not in safe:
        return [f"refused: {name} not in allowlist"]
    p = LOGS / name
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
:root { color-scheme: dark; }
body { font: 13px ui-monospace,SFMono-Regular,Menlo,monospace; background:#0b0f14;
       color:#d5d9e0; max-width: 1240px; margin: 16px auto; padding: 0 16px; }
h1 { font-size:18px; margin:0 0 6px; letter-spacing:1px; }
.subtitle { color:#6e7681; margin-bottom:18px; font-size:11px; }
.grid { display:grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.lane { background:#11161d; border-left:3px solid #4d8eff; border-radius:4px;
        padding:10px 14px; }
.lane[data-state="ok"]   { border-color:#48d597; }
.lane[data-state="warn"] { border-color:#d5b248; }
.lane[data-state="fail"] { border-color:#d54848; }
.lane h2 { font-size:13px; margin:0 0 8px; display:flex; justify-content:space-between; }
.pill { font-size:10px; padding:1px 6px; border-radius:8px; background:#193b25; color:#48d597; }
.pill.warn { background:#3b2e19; color:#d5b248; }
.pill.fail { background:#3b1919; color:#d54848; }
.kv { display:grid; grid-template-columns:110px 1fr; gap:2px 12px; font-size:12px;
      color:#a7afba; }
.kv b { color:#d5d9e0; font-weight:normal; }
details { margin-top:8px; }
summary { cursor:pointer; color:#7a8497; font-size:11px; padding:4px 0; }
details[open] summary { color:#d5d9e0; }
table { border-collapse:collapse; width:100%; margin-top:6px; font-size:12px; }
th { color:#6e7681; font-weight:normal; text-align:left; padding:3px 6px;
     border-bottom:1px solid #2a313b; cursor:pointer; user-select:none; }
th:hover { color:#d5d9e0; }
td { padding:3px 6px; }
tr:nth-child(even) td { background:#0e1319; }
.muted { color:#6e7681; }
.chart-wrap { background:#11161d; padding:12px; border-radius:4px; margin-top:14px; }
.tail { background:#080c10; padding:8px; border-radius:3px; max-height:260px;
        overflow:auto; white-space:pre; font-size:11px; color:#8e95a0;
        border:1px solid #1a212b; }
.finding { font-size:11px; padding:4px 8px; margin:4px 0; border-radius:3px;
           border-left:3px solid #2a313b; background:#0e1319; }
.finding.warn { border-left-color:#d5b248; }
.finding.fail { border-left-color:#d54848; }
.finding.muted { border-left-color:#5a6070; opacity:0.7; }
.finding b { color:#d5d9e0; }
.cluster { background:#11161d; border-left:3px solid #4d8eff; padding:10px 12px;
           margin:8px 0; border-radius:3px; }
.cluster h3 { font-size:13px; margin:0 0 4px; color:#d5d9e0; font-weight:normal; }
.cluster .meta { color:#6e7681; font-size:11px; margin-bottom:4px; }
.cluster .summary { color:#a7afba; font-size:12px; margin:6px 0; }
.cluster .chips span { display:inline-block; padding:1px 6px; margin:2px;
                       font-size:10px; background:#1a212b; border-radius:8px;
                       color:#a7afba; }
.cluster .json-block { background:#080c10; padding:6px; margin:4px 0;
                       border-radius:3px; font-size:11px; color:#8e95a0;
                       white-space:pre-wrap; word-break:break-word; }
.cluster[data-status="ACTIVE"]    { border-left-color:#48d597; }
.cluster[data-status="RECURRING"] { border-left-color:#4d8eff; }
.cluster[data-status="STALE"]     { border-left-color:#d5b248; }
.cluster[data-status="RESOLVED"]  { border-left-color:#6e7681; }
/* Cluster pack chart */
#clusterPack { width:100%; height:560px; background:#080c10; border-radius:4px;
               border:1px solid #1a212b; }
#clusterPack circle { stroke:#0b0f14; stroke-width:1.2; cursor:pointer; }
#clusterPack circle:hover { stroke:#fff; stroke-width:2; }
#clusterPack text { fill:#d5d9e0; pointer-events:none; font-size:11px;
                     text-anchor:middle; font-family: inherit; }
.tooltip { position:absolute; background:#161c25; color:#d5d9e0; padding:8px 10px;
           border-radius:4px; border:1px solid #2a313b; font-size:11px;
           max-width:340px; pointer-events:none; opacity:0;
           transition:opacity 0.12s; z-index:999; box-shadow:0 4px 16px #000a; }
.tooltip.show { opacity:1; }
.tooltip b { color:#fff; }
.tooltip .meta { color:#a7afba; font-size:10px; margin-top:4px; }
.view-toggle { display:inline-flex; gap:4px; }
.view-toggle button { background:#11161d; color:#7a8497; border:1px solid #2a313b;
                      padding:3px 10px; font:inherit; font-size:11px; cursor:pointer;
                      border-radius:3px; }
.view-toggle button.active { background:#1a212b; color:#d5d9e0; border-color:#4d8eff; }
#clusterDetail { margin-top:12px; }
.legend { display:flex; gap:14px; font-size:11px; color:#a7afba; margin:8px 0; }
.legend span::before { content:"●"; margin-right:4px; }
.legend .active::before    { color:#48d597; }
.legend .recurring::before { color:#4d8eff; }
.legend .stale::before     { color:#d5b248; }
.legend .resolved::before  { color:#6e7681; }
.row { display:flex; gap:14px; align-items:center; }
.row label { color:#6e7681; font-size:11px; }
.row select, .row button { background:#11161d; color:#d5d9e0; border:1px solid #2a313b;
        padding:4px 8px; font:inherit; border-radius:3px; }
.refresh-btn { cursor:pointer; }
</style></head>
<body>
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
  return `<div class="finding ${rhv.level}"><b>${rhv.level.toUpperCase()}</b> · runtime — `
       + `${rhv.label} (run duration vs gap to next fire)</div>`;
}
function laneFor(name, state, body) {
  const stateClass = state || "ok";
  const pill = (state === "fail" ? `<span class="pill fail">FAIL</span>`
               : state === "warn" ? `<span class="pill warn">WARN</span>`
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
      ? `<div class="finding ${cronState}"><b>${cronState.toUpperCase()}</b> · schedule — `
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
      .map(([sev, check, msg]) =>
        `<div class="finding ${sev.toLowerCase()}"><b>${sev}</b> · ${check} — ${msg}</div>`)
      .join("");

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
      </details>`));
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
    .map(([sev, check, msg]) =>
      `<div class="finding ${sev.toLowerCase()}"><b>${sev}</b> · ${check} — ${msg}</div>`)
    .join("");
  const slackValSev = (slackV.findings || []).find(f => f[0] === "FAIL") ? "fail"
                  : (slackV.findings || []).find(f => f[0] === "WARN") ? "warn"
                  : "ok";
  const slackRh = s.run_health?.slack || null;
  const slackWorst = _worstState(slackValSev, slackRh?.level);
  const slackLastRun = s.last_run_ts?.slack;
  const slackLastIso = slackLastRun ? slackLastRun.replace(" ", "T") + "+05:30" : null;
  const disc = s.discover || {};
  const discReady = (disc.n_full || 0) + (disc.n_team || 0) + (disc.n_owner || 0);
  const discOwnerStr = disc.n_owner ? ` · ${disc.n_owner} owner` : "";
  const discSilentStr = disc.n_silent ? ` · ${disc.n_silent} team-silent` : "";
  const discRow = disc.sched ? `
      <span>discover</span><b>${discReady} ready${discOwnerStr} · <span class="muted">${disc.n_review || 0} needs_review${discSilentStr}</span></b>
      <span>disc-sched</span><b>${disc.sched} IST · next ${disc.next}</b>` : "";
  lanes.push(laneFor("SLACK", slackWorst, `
    <div class="kv">
      <span>last run</span><b>${slackLastRun || "—"} <span class="muted">(${_rel(slackLastIso)})</span></b>
      <span>cursors</span><b>${Object.keys(s.slack_cursors).length}</b>
      <span>events</span><b>${(s.db?.by_source?.slack || 0).toLocaleString()}</b>
      ${_runtimeBadge(slackRh)}
      ${discRow}
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
    </details>`));

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
      </div>`));
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
    lanes.push(laneFor("CODE-GRAPH", gState, `
      <div class="kv">
        <span>schedule</span><b>daily ${g.sched || "18:00 IST"}${g.next && !g.running ? ` · next ${g.next}` : ""}</b>
        <span>last run</span><b>${lastRunCell}</b>
        <span>success.date</span><b>${sd || "—"}</b>
        <span>repos</span><b>${repos || "—"}</b>
      </div>`));
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
    lanes.push(laneFor("HOUSEKEEPING", hState, `
      <div class="kv">
        <span>schedule</span><b>${h.sched || "—"} IST${h.next ? ` · next ${h.next}` : ""}</b>
        <span>last run</span><b>${lastCell}</b>
        <span>policy</span><b class="muted">weekly · prune old bak/verdicts/handoffs/logs + .DS_Store</b>
        <span>pruned</span><b class="muted">${actStr}</b>
      </div>`));
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
        ${rows}</table>`));
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
  sigChart = new Chart(document.getElementById("sigChart"), {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: "#a7afba" }}},
      scales: {
        x: { ticks: { color: "#6e7681", maxRotation:0, autoSkip:true, maxTicksLimit: 12 }},
        y: { ticks: { color: "#6e7681" }, grid: { color: "#1a212b" }, beginAtZero: true },
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
:root { color-scheme: dark; }
body { font: 13px ui-monospace,SFMono-Regular,Menlo,monospace; background:#0b0f14;
       color:#d5d9e0; max-width: 1100px; margin: 16px auto; padding: 0 16px; }
h1 { font-size:18px; margin:0 0 4px; letter-spacing:1px; }
.subtitle { color:#6e7681; margin-bottom:14px; font-size:11px; }
a { color:#4d8eff; text-decoration:none; }
a:hover { text-decoration:underline; }
input { background:#11161d; color:#d5d9e0; border:1px solid #2a313b; border-radius:3px;
        padding:5px 8px; font:inherit; width:260px; margin-bottom:10px; }
table { border-collapse:collapse; width:100%; font-size:12px; }
th { color:#6e7681; font-weight:normal; text-align:left; padding:4px 8px;
     border-bottom:1px solid #2a313b; cursor:pointer; user-select:none; position:sticky; top:0; background:#0b0f14; }
th:hover { color:#d5d9e0; }
td { padding:4px 8px; }
tr:nth-child(even) td { background:#0e1319; }
.muted { color:#6e7681; }
.pill { font-size:10px; padding:1px 6px; border-radius:8px; background:#193b25; color:#48d597; }
.pill.warn { background:#3b2e19; color:#d5b248; }
.tag { font-size:10px; color:#7a8497; border:1px solid #2a313b; border-radius:8px; padding:0 5px; margin-left:4px; }
</style></head>
<body>
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

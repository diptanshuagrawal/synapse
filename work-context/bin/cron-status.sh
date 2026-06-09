#!/usr/bin/env python3
"""Ingest cron health — rich dashboard with DB stats, event breakdown, relative times."""

from datetime import datetime, timezone, timedelta
from pathlib import Path
import json, plistlib, re, sqlite3, sys

# Shared overrun-detection helpers (also used by bin/dashboard.py). bin/ is
# already sys.path[0] when run as a script, but insert defensively.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _run_health as rh
import _codegraph_status as cg

ROOT      = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from derive.sources_config import launchd_prefix, github_org, jira_project_keys  # noqa: E402
_LP = launchd_prefix()

IST       = timezone(timedelta(hours=5, minutes=30))
LOG       = ROOT / "logs/ingest.log"
ROLLUP_LOG = ROOT / "logs/rollup.log"
CURSORS   = ROOT / "state/cursors.json"
STATE_DIR = ROOT / "state"
DB_PATH   = ROOT / "index/events.db"
PLIST_DIR = Path.home() / "Library/LaunchAgents"

SOURCES   = ["github", "jira", "confluence", "slack"]
AGENT_MAP = {
    f"{_LP}.github-ingest":     "github",
    f"{_LP}.jira-ingest":       "jira",
    f"{_LP}.confluence-ingest": "confluence",
    f"{_LP}.slack-ingest":      "slack",
}
SRC_COLOR = {"github": "\033[36m", "jira": "\033[34m", "confluence": "\033[35m", "slack": "\033[33m"}

SLACK_CURSORS  = ROOT / "state/slack_cursors.json"
SLACK_CFG      = ROOT / "config/slack_channels.yaml"
SLACK_VALIDATE = ROOT / "state/last_slack_validate.json"
SLACK_DISCOVER = ROOT / "state/last_slack_discover.json"
TB_VALIDATE    = ROOT / "state/last_topic_brief_validate.json"

# Per-source validate cache paths (mirror SLACK_VALIDATE convention).
# Each cache is written by ingest/run-<src>.sh after every ingest. Schema:
#   {computed_at, source, n_total_events, n_actors_mapped,
#    n_actors_raw_known, n_actors_raw_unknown, raw_unknown_top,
#    findings: [[sev, check, msg], ...]}
SRC_VALIDATE = {
    "github":     ROOT / "state/last_github_validate.json",
    "jira":       ROOT / "state/last_jira_validate.json",
    "confluence": ROOT / "state/last_confluence_validate.json",
}

HOUSE_LOG         = ROOT / "logs/housekeeping.log"
HOUSE_PLIST_LABEL = f"{_LP}.housekeeping"

IDENTITY_STATE = ROOT / "state/last_identity_reconcile.json"

EMBED_VALIDATE = ROOT / "state/last_embedding_validate.json"

# ── ANSI ─────────────────────────────────────────────────────────────────────
ESC    = "\033["
RESET  = ESC + "0m"
DIM    = ESC + "2m"
BOLD   = ESC + "1m"
GREEN  = ESC + "32m"
YELLOW = ESC + "33m"
RED    = ESC + "31m"
CYAN   = ESC + "36m"

W = 62  # content width


# ── helpers ───────────────────────────────────────────────────────────────────

def bar(n: int, total: int, width: int = 22) -> str:
    filled = int(width * n / total) if total else 0
    return f"{GREEN}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET}"


def rel_time(ts_str: str) -> str:
    """'2h ago', '3d ago' from a log timestamp (IST)."""
    try:
        dt = datetime.strptime(ts_str.split(",")[0].strip(), "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=IST)
        secs = int((datetime.now(IST) - dt).total_seconds())
        if secs < 90:    return "just now"
        if secs < 3600:  return f"{secs // 60}m ago"
        if secs < 86400: return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return ""


def cursor_age(ts_str: str) -> str:
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 3600:  return f"{secs // 60}m"
        if secs < 86400: return f"{secs // 3600}h"
        return f"{secs // 86400}d"
    except Exception:
        return "?"


def cursor_to_ist(ts: str) -> str:
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M")


def ts_label(ts_str: str, today: str) -> str:
    """HH:MM if today, else MM-DD HH:MM."""
    try:
        dt = datetime.strptime(ts_str.split(",")[0].strip(), "%Y-%m-%d %H:%M:%S")
        if ts_str.startswith(today):
            return dt.strftime("%H:%M")
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return ts_str[:16]


def detect_source(line: str) -> str:
    ll = line.lower()
    return next((s for s in SOURCES if s in ll), "unknown")


# Per-source identifying markers — used to attribute interior log lines when
# multiple ingests run concurrently (LaunchAgents all fire at :00, lines
# interleave). Each marker is unique to that source's log surface.
_SOURCE_MARKERS = {
    "github":     ("fetching prs", "fetching commits", "fetching commit diff",
                   f"repo {github_org()}", "github ingest", "repo ", "github_token",
                   "/repos/"),
    "jira":       ("jira project=", "jira ingest",
                   f"project {(jira_project_keys() or ['ex'])[0].lower()}",
                   "jira_ingest", "for jira"),
    "confluence": ("confluence", "fetching pages", "fetching footer",
                   "fetching inline", "comments ingest"),
    "slack":      ("slack ingest", "channels=", "channel=", "thread_started",
                   "thread_reply"),
}


def attribute_line(line: str) -> str | None:
    """Return source name if line matches any source-specific marker."""
    ll = line.lower()
    for src, markers in _SOURCE_MARKERS.items():
        for m in markers:
            if m in ll:
                return src
    return None


def plist_schedule(label: str) -> tuple[str, list[int]]:
    p = PLIST_DIR / f"{label}.plist"
    if not p.exists():
        p = ROOT / "launchagents" / f"{label}.plist"
    if not p.exists():
        return "(plist not found)", []
    with p.open("rb") as f:
        data = plistlib.load(f)
    sci = data.get("StartCalendarInterval")
    if not sci:
        return "(no schedule)", []
    if isinstance(sci, dict):
        sci = [sci]
    minutes = sorted({e["Minute"] for e in sci if "Minute" in e})
    hours   = sorted({e["Hour"]   for e in sci if "Hour"   in e})
    min_desc = "+".join(f":{m:02d}" for m in minutes)
    if hours and hours == list(range(min(hours), max(hours) + 1)):
        hour_desc = f"{min(hours):02d}h–{max(hours):02d}h IST"
    elif hours:
        hour_desc = ",".join(f"{h:02d}h" for h in hours) + " IST"
    else:
        hour_desc = "daily"
    fire_mins = [h * 60 + m for h in hours for m in minutes]
    return f"at {min_desc} · {hour_desc}", fire_mins


_WD_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]  # launchd order


def read_plist_weekly(label: str) -> tuple[str, str]:
    """For a weekly/multi-day LaunchAgent, return (sched_label, next_fire_label).

    Reads StartCalendarInterval (dict or array of dicts with Weekday/Hour/Minute).
    Returns e.g. ("Wed+Fri 13:00", "~1h 5m").
    """
    p = PLIST_DIR / f"{label}.plist"
    if not p.exists():
        p = ROOT / "launchagents" / f"{label}.plist"
    if not p.exists():
        return "(plist not found)", "?"
    try:
        with p.open("rb") as f:
            data = plistlib.load(f)
    except Exception:
        return "(plist unreadable)", "?"
    sci = data.get("StartCalendarInterval")
    if not sci:
        return "(no schedule)", "?"
    if isinstance(sci, dict):
        sci = [sci]

    entries = []  # (launchd_wd, hour, minute)
    for e in sci:
        entries.append((e.get("Weekday"), e.get("Hour", 0), e.get("Minute", 0)))

    # Human label: group by time when all same.
    times = {(h, m) for _, h, m in entries}
    days = [_WD_NAMES[wd] for wd, _, _ in entries if isinstance(wd, int) and 0 <= wd <= 6]
    if len(times) == 1:
        h, m = next(iter(times))
        sched_label = f"{'+'.join(days)} {h:02d}:{m:02d}"
    else:
        sched_label = " · ".join(
            f"{_WD_NAMES[wd]} {h:02d}:{m:02d}"
            for wd, h, m in entries if isinstance(wd, int) and 0 <= wd <= 6)

    # Next fire: min upcoming datetime across entries.
    now = datetime.now(IST)
    best = None
    for wd, h, m in entries:
        if not isinstance(wd, int):
            continue
        py_target = (wd - 1) % 7  # launchd Sun=0 → python Sun=6
        days_ahead = (py_target - now.weekday()) % 7
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=days_ahead)
        if cand <= now:
            cand += timedelta(days=7)
        if best is None or cand < best:
            best = cand
    if best is None:
        return sched_label, "?"
    delta = best - now
    total_min = int(delta.total_seconds()) // 60
    d, rem = divmod(total_min, 1440)
    h2, mn = divmod(rem, 60)
    if d:
        nxt = f"~{d}d {h2}h"
    elif h2:
        nxt = f"~{h2}h {mn}m"
    else:
        nxt = f"~{mn}m"
    return sched_label, nxt


def next_fire_label(fire_minutes: list[int]) -> str:
    if not fire_minutes:
        return "?"
    now = datetime.now(IST)
    now_min = now.hour * 60 + now.minute
    upcoming = [m for m in fire_minutes if m > now_min]
    target = upcoming[0] if upcoming else fire_minutes[0] + 1440
    delta = target - now_min
    if delta < 0:
        delta += 1440
    if delta < 60:
        return f"~{delta}m"
    return f"~{delta // 60}h {delta % 60}m"


def parse_runs() -> tuple[list[dict], str | None, str | None, dict[str, str]]:
    """Per-source start/done pairing that survives concurrent ingests.

    Returns (runs, last_start_line, last_done_line, inflight_starts) where
    inflight_starts maps src -> start_ts for runs with no matching Done. yet.

    Multiple LaunchAgents fire at :00; their log lines interleave when sorted
    by timestamp. We track an open `start` per source, and attribute each
    interior line + Done. to whichever source's marker matched most recently.
    """
    log_files = [p for p in (ROOT / "logs").glob("*.log")
                 if p.name != "rollup.log" and p.exists()]
    all_lines: list[str] = []
    for lf in log_files:
        all_lines.extend(lf.read_text().splitlines())
    all_lines.sort()

    open_starts: dict[str, str] = {}
    open_warns:  dict[str, list[str]] = {}
    last_attr:   str | None = None
    runs: list[dict] = []

    for line in all_lines:
        if "ingest starting" in line:
            src = detect_source(line)
            if src == "unknown":
                continue
            open_starts[src] = line.split(",")[0].strip()
            open_warns[src] = []
            last_attr = src
            continue

        # Interior line — try to attribute via source-specific marker
        hint = attribute_line(line)
        if hint and hint in open_starts:
            last_attr = hint

        if "WARNING" in line:
            tgt = last_attr if last_attr in open_starts else None
            if tgt:
                open_warns[tgt].append(line)

        if "Done." in line:
            # Prefer the explicit source tag (Done. source=jira …) — deterministic
            # even when a long slack run interleaves sub-second lines around a
            # fast source's completion. Fall back to interleave heuristic for
            # legacy untagged lines still in the log.
            m_src = re.search(r"\bsource=(\w+)", line)
            src = m_src.group(1) if m_src and m_src.group(1) in open_starts else None
            if not src:
                src = last_attr if last_attr in open_starts else None
            if not src and open_starts:
                src = next(iter(open_starts))
            if not src:
                continue
            m_new = re.search(r"total_new=(\d+)", line)
            m_dup = re.search(r"total_dup=(\d+)", line)
            runs.append({
                "source":   src,
                "start":    open_starts.pop(src, ""),
                "done":     line.split(",")[0].strip(),
                "new":      int(m_new.group(1)) if m_new else 0,
                "dup":      int(m_dup.group(1)) if m_dup else 0,
                "warnings": open_warns.pop(src, []),
            })
            # Reset attribution so next Done. doesn't re-claim same src.
            last_attr = None

    last_start = next((l for l in reversed(all_lines) if "ingest starting" in l), None)
    last_done  = next((l for l in reversed(all_lines) if "Done." in l), None)
    # Sources whose most-recent start never got a matching Done. = in-flight
    # (running now, or crashed without a Done sentinel). {src: start_ts}.
    inflight = dict(open_starts)
    return runs, last_start, last_done, inflight


def read_marker(source: str) -> str | None:
    p = STATE_DIR / f"last_{source}_success.date"
    return p.read_text().strip() if p.exists() else None


def db_stats() -> dict:
    if not DB_PATH.exists():
        return {}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute("SELECT source, COUNT(*) FROM events GROUP BY source")
        by_source = dict(cur.fetchall())
        cur.execute(
            "SELECT source, event_type, COUNT(*) n FROM events "
            "GROUP BY source, event_type ORDER BY source, n DESC"
        )
        by_type: dict[str, list] = {}
        for src, et, cnt in cur.fetchall():
            by_type.setdefault(src, []).append((et, cnt))
        since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur.execute(
            "SELECT source, event_type, COUNT(*) n FROM events WHERE ts >= ? "
            "GROUP BY source, event_type ORDER BY source, n DESC",
            (since_24h,),
        )
        recent: dict[str, list] = {}
        for src, et, cnt in cur.fetchall():
            recent.setdefault(src, []).append((et, cnt))
        # subject_summary cache breakdown (claude vs fallback).
        # Fallback rows = subjects awaiting /rollup chat classification.
        cur.execute("SELECT source, COUNT(*) FROM subject_summary GROUP BY source")
        cls_breakdown = dict(cur.fetchall())
        conn.close()
        return {
            "by_source": by_source,
            "by_type": by_type,
            "recent_24h": recent,
            "classification": cls_breakdown,
        }
    except Exception as e:
        return {"error": str(e)}


# ── render helpers ────────────────────────────────────────────────────────────

def rule() -> str:
    return f"  {DIM}{'─' * W}{RESET}"


def pill(sym: str, text: str, color: str) -> str:
    return f"{color}{BOLD}{sym}{RESET} {color}{text}{RESET}"


def kv(key: str, val: str, key_w: int = 9) -> str:
    return f"  {'':4}{DIM}{key:<{key_w}}{RESET}  {val}"


def section_header(title: str) -> None:
    """Bordered header used by drill-down subcommands."""
    print()
    pad = max(0, W + 2 - len(title) - 2)
    lp = pad // 2
    rp = pad - lp
    print(f"  {BOLD}{CYAN}╔{'═' * lp} {title} {'═' * rp}╗{RESET}")
    print(f"  {BOLD}{CYAN}╚{'═' * (W + 2)}╝{RESET}")
    print()


# ─── drill-down dispatch ──────────────────────────────────────────────────────
import sys

DRILL_DOWNS = {"slack", "identity", "housekeeping", "embedding", "pipeline",
               "github", "jira", "confluence", "rollup", "html", "discover"}


def cmd_discover() -> None:
    """Full discovered-channel list from state/last_slack_discover.json."""
    section_header("SLACK DISCOVER · proposed channels")
    if not SLACK_DISCOVER.exists():
        print(f"  {DIM}no discover cache yet{RESET}")
        print()
        return
    try:
        d = json.loads(SLACK_DISCOVER.read_text())
    except Exception as e:
        print(f"  {RED}parse error: {e}{RESET}")
        print()
        return

    sched, nxt = read_plist_weekly(f"{_LP}.slack-discover")
    gen = d.get("generated_at", "?")
    print(f"  {DIM}generated {gen} · window {d.get('days','?')}d · "
          f"schedule {sched} IST · next {nxt}{RESET}")
    print()

    def _table(title: str, rows: list, color: str) -> None:
        if not rows:
            return
        print(f"  {color}{BOLD}{title}{RESET}  ({len(rows)})")
        print(f"  {DIM}{'NAME':<34} {'KIND':<8} {'TEAM':<5} "
              f"{'T-MSG':<7} {'ALL-MSG':<8} {'MODE'}{RESET}")
        # Sort by team_msgs desc.
        for c in sorted(rows, key=lambda x: x.get("team_msgs", 0), reverse=True):
            print(f"  {c.get('name','?'):<34} {c.get('kind','?'):<8} "
                  f"{c.get('team_members',0):<5} {c.get('team_msgs',0):<7} "
                  f"{c.get('total_msgs',0):<8} {DIM}{c.get('mode','')}{RESET}")
        print()

    _table("AUTO-FULL (ready to apply)", d.get("auto_full") or [], GREEN)
    _table("AUTO-TEAM-INVOLVED (ready to apply)", d.get("auto_team_involved") or [], GREEN)
    _table("NEEDS REVIEW", d.get("needs_review") or [], YELLOW)
    skipped = d.get("skipped") or []
    if skipped:
        print(f"  {DIM}skipped: {len(skipped)} (DM/archived/already-tracked){RESET}")
        print()


def cmd_slack() -> None:
    """Per-channel slack detail: cursor age, event count, last activity, validate state."""
    import yaml as _yaml
    section_header("SLACK · per-channel detail")

    cursors: dict = {}
    if SLACK_CURSORS.exists():
        try:
            cursors = json.loads(SLACK_CURSORS.read_text())
        except Exception:
            pass

    meta: dict = {}
    meta_path = ROOT / "state/slack_channel_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text()).get("channels", {})
        except Exception:
            pass

    # Per-channel event counts + last activity from DB.
    counts: dict[str, tuple[int, str]] = {}
    if DB_PATH.exists():
        try:
            con = sqlite3.connect(str(DB_PATH))
            for cid, n, last_ts in con.execute(
                "SELECT channel_id, COUNT(*), MAX(ts) FROM events "
                "WHERE source='slack' AND channel_id IS NOT NULL GROUP BY channel_id"
            ).fetchall():
                counts[cid] = (n, last_ts or "")
            con.close()
        except Exception:
            pass

    # Config channels (yaml).
    cfg_channels: list[dict] = []
    if SLACK_CFG.exists():
        try:
            cfg = _yaml.safe_load(SLACK_CFG.read_text()) or {}
            cfg_channels = cfg.get("channels", []) or []
        except Exception:
            pass

    # Per-channel last-polled timestamps (lag = now − checked).
    checked: dict = {}
    checked_path = ROOT / "state/slack_channel_checked.json"
    if checked_path.exists():
        try:
            checked = json.loads(checked_path.read_text())
        except Exception:
            pass

    def _age_secs(ts: str) -> int | None:
        if not ts:
            return None
        try:
            if "T" in ts:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            return int((datetime.now(timezone.utc) - dt).total_seconds())
        except Exception:
            return None

    def _fmt_age(secs: int | None) -> str:
        if secs is None:
            return "—"
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            return f"{secs // 3600}h"
        return f"{secs // 86400}d"

    # Up-to-date threshold: 2 fire intervals (slack fires every 30min) + slack.
    FRESH_S = 45 * 60

    def _status(cid: str) -> str:
        if cid not in cursors:
            return f"{YELLOW}no cursor{RESET}"
        csecs = _age_secs(checked.get(cid))
        if csecs is None:
            return f"{DIM}? unpolled{RESET}"
        if csecs <= FRESH_S:
            return f"{GREEN}✓ up-to-date{RESET}"
        return f"{YELLOW}⚠ lag {_fmt_age(csecs)}{RESET}"

    print(f"  {BOLD}{'NAME':<34} {'EVENTS':<7} {'LAST MSG':<9} "
          f"{'CHECKED':<8} {'STATUS'}{RESET}")
    print(f"  {DIM}{'─' * (W + 8)}{RESET}")

    cfg_by_id = {c.get("id"): c for c in cfg_channels if c.get("id")}
    all_ids = sorted(set(cursors) | set(cfg_by_id) | set(counts))
    sorted_ids = sorted(all_ids,
                        key=lambda i: counts.get(i, (0, ""))[0],
                        reverse=True)

    for cid in sorted_ids:
        c = cfg_by_id.get(cid, {})
        m = meta.get(cid, {})
        name = (c.get("name") or m.get("name") or "?")[:33]
        n, last_ts = counts.get(cid, (0, ""))
        last_age = _fmt_age(_age_secs(last_ts)) if last_ts else "—"
        check_age = _fmt_age(_age_secs(checked.get(cid)))
        print(f"  {name:<34} {n:<7,} {last_age:<9} "
              f"{check_age:<8} {_status(cid)}")

    print()
    print(f"  {DIM}STATUS: ✓ up-to-date = polled within 45m (cursor age is true "
          f"quiet-time, not lag).{RESET}")
    print(f"  {DIM}        ⚠ lag X = last successful poll X ago — data may be "
          f"X behind Slack.{RESET}")

    # Validate state.
    print()
    print(f"  {BOLD}VALIDATE{RESET}")
    if SLACK_VALIDATE.exists():
        try:
            v = json.loads(SLACK_VALIDATE.read_text())
            findings = v.get("findings") or []
            for sev, check, msg in findings[:20]:
                col = GREEN if sev == "PASS" else (YELLOW if sev == "WARN" else RED)
                print(f"  {col}{sev:4s}{RESET}  {check:24s}  {DIM}{msg[:100]}{RESET}")
            if not findings:
                print(f"  {GREEN}✓ clean (no findings){RESET}")
        except Exception as e:
            print(f"  {RED}validate parse error: {e}{RESET}")
    else:
        print(f"  {DIM}no validate cache{RESET}")

    # Discover state.
    print()
    print(f"  {BOLD}DISCOVER{RESET}")
    if SLACK_DISCOVER.exists():
        try:
            d = json.loads(SLACK_DISCOVER.read_text())
            ready = d.get("ready") or []
            review = d.get("needs_review") or []
            print(f"  ready:        {len(ready)}")
            print(f"  needs_review: {len(review)}")
            for c in (review[:8] if isinstance(review, list) else []):
                if isinstance(c, dict):
                    print(f"    {DIM}{c.get('name','?')}  {c.get('reason','')[:60]}{RESET}")
        except Exception:
            pass
    else:
        print(f"  {DIM}no discover state{RESET}")

    print()


def _html_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


_HTML_CSS = """
:root { color-scheme: dark; }
body { font: 13px ui-monospace,SFMono-Regular,Menlo,monospace; background:#0b0f14; color:#d5d9e0;
       max-width: 1080px; margin: 24px auto; padding: 0 16px; }
h1 { font-size: 18px; letter-spacing: 1px; margin: 0 0 16px; }
.muted { color:#6e7681; }
.lane { margin: 12px 0; padding: 12px 14px; background:#11161d; border-left: 3px solid #4d8eff;
         border-radius: 4px; }
.lane h2 { font-size: 14px; margin: 0 0 6px; display:flex; align-items:center; gap:8px; }
.pill { font-size: 11px; padding: 1px 6px; border-radius: 8px; }
.pill.ok   { background:#193b25; color:#48d597; }
.pill.warn { background:#3b2e19; color:#d5b248; }
.pill.fail { background:#3b1919; color:#d54848; }
.lane[data-state="ok"]   { border-color: #48d597; }
.lane[data-state="warn"] { border-color: #d5b248; }
.lane[data-state="fail"] { border-color: #d54848; }
.kv { display:grid; grid-template-columns: 110px 1fr; gap: 2px 12px; margin: 4px 0;
       font-size: 12px; color:#a7afba; }
.kv b { color:#d5d9e0; font-weight:normal; }
details { margin-top: 8px; padding: 4px 0; }
details summary { cursor: pointer; color:#7a8497; font-size: 11px;
                   user-select:none; padding: 4px 0; }
details[open] summary { color:#d5d9e0; }
table { border-collapse: collapse; margin: 6px 0; font-size: 12px; width: 100%; }
th, td { padding: 3px 8px; text-align: left; }
th { color:#6e7681; font-weight:normal; border-bottom:1px solid #2a313b; }
tr:nth-child(even) td { background:#0e1319; }
.bar { display:inline-block; height:8px; background:#48d597; border-radius:2px; vertical-align:middle; }
.bar-track { display:inline-block; width:160px; background:#1a212b; border-radius:2px; vertical-align:middle; }
"""


def cmd_html() -> None:
    """Generate single-file HTML report w/ <details> collapsible sections.

    Output path: positional arg #2 (or /tmp/cron-status.html by default).
    """
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/cron-status.html")

    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")

    # Reuse existing helpers/data.
    runs_list, _, _, _ = parse_runs()
    last_per: dict[str, dict] = {r["source"]: r for r in runs_list}
    cursors_raw: dict = {}
    if CURSORS.exists():
        try:
            cursors_raw = json.loads(CURSORS.read_text())
        except Exception:
            pass
    sched_info = {src: plist_schedule(label) for label, src in AGENT_MAP.items()}
    stats = db_stats()
    db_by_src = stats.get("by_source", {})
    db_by_type = stats.get("by_type", {})
    db_recent = stats.get("recent_24h", {})

    out: list[str] = []
    out.append("<!doctype html><html><head><meta charset='utf-8'>")
    out.append(f"<title>Ingest Status · {today_str}</title>")
    out.append(f"<style>{_HTML_CSS}</style></head><body>")
    out.append(f"<h1>INGEST STATUS · {_html_escape(now_ist.strftime('%a %d %b %Y · %H:%M IST'))}</h1>")

    def pill_html(text: str, level: str = "ok") -> str:
        return f'<span class="pill {level}">{_html_escape(text)}</span>'

    # ── per-cron-source lanes ────────────────────────────────────────────────
    for src in SOURCES:
        if src == "slack":
            continue  # custom block
        marker = read_marker(src)
        last = last_per.get(src)
        cur_ts = cursors_raw.get(src)
        if marker == today_str:
            state = "ok"; pill = pill_html("ran today")
        elif marker:
            days = (datetime.strptime(today_str, "%Y-%m-%d")
                    - datetime.strptime(marker, "%Y-%m-%d")).days
            state = "warn"; pill = pill_html(f"last success {days}d ago", "warn")
        else:
            state = "fail"; pill = pill_html("never succeeded", "fail")
        out.append(f'<div class="lane" data-state="{state}">')
        out.append(f'<h2>{src.upper()} {pill}</h2>')
        sched_label, _fm = sched_info.get(src, ("?", []))
        out.append('<div class="kv">')
        out.append(f'<span>schedule</span><b>{_html_escape(sched_label)}</b>')
        if last:
            out.append(f'<span>last run</span><b>{_html_escape(last["done"][:16])} '
                       f'<span class="muted">+{last["new"]} new · {last["dup"]} dup</span></b>')
            _ov = rh.overrun_verdict(
                rh.run_duration_min(last.get("start", ""), last.get("done", "")),
                rh.fire_interval_min(_fm))
            if _ov:
                out.append(f'<span>runtime</span>'
                           f'<b>{pill_html(_ov["label"], _ov["level"])}</b>')
        if cur_ts:
            out.append(f'<span>cursor</span><b>{_html_escape(cursor_to_ist(cur_ts))} IST '
                       f'<span class="muted">({cursor_age(cur_ts)} old)</span></b>')
        s_tot = db_by_src.get(src, 0)
        out.append(f'<span>db total</span><b>{s_tot:,} events</b>')
        out.append('</div>')

        # Detail: per-type breakdown + 24h activity.
        out.append('<details><summary>event-type breakdown · 24h activity</summary>')
        out.append('<table><tr><th>event_type</th><th>count</th><th></th><th>24h</th></tr>')
        s_types = dict(db_by_type.get(src, []))
        s_rec = dict(db_recent.get(src, []))
        max_n = max(s_types.values(), default=1)
        for et, n in sorted(s_types.items(), key=lambda x: -x[1]):
            bar_w = int(160 * n / max_n)
            out.append(f'<tr><td>{_html_escape(et)}</td><td>{n:,}</td>'
                       f'<td><span class="bar-track"><span class="bar" style="width:{bar_w}px"></span></span></td>'
                       f'<td>{s_rec.get(et, 0):,}</td></tr>')
        out.append('</table>')
        out.append('</details>')
        out.append('</div>')

    # ── SLACK lane ───────────────────────────────────────────────────────────
    slack_cursors_d: dict = {}
    if SLACK_CURSORS.exists():
        try:
            slack_cursors_d = json.loads(SLACK_CURSORS.read_text())
        except Exception:
            pass
    meta_path = ROOT / "state/slack_channel_meta.json"
    meta_chans: dict = {}
    if meta_path.exists():
        try:
            meta_chans = json.loads(meta_path.read_text()).get("channels", {})
        except Exception:
            pass
    counts: dict[str, tuple[int, str]] = {}
    if DB_PATH.exists():
        try:
            con = sqlite3.connect(str(DB_PATH))
            for cid, n, lt in con.execute(
                "SELECT channel_id, COUNT(*), MAX(ts) FROM events "
                "WHERE source='slack' AND channel_id IS NOT NULL GROUP BY channel_id"
            ).fetchall():
                counts[cid] = (n, lt or "")
            con.close()
        except Exception:
            pass

    out.append('<div class="lane" data-state="ok">')
    out.append('<h2>SLACK ' + pill_html("ran today") + '</h2>')
    out.append(f'<div class="kv"><span>channels</span><b>{len(slack_cursors_d)} active cursor(s)</b>'
               f'<span>db total</span><b>{db_by_src.get("slack",0):,} events</b></div>')
    out.append('<details open><summary>per-channel detail</summary>')
    out.append('<table><tr><th>id</th><th>name</th><th>private</th>'
               '<th>cursor age</th><th>events</th><th>last activity</th></tr>')
    all_ids = sorted(set(slack_cursors_d) | set(counts),
                     key=lambda i: counts.get(i, (0, ""))[0], reverse=True)
    for cid in all_ids:
        m = meta_chans.get(cid, {})
        name = m.get("name", "?")
        priv = "yes" if m.get("is_private") else "no"
        cur = slack_cursors_d.get(cid)
        if cur:
            try:
                dt_ = datetime.fromtimestamp(float(cur), tz=timezone.utc)
                secs = int((datetime.now(timezone.utc) - dt_).total_seconds())
                cur_age = f"{secs // 3600}h" if secs < 86400 else f"{secs // 86400}d"
            except Exception:
                cur_age = "?"
        else:
            cur_age = "—"
        n, last_ts = counts.get(cid, (0, ""))
        out.append(f'<tr><td><span class="muted">{_html_escape(cid)}</span></td>'
                   f'<td>{_html_escape(name)}</td><td>{priv}</td>'
                   f'<td>{cur_age}</td><td>{n:,}</td>'
                   f'<td><span class="muted">{_html_escape(last_ts[:19])}</span></td></tr>')
    out.append('</table></details>')
    out.append('</div>')

    # ── IDENTITY lane ────────────────────────────────────────────────────────
    if IDENTITY_STATE.exists():
        try:
            snap = json.loads(IDENTITY_STATE.read_text())
        except Exception:
            snap = {}
        out.append('<div class="lane" data-state="ok">')
        out.append('<h2>IDENTITY ' + pill_html("self-heal") + '</h2>')
        cov = snap.get("coverage") or {}
        total = snap.get("total_entries", 1) or 1
        cov_str = " · ".join(f"{k}={round(100*v/total)}%" for k, v in cov.items())
        by_scope = snap.get("by_scope") or {}
        out.append(f'<div class="kv">'
                   f'<span>scope</span><b>team={by_scope.get("team",0)} '
                   f'org={by_scope.get("org",0)} external={by_scope.get("external",0)}</b>'
                   f'<span>signals</span><b>{snap.get("signals_total",0):,} pairs</b>'
                   f'<span>coverage</span><b>{cov_str}</b>'
                   f'<span>last run</span><b>+{snap.get("n_changes",0)} fills · '
                   f'{snap.get("n_orphans",0)} orphans</b></div>')

        out.append('<details><summary>signals by source + pair types</summary>')
        if DB_PATH.exists():
            try:
                con = sqlite3.connect(str(DB_PATH))
                has = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='identity_signals'"
                ).fetchone()
                if has:
                    out.append('<table><tr><th>source</th><th>pairs</th><th>observations</th></tr>')
                    for src, n, obs in con.execute(
                        "SELECT source, COUNT(*), SUM(n_obs) FROM identity_signals "
                        "GROUP BY source ORDER BY 2 DESC"
                    ).fetchall():
                        out.append(f'<tr><td>{_html_escape(src)}</td><td>{n:,}</td><td>{obs:,}</td></tr>')
                    out.append('</table>')
                    out.append('<table><tr><th>pair type</th><th>count</th></tr>')
                    for pair, n in con.execute(
                        "SELECT key_a_type||' ↔ '||key_b_type, COUNT(*) FROM identity_signals "
                        "GROUP BY 1 ORDER BY 2 DESC LIMIT 15"
                    ).fetchall():
                        out.append(f'<tr><td>{_html_escape(pair)}</td><td>{n:,}</td></tr>')
                    out.append('</table>')
                con.close()
            except Exception:
                pass
        out.append('</details>')
        # Recent fills.
        rlog = ROOT / "logs/identity_reconcile.log"
        if rlog.exists():
            out.append('<details><summary>recent fills (tail of identity_reconcile.log)</summary>')
            out.append('<table><tr><th>line</th></tr>')
            for ln in rlog.read_text().splitlines()[-25:]:
                if ln.strip():
                    out.append(f'<tr><td><span class="muted">{_html_escape(ln[:140])}</span></td></tr>')
            out.append('</table></details>')
        out.append('</div>')

    # ── EMBEDDING lane ───────────────────────────────────────────────────────
    if DB_PATH.exists():
        con = sqlite3.connect(str(DB_PATH))
        has_emb = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='embedding'"
        ).fetchone()
        if has_emb:
            total_emb = con.execute("SELECT COUNT(*) FROM embedding").fetchone()[0]
            newest = con.execute("SELECT MAX(computed_at) FROM embedding").fetchone()[0] or "—"
            state = "ok"
            try:
                nd = datetime.strptime(newest, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - nd).days >= 2:
                    state = "warn"
            except Exception:
                pass
            out.append(f'<div class="lane" data-state="{state}">')
            out.append('<h2>EMBEDDING ' + pill_html("vectors", state) + '</h2>')
            out.append(f'<div class="kv"><span>total</span><b>{total_emb:,} vectors</b>'
                       f'<span>newest</span><b>{_html_escape(newest)}</b></div>')
            out.append('<details><summary>per-source coverage + gaps</summary>')
            out.append('<table><tr><th>src</th><th>embedded</th><th>subjects</th>'
                       '<th>coverage</th><th>gap</th></tr>')
            for src in SOURCES:
                n_emb = con.execute(
                    "SELECT COUNT(*) FROM embedding WHERE source=?", (src,)
                ).fetchone()[0]
                n_sub = con.execute(
                    "SELECT COUNT(DISTINCT subject) FROM events "
                    "WHERE source=? AND subject IS NOT NULL", (src,)
                ).fetchone()[0]
                gap = con.execute("""
                    SELECT COUNT(DISTINCT e.subject) FROM events e
                    LEFT JOIN embedding em ON em.subject=e.subject
                    WHERE em.subject IS NULL AND e.subject IS NOT NULL AND e.source=?
                """, (src,)).fetchone()[0]
                pct = f"{100*n_emb//n_sub}%" if n_sub else "—"
                out.append(f'<tr><td>{src}</td><td>{n_emb:,}</td><td>{n_sub:,}</td>'
                           f'<td>{pct}</td><td>{gap:,}</td></tr>')
            out.append('</table></details>')
            out.append('</div>')
        con.close()

    # ── HOUSEKEEPING lane ────────────────────────────────────────────────────
    if HOUSE_LOG.exists():
        out.append('<div class="lane" data-state="ok">')
        out.append('<h2>HOUSEKEEPING ' + pill_html("weekly") + '</h2>')
        text = HOUSE_LOG.read_text()
        blocks = [b for b in re.split(r"(?=^=== Housekeeping)", text, flags=re.MULTILINE) if b.strip()]
        out.append(f'<div class="kv"><span>runs logged</span><b>{len(blocks)}</b></div>')
        for blk in reversed(blocks[-4:]):
            head = blk.split("\n", 1)[0]
            sm = re.search(r"Files affected: (\d+)\s*\nBytes affected: (\S+)", blk)
            summary = f"{sm.group(1)} files · {sm.group(2)}" if sm else "?"
            out.append(f'<details><summary>{_html_escape(head)} — {summary}</summary>')
            out.append('<table><tr><th>action</th><th>category</th><th>size</th><th>path</th></tr>')
            for m in re.finditer(r"\[(DELETED|TRUNCD|DRY-RUN)\s*\]\s+(\S+)\s+(\S+)\s+(\S+)", blk):
                out.append(f'<tr><td>{m.group(1)}</td><td>{m.group(2)}</td>'
                           f'<td>{m.group(3)}</td>'
                           f'<td><span class="muted">{_html_escape(m.group(4))}</span></td></tr>')
            out.append('</table></details>')
        out.append('</div>')

    out.append('</body></html>')
    out_path.write_text("\n".join(out))
    print(f"  wrote {out_path}")
    print(f"  open: open {out_path}")


def cmd_housekeeping() -> None:
    """Per-run housekeeping breakdown from logs/housekeeping.log."""
    section_header("HOUSEKEEPING · per-run action log")
    if not HOUSE_LOG.exists():
        print(f"  {DIM}no housekeeping log yet{RESET}")
        print()
        return
    text = HOUSE_LOG.read_text()
    # Split into runs.
    blocks = re.split(r"(?=^=== Housekeeping)", text, flags=re.MULTILINE)
    blocks = [b.strip() for b in blocks if b.strip()]
    for blk in blocks[-4:]:
        # Header
        head = blk.split("\n", 1)[0]
        print(f"  {BOLD}{head}{RESET}")
        # Per-action counts.
        actions: dict[str, list[str]] = {}
        for m in re.finditer(
            r"\[(DELETED|TRUNCD|DRY-RUN)\s*\]\s+(\S+)\s+(\S+)\s+(\S+)", blk
        ):
            kind, cat, size, path = m.groups()
            actions.setdefault(cat, []).append(f"{size:<8} {path}")
        for cat, items in actions.items():
            print(f"    {CYAN}{cat}{RESET}  ({len(items)} files)")
            for it in items[:8]:
                print(f"      {DIM}{it}{RESET}")
            if len(items) > 8:
                print(f"      {DIM}…+{len(items) - 8} more{RESET}")
        # Summary footer.
        sm = re.search(r"Files affected: (\d+)\s*\nBytes affected: (\S+)", blk)
        if sm:
            print(f"    {GREEN}total{RESET}  files={sm.group(1)}  bytes={sm.group(2)}")
        print()


def cmd_embedding() -> None:
    """Embedding coverage gaps + per-source oldest computed_at."""
    section_header("EMBEDDING · coverage detail")
    if not DB_PATH.exists():
        print(f"  {RED}db missing{RESET}")
        print()
        return
    con = sqlite3.connect(str(DB_PATH))
    if not con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='embedding'"
    ).fetchone():
        print(f"  {DIM}no embedding table{RESET}")
        print()
        return

    # Per-source breakdown vs total subjects.
    print(f"  {BOLD}COVERAGE BY SOURCE{RESET}")
    print(f"    {'src':<12} {'embedded':>10} {'subjects':>10} {'cov':>6}  {'oldest':<22} {'newest':<22}")
    for src in SOURCES:
        n_emb = con.execute(
            "SELECT COUNT(*) FROM embedding WHERE source=?", (src,)
        ).fetchone()[0]
        n_sub = con.execute(
            "SELECT COUNT(DISTINCT subject) FROM events WHERE source=? AND subject IS NOT NULL",
            (src,),
        ).fetchone()[0]
        oldest = con.execute(
            "SELECT MIN(computed_at) FROM embedding WHERE source=?", (src,)
        ).fetchone()[0] or "—"
        newest = con.execute(
            "SELECT MAX(computed_at) FROM embedding WHERE source=?", (src,)
        ).fetchone()[0] or "—"
        pct = f"{100 * n_emb // n_sub}%" if n_sub else "—"
        sc = SRC_COLOR.get(src, "")
        print(f"    {sc}{src:<12}{RESET} {n_emb:>10,} {n_sub:>10,} {pct:>6}  "
              f"{oldest:<22} {newest:<22}")

    # Coverage gap — subjects without embedding.
    gaps = con.execute("""
        SELECT e.source, COUNT(DISTINCT e.subject)
        FROM events e
        LEFT JOIN embedding em ON em.subject = e.subject
        WHERE em.subject IS NULL AND e.subject IS NOT NULL
        GROUP BY e.source ORDER BY 2 DESC
    """).fetchall()
    print()
    print(f"  {BOLD}GAP — subjects WITHOUT embedding (per source){RESET}")
    if gaps:
        for src, n in gaps:
            sc = SRC_COLOR.get(src, "")
            print(f"    {sc}{src:<12}{RESET}  {n:>6,} subjects need embedding")
    else:
        print(f"    {GREEN}no gaps — all subjects embedded{RESET}")

    # Model breakdown.
    print()
    print(f"  {BOLD}MODEL DISTRIBUTION{RESET}")
    for model, n in con.execute(
        "SELECT model, COUNT(*) FROM embedding GROUP BY model ORDER BY 2 DESC"
    ).fetchall():
        print(f"    {model:<30}  {n:,}")
    con.close()
    print()


def cmd_pipeline() -> None:
    """Cluster details from topic_brief + cluster_status tables (if present)."""
    section_header("PIPELINE · cluster detail")
    if not DB_PATH.exists():
        print(f"  {RED}db missing{RESET}")
        print()
        return
    con = sqlite3.connect(str(DB_PATH))
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    # Cluster status table presence.
    for tbl in ("topic_brief", "cluster_status"):
        if tbl not in tables:
            print(f"  {DIM}{tbl} not found{RESET}")
            continue
        cols = [r[1] for r in con.execute(f"pragma table_info({tbl})").fetchall()]
        print(f"  {BOLD}{tbl}{RESET}  {DIM}cols={','.join(cols[:8])}{RESET}")
        n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"    rows={n}")

    # Per-status counts.
    if "topic_brief" in tables:
        cols = [r[1] for r in con.execute("pragma table_info(topic_brief)").fetchall()]
        if "status" in cols:
            print()
            print(f"  {BOLD}CLUSTERS BY STATUS{RESET}")
            for s, n in con.execute(
                "SELECT status, COUNT(*) FROM topic_brief GROUP BY status ORDER BY 2 DESC"
            ).fetchall():
                col = GREEN if s == "active" else (YELLOW if s == "stale" else DIM)
                print(f"    {col}{s:<12}{RESET}  {n}")

        # Missing v2 fields.
        v2_cols = [c for c in cols if c.startswith("v2_") or c in ("v2_label", "v2_summary")]
        if v2_cols:
            print()
            print(f"  {BOLD}v2 FIELDS{RESET}")
            for c in v2_cols:
                missing = con.execute(
                    f"SELECT COUNT(*) FROM topic_brief WHERE {c} IS NULL OR {c} = ''"
                ).fetchone()[0]
                print(f"    {c:<30}  {missing:>6} missing")
    con.close()
    print()


def cmd_identity() -> None:
    """Identity reconcile detail: signals by source + recent fills + orphans."""
    section_header("IDENTITY · reconcile detail")

    # Last reconcile snapshot.
    if IDENTITY_STATE.exists():
        try:
            snap = json.loads(IDENTITY_STATE.read_text())
        except Exception:
            snap = {}
    else:
        snap = {}

    if snap:
        print(f"  {BOLD}LAST RECONCILE{RESET}  {DIM}({snap.get('computed_at','?')}){RESET}")
        print(f"    entries={snap.get('total_entries','?')}  "
              f"signals={snap.get('signals_total','?')}  "
              f"fills={snap.get('n_changes',0)}  orphans={snap.get('n_orphans',0)}")
        by_scope = snap.get("by_scope") or {}
        print(f"    scope: team={by_scope.get('team',0)}  "
              f"org={by_scope.get('org',0)}  external={by_scope.get('external',0)}")
        cov = snap.get("coverage") or {}
        total = snap.get("total_entries", 0) or 1
        print(f"    coverage: " + "  ".join(
            f"{k}={round(100*v/total)}%" for k, v in cov.items()))
        fill = snap.get("fill_breakdown") or {}
        if fill:
            print(f"    fill_breakdown: " + "  ".join(f"{k}:{v}" for k, v in sorted(fill.items())))

    # Signals by source.
    print()
    print(f"  {BOLD}SIGNALS BY SOURCE{RESET}")
    if DB_PATH.exists():
        try:
            con = sqlite3.connect(str(DB_PATH))
            has_tbl = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='identity_signals'"
            ).fetchone()
            if has_tbl:
                rows = con.execute(
                    "SELECT source, COUNT(*) n, SUM(n_obs) obs "
                    "FROM identity_signals GROUP BY source ORDER BY n DESC"
                ).fetchall()
                for src, n, obs in rows:
                    print(f"    {src:<12}  {n:>6,} pairs  {DIM}({obs:,} observations){RESET}")
                # Pair-type distribution.
                print()
                print(f"  {BOLD}PAIR TYPES{RESET}")
                rows = con.execute(
                    "SELECT key_a_type || ' ↔ ' || key_b_type AS pair, COUNT(*) n "
                    "FROM identity_signals GROUP BY pair ORDER BY n DESC LIMIT 15"
                ).fetchall()
                for pair, n in rows:
                    print(f"    {pair:<30}  {n:>6,}")
            con.close()
        except Exception as e:
            print(f"  {RED}db error: {e}{RESET}")

    # Recent reconcile activity from log.
    print()
    print(f"  {BOLD}RECENT RECONCILE RUNS{RESET}  {DIM}(tail of logs/identity_reconcile.log){RESET}")
    log = ROOT / "logs/identity_reconcile.log"
    if log.exists():
        lines = log.read_text().splitlines()
        for ln in lines[-30:]:
            if not ln.strip():
                continue
            if "fill[" in ln or "orphan" in ln:
                print(f"    {DIM}{ln[:120]}{RESET}")
            elif ln.startswith("signals="):
                print(f"    {ln[:120]}")
    else:
        print(f"    {DIM}no log yet{RESET}")
    print()


# Drill-down dispatch.
if len(sys.argv) > 1:
    arg = sys.argv[1].lstrip("-")
    if arg in ("h", "help"):
        print("usage: cron-status [<lane>]")
        print("  no arg                  → summary dashboard (default)")
        print("  cron-status slack       → per-channel slack detail")
        print("  cron-status identity    → reconcile fills + signal breakdown")
        print("  cron-status housekeeping→ per-run action log")
        print("  cron-status embedding   → coverage gaps + oldest per source")
        print("  cron-status pipeline    → topic_brief cluster details")
        print("  cron-status discover    → full proposed slack-channel list")
        print("  cron-status html [path] → write self-contained HTML report (default /tmp/cron-status.html)")
        sys.exit(0)
    if arg in DRILL_DOWNS:
        fn = globals().get(f"cmd_{arg}")
        if fn:
            fn()
            sys.exit(0)
        else:
            print(f"  {DIM}drill-down for '{arg}' not yet implemented — "
                  f"run with no args for summary{RESET}")
            sys.exit(0)


# ─── main ─────────────────────────────────────────────────────────────────────

now   = datetime.now(IST)
today = now.strftime("%Y-%m-%d")

runs, last_start_line, last_done_line, inflight_starts = parse_runs()
last_per_src: dict[str, dict] = {}
for r in runs:
    last_per_src[r["source"]] = r


def overrun_for(src: str, fire_mins: list[int]) -> dict | None:
    """Overrun verdict for a source: in-flight run if genuinely live, else last
    completed. In-flight is gated on pgrep so a mis-attributed open start (from
    concurrent-fire log interleaving) can't false-flag as 'running OVERRUN'."""
    interval = rh.fire_interval_min(fire_mins)
    if not interval:
        return None
    start_ts = inflight_starts.get(src)
    if start_ts and rh.source_running(src):
        return rh.overrun_verdict(rh.inflight_duration_min(start_ts),
                                  interval, in_flight=True)
    r = last_per_src.get(src)
    if r:
        return rh.overrun_verdict(rh.run_duration_min(r.get("start", ""),
                                                       r.get("done", "")), interval)
    return None

cursors_raw: dict = {}
if CURSORS.exists():
    try:
        cursors_raw = json.loads(CURSORS.read_text())
    except Exception:
        pass

running_src: str | None = None
if last_start_line and last_done_line:
    if last_start_line.split(",")[0] > last_done_line.split(",")[0]:
        running_src = detect_source(last_start_line)
elif last_start_line and not last_done_line:
    running_src = detect_source(last_start_line)

sched_info = {src: plist_schedule(label) for label, src in AGENT_MAP.items()}
stats      = db_stats()
db_by_src  = stats.get("by_source", {})
db_by_type = stats.get("by_type", {})
db_recent  = stats.get("recent_24h", {})
db_total   = sum(db_by_src.values())

# ─── header ──────────────────────────────────────────────────────────────────
print()
title   = f" INGEST STATUS · {now.strftime('%a %d %b %Y')} · {now.strftime('%H:%M IST')} "
pad     = max(0, W + 2 - len(title))
lpad    = pad // 2
rpad    = pad - lpad
print(f"  {BOLD}{CYAN}╔{'═' * lpad}{title}{'═' * rpad}╗{RESET}")
print(f"  {BOLD}{CYAN}╚{'═' * (W + 2)}╝{RESET}")
print()

# ─── per-source blocks ────────────────────────────────────────────────────────
for src in SOURCES:
    sc     = SRC_COLOR.get(src, "")
    sched_label, fire_mins = sched_info.get(src, ("(unknown)", []))
    marker = read_marker(src)
    last   = last_per_src.get(src)
    # slack uses per-channel cursors in state/slack_cursors.json — no single
    # cursor in cursors.json. Skip the cursor kv for slack (channels kv shown
    # in the custom slack block below).
    cur_ts = None if src == "slack" else cursors_raw.get(src)
    s_tot  = db_by_src.get(src, 0)
    s_types = db_by_type.get(src, [])
    s_rec  = db_recent.get(src, [])

    # Detect ingest-down state: marker stale AND last run logged a
    # "Cursor NOT updated" warning/error — distinguishes total auth/network
    # outage from a transient flake.
    ingest_down = False
    down_reason = ""
    if last and last.get("warnings"):
        for w in last["warnings"]:
            if "Cursor NOT updated" in w:
                ingest_down = True
                # Pluck a short reason from the warning line.
                m_msg = re.search(r"Cursor NOT updated[^\n]*", w)
                if m_msg:
                    down_reason = m_msg.group(0)[:90]
                break

    if running_src == src:
        status = pill("⚡", "running now", YELLOW)
    elif marker == today and not ingest_down:
        status = pill("●", "ran today", GREEN)
    elif ingest_down:
        # Marker may be 0 (fresh fail today) or stale; either way → FAIL.
        if marker:
            days = (datetime.strptime(today, "%Y-%m-%d")
                    - datetime.strptime(marker, "%Y-%m-%d")).days
            status = pill("✗", f"INGEST DOWN · last success {days}d ago", RED)
        else:
            status = pill("✗", "INGEST DOWN", RED)
    elif marker:
        days   = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(marker, "%Y-%m-%d")).days
        status = pill("◐", f"last success {days}d ago", YELLOW)
    else:
        status = pill("○", "never succeeded", RED)

    nxt = next_fire_label(fire_mins) if not running_src else "—"

    print(f"  {sc}{BOLD}{src.upper():<13}{RESET}  {status}  {DIM}next {nxt}{RESET}")
    print(kv("schedule", sched_label))
    policy_text = ("retry every fire · no daily gate · DM hard-skip"
                   if src == "slack"
                   else "retry every fire · 1 success/day then idle")
    print(kv("policy", policy_text))

    if last:
        t_lbl = ts_label(last["done"], today)
        age   = rel_time(last["done"])
        wnote = f"  {YELLOW}⚠ {len(last['warnings'])} warn{RESET}" if last["warnings"] else ""
        print(kv("last run", f"{BOLD}{t_lbl}{RESET}  {DIM}({age}){RESET}   {GREEN}+{last['new']} new{RESET}  {DIM}{last['dup']} dup{RESET}{wnote}"))
        for w in last.get("warnings", []):
            print(f"  {'':13}  {YELLOW}⚠ {DIM}{w.split('WARNING')[-1].strip()[:68]}{RESET}")
    else:
        print(kv("last run", f"{DIM}none recorded{RESET}"))

    # Overrun check: run duration vs fire interval. Flags a run that takes
    # (or is taking) longer than the gap to its next fire → collides/SIGTERM.
    ov = overrun_for(src, fire_mins)
    if ov:
        col = RED if ov["level"] == "fail" else YELLOW
        print(kv("runtime", f"{col}{'✗' if ov['level']=='fail' else '⚠'} {ov['label']}{RESET}"))

    if cur_ts:
        print(kv("cursor", f"{cursor_to_ist(cur_ts)} IST  {DIM}({cursor_age(cur_ts)} old){RESET}"))

    if s_tot:
        top = "  ".join(f"{et}:{n}" for et, n in s_types[:5])
        print(kv("db total", f"{s_tot:,} events  {DIM}{top}{RESET}"))

    if s_rec:
        rec_str = "  ".join(f"{et}:{n}" for et, n in s_rec[:5])
        print(kv("24h",     f"{GREEN}{rec_str}{RESET}"))

    # ── attribution validate cache (github/jira/confluence) ────────────────
    # Mirrors the slack validate render block. Slack has its own bigger
    # validate cache rendered after the source-loop. This renders the
    # smaller v1 attribution-only cache inline under each source.
    vp = SRC_VALIDATE.get(src)
    if vp and vp.exists():
        try:
            vrep = json.loads(vp.read_text())
            v_mtime = datetime.fromtimestamp(vp.stat().st_mtime, tz=IST)
            v_age_s = int((datetime.now(IST) - v_mtime).total_seconds())
            v_age = (f"{v_age_s // 60}m" if v_age_s < 3600
                     else f"{v_age_s // 3600}h" if v_age_s < 86400
                     else f"{v_age_s // 86400}d")
            n_fail = sum(1 for f in vrep.get("findings", []) if f[0] == "FAIL")
            n_warn = sum(1 for f in vrep.get("findings", []) if f[0] == "WARN")
            if n_fail == 0 and n_warn == 0:
                verdict = f"{GREEN}✓ clean{RESET}"
            else:
                parts = []
                if n_fail: parts.append(f"{RED}{n_fail} FAIL{RESET}")
                if n_warn: parts.append(f"{YELLOW}{n_warn} WARN{RESET}")
                verdict = "  ".join(parts)
            unmapped = vrep.get("n_actors_raw_unknown", 0)
            mapped = vrep.get("n_actors_mapped", 0)
            known = vrep.get("n_actors_raw_known", 0)
            print(kv("validate", f"{verdict}  {DIM}({v_age} old){RESET}   "
                                 f"{GREEN}{mapped:,} mapped{RESET}  "
                                 f"{DIM}{known:,} known-ext{RESET}  "
                                 f"{YELLOW if unmapped else DIM}{unmapped:,} unmapped{RESET}"))
            # One line per finding (FAIL first), capped at 3.
            findings = sorted(vrep.get("findings", []),
                              key=lambda x: (0 if x[0] == "FAIL" else 1))
            for sev, check, msg in findings[:3]:
                if sev == "PASS":
                    continue
                col = RED if sev == "FAIL" else YELLOW
                print(f"  {'':13}  {col}{sev}{RESET} {DIM}{check:<13}{RESET} {msg[:64]}")
            # Top unmapped names hint (no clutter when clean).
            top = vrep.get("raw_unknown_top", [])
            if top:
                top_str = ", ".join(f"{a}({n})" for a, n in top[:3])
                print(f"  {'':13}  {DIM}top unmapped: {top_str[:70]}{RESET}")
        except Exception as e:
            print(kv("validate", f"{YELLOW}cache parse error: {str(e)[:40]}{RESET}"))
    elif vp is not None:
        print(kv("validate", f"{DIM}no cache yet (writes after next ingest){RESET}"))

    # Slack extras block prints below; skip rule for slack so the two
    # sub-blocks flow as one section.
    if src != "slack":
        print(rule())
        print()

# ─── slack extras (channels + DM-skip + validate) ───────────────────────────
# Slack header/schedule/policy/last-run/db-total/24h handled by main loop.
# This block adds slack-specific kvs that don't fit the generic template.
slack_cursors: dict = {}
if SLACK_CURSORS.exists():
    try:
        slack_cursors = json.loads(SLACK_CURSORS.read_text())
    except Exception:
        pass

# Per-channel cursor age (laggiest channel).
if slack_cursors:
    n_ch = len(slack_cursors)
    try:
        oldest = min(float(v) for v in slack_cursors.values() if v)
        oldest_age_s = datetime.now(timezone.utc).timestamp() - oldest
        if oldest_age_s < 3600:    age_str = f"{int(oldest_age_s // 60)}m"
        elif oldest_age_s < 86400: age_str = f"{int(oldest_age_s // 3600)}h"
        else:                      age_str = f"{int(oldest_age_s // 86400)}d"
        print(kv("channels", f"{n_ch} cursor(s) · {DIM}laggiest {age_str} old{RESET}"))
    except (ValueError, TypeError):
        print(kv("channels", f"{n_ch} cursor(s)"))

# DM hard-skip invariant — surfaced for at-a-glance verification.
print(kv("DM skip", f"{GREEN}✓{RESET} {DIM}allow-list in config/slack_channels.yaml{RESET}"))

# ── validate cache (refreshed by run-slack.sh post-ingest) ───────────────
if SLACK_VALIDATE.exists():
    try:
        sv = json.loads(SLACK_VALIDATE.read_text())
        sv_mtime = datetime.fromtimestamp(SLACK_VALIDATE.stat().st_mtime, tz=IST)
        age_s = int((datetime.now(IST) - sv_mtime).total_seconds())
        if age_s < 3600:
            age_str = f"{age_s // 60}m"
        elif age_s < 86400:
            age_str = f"{age_s // 3600}h"
        else:
            age_str = f"{age_s // 86400}d"

        n_fail = sum(1 for c in sv.get("channels", []) for f in c.get("findings", []) if f[0] == "FAIL")
        n_warn = sum(1 for c in sv.get("channels", []) for f in c.get("findings", []) if f[0] == "WARN")
        n_fail += sum(1 for f in sv.get("global", []) if f.get("sev") == "FAIL")
        n_warn += sum(1 for f in sv.get("global", []) if f.get("sev") == "WARN")
        n_ch = len(sv.get("channels", []))

        if n_fail == 0 and n_warn == 0:
            verdict = f"{GREEN}✓ clean{RESET} ({n_ch} channels)"
        else:
            parts = []
            if n_fail: parts.append(f"{RED}{n_fail} FAIL{RESET}")
            if n_warn: parts.append(f"{YELLOW}{n_warn} WARN{RESET}")
            verdict = "  ".join(parts) + f"  {DIM}({n_ch} channels){RESET}"
        print(kv("validate", f"{verdict}  {DIM}({age_str} old){RESET}"))

        # One line per finding (FAIL first, then WARN), capped at 4.
        flat: list[tuple[str, str, str]] = []
        for c in sv.get("channels", []):
            for sev, check, msg in c.get("findings", []):
                flat.append((sev, c.get("channel", c.get("id", "?")), f"{check}: {msg}"))
        for g in sv.get("global", []):
            flat.append((g.get("sev", "?"), "global", f"{g.get('check', '?')}: {g.get('msg', '?')}"))
        flat.sort(key=lambda x: (0 if x[0] == "FAIL" else 1))
        for sev, where, msg in flat[:4]:
            col = RED if sev == "FAIL" else YELLOW
            print(f"  {'':13}  {col}{sev}{RESET} {DIM}{where:<28}{RESET} {msg[:60]}")
        if len(flat) > 4:
            print(f"  {'':13}  {DIM}…+{len(flat) - 4} more (run derive/slack_validate.py){RESET}")
    except Exception as e:
        print(kv("validate", f"{YELLOW}cache parse error: {str(e)[:40]}{RESET}"))
else:
    print(kv("validate", f"{DIM}no cache yet (writes after first cron fire){RESET}"))

# ── discover proposals (refreshed by run-slack-discover.sh LaunchAgent) ─────
# Surfaces auto_full + auto_team_involved counts ready for owner apply,
# plus the needs_review backlog. Schedule + next-fire read from the
# com.example.slack-discover plist; owner manually applies after review.
_disc_sched, _disc_next = read_plist_weekly(f"{_LP}.slack-discover")
if SLACK_DISCOVER.exists():
    try:
        sd = json.loads(SLACK_DISCOVER.read_text())
        sd_mtime = datetime.fromtimestamp(SLACK_DISCOVER.stat().st_mtime, tz=IST)
        sd_age_s = int((datetime.now(IST) - sd_mtime).total_seconds())
        if sd_age_s < 3600:
            sd_age = f"{sd_age_s // 60}m"
        elif sd_age_s < 86400:
            sd_age = f"{sd_age_s // 3600}h"
        else:
            sd_age = f"{sd_age_s // 86400}d"

        n_full   = len(sd.get("auto_full", []))
        n_ti     = len(sd.get("auto_team_involved", []))
        n_review = len(sd.get("needs_review", []))
        n_ready  = n_full + n_ti

        if n_ready > 0:
            ready_str = (f"{GREEN}{BOLD}{n_ready} ready{RESET} "
                         f"{DIM}({n_full} full + {n_ti} team_involved){RESET}")
        else:
            ready_str = f"{DIM}0 ready{RESET}"
        review_str = (f"{YELLOW}{n_review} needs_review{RESET}"
                      if n_review else f"{DIM}0 needs_review{RESET}")
        print(kv("discover", f"{ready_str}  ·  {review_str}  {DIM}({sd_age} old){RESET}"))
        print(kv("disc-sched",
                 f"{DIM}{_disc_sched} IST · next {_disc_next}{RESET}"))
        if n_ready > 0:
            print(kv("apply",
                     f"{DIM}python derive/slack_discover_channels.py "
                     f"--auto-mode --top 500 --apply{RESET}"))
    except Exception as e:
        print(kv("discover", f"{YELLOW}cache parse error: {str(e)[:40]}{RESET}"))
else:
    print(kv("discover",
             f"{DIM}no cache yet · {_disc_sched} IST · next {_disc_next}{RESET}"))

print(rule())
print()

# ─── rollup block ─────────────────────────────────────────────────────────────
if ROLLUP_LOG.exists():
    rl          = ROLLUP_LOG.read_text().splitlines()
    last_done_r = next((l for l in reversed(rl) if "Rollup done" in l), None)
    last_srt_r  = next((l for l in reversed(rl) if "Rollup starting" in l), None)

    if last_done_r:
        ts_str  = last_done_r.split(",")[0].strip()
        ran     = ts_str.startswith(today)
        r_status = pill("●", "ran today", GREEN) if ran else pill("◐", f"last: {ts_label(ts_str, today)}", YELLOW)
    elif last_srt_r:
        r_status = pill("⚡", "started (no done yet)", YELLOW)
    else:
        r_status = pill("○", "never ran", RED)

    print(f"  {'ROLLUP':<13}  {r_status}")
    print(kv("policy", "fires once · no retry · manual or cron"))
    if last_done_r:
        ts_str  = last_done_r.split(",")[0].strip()
        age     = rel_time(ts_str)
        ppl_m   = next((l for l in reversed(rl) if "People:" in l and "active" in l), None)
        prj_m   = next((l for l in reversed(rl) if "Projects:" in l and "configured" in l), None)
        ppl_s   = re.search(r"People:\s*(.+)", ppl_m).group(1).strip() if ppl_m else ""
        prj_s   = re.search(r"Projects:\s*(.+)", prj_m).group(1).strip() if prj_m else ""
        print(kv("last run", f"{BOLD}{ts_label(ts_str, today)}{RESET}  {DIM}({age}){RESET}"))
        if ppl_s: print(kv("people",   ppl_s))
        if prj_s: print(kv("projects", prj_s))

    # Classification cache breakdown (claude vs fallback).
    # fallback rows = subjects awaiting /rollup chat classification.
    cls = stats.get("classification", {})
    if cls:
        claude_n   = cls.get("claude",   0)
        fallback_n = cls.get("fallback", 0)
        total_cls  = claude_n + fallback_n
        if fallback_n > 0:
            print(kv("classify", f"{GREEN}{claude_n} claude{RESET}  {YELLOW}{fallback_n} fallback{RESET}  {DIM}/ {total_cls} cached{RESET}"))
            print(kv("pending",  f"{YELLOW}{BOLD}{fallback_n} subjects need /rollup{RESET}  {DIM}(keyword-only — upgrade via chat){RESET}"))
        elif total_cls > 0:
            print(kv("classify", f"{GREEN}{claude_n} claude{RESET}  {DIM}/ {total_cls} cached · 0 pending{RESET}"))
    print(rule())
    print()

# ─── leaves block ────────────────────────────────────────────────────────────
# Phase 1 (regex dump + render) cron-fires daily 04:00 via
# launchagents/com.example.leaves.plist. Phase 2 (chat classify) is
# owner-invoked via /leaves OR fired by autonomous-session routine.
LEAVES_LOG = ROOT / "logs/leaves.log"
LEAVES_PENDING = ROOT / "state/pending_leaves.json"
LEAVES_VERDICTS = ROOT / "state/verdicts.leaves.json"
LEAVES_STATE = ROOT / "state/last_leaves_success.date"
LEAVES_MD = ROOT / "derived/team-leaves.md"

if (LEAVES_STATE.exists() or LEAVES_PENDING.exists() or LEAVES_MD.exists()):
    last_date = LEAVES_STATE.read_text().strip() if LEAVES_STATE.exists() else None
    if last_date == today:
        l_status = pill("●", "ran today", GREEN)
    elif last_date:
        l_status = pill("◐", f"last: {last_date}", YELLOW)
    else:
        l_status = pill("○", "never ran", RED)
    print(f"  {'LEAVES':<13}  {l_status}")
    print(kv("policy", "daily 04:00 IST · dump + render · chat via /leaves"))

    # Pending count (regex-matched events awaiting chat classify).
    n_pending = 0
    if LEAVES_PENDING.exists():
        try:
            lp = json.loads(LEAVES_PENDING.read_text())
            n_pending = len(lp.get("pending", []))
        except Exception:
            pass

    # Verdicts file present = chat session emitted but apply hasn't run.
    n_unapplied = 0
    if LEAVES_VERDICTS.exists():
        try:
            lv = json.loads(LEAVES_VERDICTS.read_text())
            n_unapplied = len(lv.get("verdicts", lv) if isinstance(lv, dict) else lv)
        except Exception:
            pass

    # DB counts: total, upcoming next 30d, active today.
    n_total = n_up = n_active = 0
    if DB_PATH.exists():
        try:
            con = sqlite3.connect(str(DB_PATH))
            n_total = con.execute("SELECT COUNT(*) FROM team_leaves").fetchone()[0]
            today_iso = datetime.now(IST).date().isoformat()
            horizon = (datetime.now(IST).date() + timedelta(days=30)).isoformat()
            n_up = con.execute(
                "SELECT COUNT(*) FROM team_leaves "
                "WHERE date_start IS NOT NULL AND date_start > ? AND date_start <= ?",
                (today_iso, horizon),
            ).fetchone()[0]
            n_active = con.execute(
                "SELECT COUNT(*) FROM team_leaves "
                "WHERE date_start IS NOT NULL AND date_start <= ? "
                "AND (date_end IS NULL OR date_end >= ?)",
                (today_iso, today_iso),
            ).fetchone()[0]
            con.close()
        except Exception:
            pass

    pending_part = (f"{YELLOW}{BOLD}{n_pending} pending /leaves{RESET}"
                    if n_pending else f"{DIM}0 pending{RESET}")
    print(kv("classify", pending_part))
    if n_unapplied:
        print(kv("unapplied", f"{YELLOW}{n_unapplied} verdicts awaiting apply_leaves.py{RESET}"))
    if n_total or n_active or n_up:
        active_part = (f"{GREEN}{BOLD}{n_active} active{RESET}" if n_active
                       else f"{DIM}0 active{RESET}")
        up_part = (f"{CYAN}{n_up} upcoming-30d{RESET}" if n_up
                   else f"{DIM}0 upcoming-30d{RESET}")
        print(kv("calendar", f"{active_part}  ·  {up_part}  {DIM}({n_total} total){RESET}"))

    if LEAVES_MD.exists():
        md_mtime = datetime.fromtimestamp(LEAVES_MD.stat().st_mtime, tz=IST)
        md_age_s = int((datetime.now(IST) - md_mtime).total_seconds())
        if md_age_s < 3600:   md_age = f"{md_age_s // 60}m"
        elif md_age_s < 86400: md_age = f"{md_age_s // 3600}h"
        else:                  md_age = f"{md_age_s // 86400}d"
        print(kv("rendered", f"{DIM}derived/team-leaves.md ({md_age} old){RESET}"))
    print(rule())
    print()

# ─── pipeline (topic_brief) block ────────────────────────────────────────────
# Surfaces label/status/enrichment NULL gaps in topic_brief — catches the
# silent regression after every `cluster_diff apply` where new+relabel
# clusters land unlabelled / unenriched and the owner forgets the chat loop.
# Cache is refreshed by derive/topic_brief_validate.py (run on demand or
# wired into refresh-embeddings flow).
if TB_VALIDATE.exists():
    try:
        tb = json.loads(TB_VALIDATE.read_text())
        tb_mtime = datetime.fromtimestamp(TB_VALIDATE.stat().st_mtime, tz=IST)
        tb_age_s = int((datetime.now(IST) - tb_mtime).total_seconds())
        tb_age = (f"{tb_age_s // 60}m" if tb_age_s < 3600
                  else f"{tb_age_s // 3600}h" if tb_age_s < 86400
                  else f"{tb_age_s // 86400}d")
        n_fail = sum(1 for f in tb.get("findings", []) if f[0] == "FAIL")
        n_warn = sum(1 for f in tb.get("findings", []) if f[0] == "WARN")
        if n_fail:
            tb_pill = pill("○", f"{n_fail} FAIL · {n_warn} WARN", RED)
        elif n_warn:
            tb_pill = pill("◐", f"{n_warn} WARN", YELLOW)
        else:
            tb_pill = pill("●", "all fields populated", GREEN)
        print(f"  {BOLD}{'PIPELINE':<13}{RESET}  {tb_pill}  {DIM}topic_brief · {tb_age} old{RESET}")
        print(kv("policy", "after every cluster_diff apply: label → enrich → validate"))
        n_total = tb.get("n_total", 0)
        sd = tb.get("status_distribution", {})
        if sd:
            sd_str = "  ".join(f"{k.lower()}={v}" for k, v in sorted(sd.items(), key=lambda x: -x[1]))
            print(kv("clusters", f"{n_total} total  {DIM}{sd_str}{RESET}"))
        findings = sorted(tb.get("findings", []),
                          key=lambda x: (0 if x[0] == "FAIL" else 1))
        for sev, check, msg in findings[:4]:
            if sev == "PASS":
                continue
            col = RED if sev == "FAIL" else YELLOW
            print(f"  {'':13}  {col}{sev}{RESET} {DIM}{check:<22s}{RESET} {msg[:80]}")
        print(rule())
        print()
    except Exception as e:
        print(kv("pipeline", f"{YELLOW}cache parse error: {str(e)[:40]}{RESET}"))
        print(rule())
        print()
else:
    print(f"  {BOLD}{'PIPELINE':<13}{RESET}  {pill('○', 'no cache', YELLOW)}  {DIM}run derive/topic_brief_validate.py{RESET}")
    print(rule())
    print()


# ─── housekeeping block ──────────────────────────────────────────────────────
def parse_housekeeping():
    """Return (sched_label, next_fire_label, last_run_info)."""
    plist_p = PLIST_DIR / f"{HOUSE_PLIST_LABEL}.plist"
    if not plist_p.exists():
        plist_p = ROOT / "launchagents" / f"{HOUSE_PLIST_LABEL}.plist"

    sched_label = "(plist not found)"
    next_fire   = "?"
    if plist_p.exists():
        try:
            with plist_p.open("rb") as f:
                data = plistlib.load(f)
            sci = data.get("StartCalendarInterval")
            if isinstance(sci, dict):
                wd = sci.get("Weekday")
                hr = sci.get("Hour", 0)
                mn = sci.get("Minute", 0)
                weekdays = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
                wd_name = weekdays[wd] if isinstance(wd, int) and 0 <= wd <= 6 else "?"
                sched_label = f"weekly {wd_name} {hr:02d}:{mn:02d}"
                if isinstance(wd, int):
                    # launchd Weekday: Sun=0..Sat=6 | python weekday: Mon=0..Sun=6
                    py_target = (wd - 1) % 7
                    today_py  = now.weekday()
                    days_ahead = (py_target - today_py) % 7
                    target = now.replace(hour=hr, minute=mn, second=0, microsecond=0)
                    if days_ahead == 0 and now >= target:
                        days_ahead = 7
                    target = target + timedelta(days=days_ahead)
                    delta = target - now
                    total_h = int(delta.total_seconds()) // 3600
                    d = total_h // 24
                    h = total_h % 24
                    next_fire = f"~{d}d {h}h" if d else f"~{h}h"
        except Exception:
            pass

    last_info: dict = {}
    if HOUSE_LOG.exists():
        try:
            text = HOUSE_LOG.read_text()
            # Slice the LAST run: from final "=== Housekeeping (mode) ===" to EOF.
            run_starts = list(re.finditer(
                r"=== Housekeeping \((\w[\w-]*)\) ===\nToday: (\d{4}-\d{2}-\d{2})",
                text,
            ))
            if run_starts:
                last_match = run_starts[-1]
                mode      = last_match.group(1)
                date_str  = last_match.group(2)
                run_body  = text[last_match.start():]
                # Per-category action counts.
                actions: dict[str, int] = {}
                for m in re.finditer(
                    r"\[(?:DELETED|TRUNCD|DRY-RUN)\s*\]\s+(\S+)", run_body,
                ):
                    cat = m.group(1)
                    actions[cat] = actions.get(cat, 0) + 1
                # Summary footer.
                summary = re.search(
                    r"=== Summary ===\s*\nFiles affected: (\d+)\s*\nBytes affected: (\S+)",
                    run_body,
                )
                last_info = {"mode": mode, "date": date_str, "actions": actions}
                if summary:
                    last_info["files"] = int(summary.group(1))
                    last_info["bytes"] = summary.group(2)
            mtime = datetime.fromtimestamp(HOUSE_LOG.stat().st_mtime, tz=IST)
            last_info.setdefault("mtime", mtime)
        except Exception:
            pass

    return sched_label, next_fire, last_info


hk_sched, hk_next, hk_last = parse_housekeeping()

if hk_last.get("date"):
    try:
        last_dt = datetime.strptime(hk_last["date"], "%Y-%m-%d").replace(tzinfo=IST)
        age_days = (now - last_dt).days
        if age_days <= 8:
            hk_pill = pill("●", f"ran {age_days}d ago", GREEN)
        elif age_days <= 15:
            hk_pill = pill("◐", f"{age_days}d ago", YELLOW)
        else:
            hk_pill = pill("◐", f"stale ({age_days}d ago)", YELLOW)
    except Exception:
        hk_pill = pill("○", "parse error", YELLOW)
elif hk_last.get("mtime"):
    age_days = (now - hk_last["mtime"]).days
    hk_pill = pill("◐", f"log mtime {age_days}d ago", YELLOW)
else:
    hk_pill = pill("○", "never ran", RED)

print(f"  {BOLD}{'HOUSEKEEPING':<13}{RESET}  {hk_pill}  {DIM}next {hk_next}{RESET}")
print(kv("schedule", hk_sched))
print(kv("policy",   "weekly · prune old bak/verdicts/handoffs/logs + .DS_Store"))
if hk_last.get("date"):
    files_part = f"{hk_last['files']} files" if "files" in hk_last else "?"
    bytes_part = hk_last.get("bytes", "?")
    print(kv("last run",
             f"{BOLD}{hk_last['date']}{RESET}  {DIM}({hk_last['mode']}){RESET}   "
             f"{GREEN}{files_part}{RESET}  {DIM}{bytes_part}{RESET}"))
    actions = hk_last.get("actions") or {}
    if actions:
        cat_order = ["bak>60d", "verdict>15d", "handoff>15d",
                     "dverdict>15d", "log>60d", ".DS_Store"]
        ordered = [(c, actions[c]) for c in cat_order if c in actions]
        ordered += [(c, n) for c, n in actions.items() if c not in cat_order]
        act_str = "  ".join(f"{c}:{n}" for c, n in ordered)
        print(kv("actions", f"{DIM}{act_str}{RESET}"))
elif hk_last.get("mtime"):
    print(kv("last run",
             f"{BOLD}{hk_last['mtime'].strftime('%Y-%m-%d %H:%M')}{RESET}  "
             f"{DIM}(no summary block in log){RESET}"))
else:
    print(kv("last run", f"{DIM}none recorded{RESET}"))
print(rule())
print()

# ─── identity block ───────────────────────────────────────────────────────────
identity = {}
if IDENTITY_STATE.exists():
    try:
        identity = json.loads(IDENTITY_STATE.read_text())
    except Exception:
        pass

if identity:
    last_iso = identity.get("computed_at", "")
    # Age in IST relative to now.
    try:
        last_dt = datetime.strptime(last_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).astimezone(IST)
        secs = int((datetime.now(IST) - last_dt).total_seconds())
        if secs < 3600:
            age = f"{secs // 60}m"
        elif secs < 86400:
            age = f"{secs // 3600}h"
        else:
            age = f"{secs // 86400}d"
        ran_today = last_dt.strftime("%Y-%m-%d") == today
    except Exception:
        age = "?"
        ran_today = False

    n_changes = identity.get("n_changes", 0)
    n_orphans = identity.get("n_orphans", 0)
    if ran_today:
        id_pill = pill("●", "ran today", GREEN)
    elif age and age.endswith("d") and age != "?":
        id_pill = pill("◐", f"{age} stale", YELLOW)
    else:
        id_pill = pill("●", f"{age} ago", GREEN)

    print(f"  {BOLD}{'IDENTITY':<13}{RESET}  {id_pill}  {DIM}reconcile · {age} old{RESET}")
    print(kv("policy",   "ingest emits signals · reconcile after every fire"))

    by_scope = identity.get("by_scope", {}) or {}
    total = identity.get("total_entries", sum(by_scope.values()))
    print(kv("scope",
             f"team={by_scope.get('team', 0)}  "
             f"org={by_scope.get('org', 0)}  "
             f"external={by_scope.get('external', 0)}  "
             f"{DIM}(total={total}){RESET}"))

    cov = identity.get("coverage", {}) or {}
    if total:
        def _pct(n):
            return f"{round(100 * n / total)}%" if total else "—"
        print(kv("coverage",
                 f"email={_pct(cov.get('email', 0))}  "
                 f"jira_id={_pct(cov.get('jira_id', 0))}  "
                 f"slack_id={_pct(cov.get('slack_id', 0))}  "
                 f"github={_pct(cov.get('github', 0))}"))

    print(kv("signals",  f"{identity.get('signals_total', 0):,} pairs"))

    if n_changes or n_orphans:
        breakdown = identity.get("fill_breakdown") or {}
        b_str = "  ".join(f"{k}:{v}" for k, v in sorted(breakdown.items()))
        msg = f"{GREEN}+{n_changes} fills{RESET}"
        if b_str:
            msg += f"  {DIM}{b_str}{RESET}"
        if n_orphans:
            msg += f"  {YELLOW}+{n_orphans} orphans{RESET}"
        print(kv("last run", msg))
    else:
        print(kv("last run", f"{DIM}no changes — all entries fully resolved{RESET}"))
    print(rule())
    print()

# ─── embedding block ──────────────────────────────────────────────────────────
def embed_stats() -> dict:
    """Lightweight read of embedding table for cron-status. Does NOT run
    HDBSCAN / coherence checks — those live in derive/validate_embeddings.py
    and emit to EMBED_VALIDATE on demand."""
    if not DB_PATH.exists():
        return {}
    try:
        conn = sqlite3.connect(str(DB_PATH))
        # Schema may not exist on fresh DBs.
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='embedding'"
        ).fetchone():
            conn.close()
            return {}
        total = conn.execute("SELECT COUNT(*) FROM embedding").fetchone()[0]
        if not total:
            conn.close()
            return {"total": 0}
        by_src = dict(conn.execute(
            "SELECT source, COUNT(*) FROM embedding GROUP BY source"
        ).fetchall())
        by_model = dict(conn.execute(
            "SELECT model, COUNT(*) FROM embedding GROUP BY model"
        ).fetchall())
        dims = [r[0] for r in conn.execute(
            "SELECT DISTINCT dim FROM embedding"
        ).fetchall()]
        newest = conn.execute(
            "SELECT MAX(computed_at) FROM embedding"
        ).fetchone()[0]
        oldest = conn.execute(
            "SELECT MIN(computed_at) FROM embedding"
        ).fetchone()[0]
        # Coverage: events with content vs embedded subjects.
        n_events_subjects = conn.execute(
            "SELECT COUNT(DISTINCT subject) FROM events WHERE subject IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        return {
            "total":  total,
            "by_src": by_src,
            "by_model": by_model,
            "dims":   dims,
            "newest": newest,
            "oldest": oldest,
            "subjects_in_events": n_events_subjects,
        }
    except Exception:
        return {}


emb = embed_stats()
if emb.get("total"):
    # Freshness pill.
    newest = emb.get("newest")
    try:
        nd = datetime.strptime(newest, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).astimezone(IST)
        secs = int((datetime.now(IST) - nd).total_seconds())
        if secs < 86400:
            emb_age = f"{secs // 3600}h" if secs >= 3600 else f"{secs // 60}m"
        else:
            emb_age = f"{secs // 86400}d"
    except Exception:
        emb_age = "?"

    if emb_age.endswith("d") and emb_age != "?":
        days = int(emb_age[:-1])
        emb_pill = pill("◐", f"{days}d stale", YELLOW) if days >= 2 \
                   else pill("●", f"refreshed {emb_age} ago", GREEN)
    else:
        emb_pill = pill("●", f"refreshed {emb_age} ago", GREEN)

    print(f"  {BOLD}{'EMBEDDING':<13}{RESET}  {emb_pill}  {DIM}newest · {emb_age} old{RESET}")
    print(kv("policy",   "refresh-embeddings after ingest · validate on demand"))

    # Per-source counts.
    parts = []
    for src in SOURCES:
        n = emb.get("by_src", {}).get(src, 0)
        if n:
            sc = SRC_COLOR.get(src, "")
            parts.append(f"{sc}{src}={n:,}{RESET}")
    print(kv("by source", "  ".join(parts) if parts else "—"))

    # Model + dim line.
    models = list((emb.get("by_model") or {}).keys())
    model_str = models[0] if len(models) == 1 else f"{len(models)} models"
    dims = emb.get("dims", [])
    dim_str = str(dims[0]) if len(dims) == 1 else f"{len(dims)} dims"
    print(kv("model",    f"{model_str}  {DIM}dim={dim_str}{RESET}"))

    # Coverage: embedded subjects vs total subjects in events table.
    n_total_subj = emb.get("subjects_in_events", 0)
    if n_total_subj:
        cov_pct = round(100 * emb["total"] / n_total_subj)
        print(kv("coverage",
                 f"{emb['total']:,} / {n_total_subj:,} subjects  "
                 f"{DIM}({cov_pct}%){RESET}"))

    # Optional: validate report findings (if present).
    if EMBED_VALIDATE.exists():
        try:
            evj = json.loads(EMBED_VALIDATE.read_text())
            findings = evj.get("findings") or []
            warns = [f for f in findings if f and f[0] in ("WARN", "FAIL")]
            if warns:
                worst = warns[0]
                print(kv("validate",
                         f"{YELLOW}{len(warns)} {worst[0]}{RESET}  "
                         f"{DIM}{worst[1]}: {worst[2][:60]}{RESET}"))
            else:
                print(kv("validate", f"{GREEN}✓ clean{RESET}"))
        except Exception:
            pass

    print(rule())
    print()

# ─── CODE-GRAPH lane ───────────────────────────────────────────────────────────
# Daily 18:00 LaunchAgent (com.example.codegraph): git ff-if-clean + full
# code-review-graph rebuild for service-a + service-c. Feeds /ask.
cgs = cg.read_status(STATE_DIR, PLIST_DIR)
if cgs.get("success_date") or cgs.get("start"):
    sd = cgs.get("success_date")
    if cgs.get("running"):
        cg_pill = pill("⚡", "rebuilding now", YELLOW)
    elif cgs.get("fail"):
        cg_pill = pill("✗", f"last run {cgs['fail']} repo(s) failed", RED)
    elif sd == today:
        cg_pill = pill("●", "rebuilt today", GREEN)
    elif sd:
        days = (datetime.strptime(today, "%Y-%m-%d")
                - datetime.strptime(sd, "%Y-%m-%d")).days
        cg_pill = pill("◐", f"{days}d stale", YELLOW)
    else:
        cg_pill = pill("○", "never succeeded", RED)

    nxt = f"next {cgs['next']}" if cgs.get("next") and not cgs.get("running") else "next —"
    print(f"  {BOLD}{'CODE-GRAPH':<13}{RESET}  {cg_pill}  {DIM}{nxt}{RESET}")
    print(kv("schedule", f"daily {cgs.get('sched', '18:00 IST')}"))
    print(kv("policy",   "git ff-if-clean · full rebuild · no LLM · feeds /ask code-logic"))

    if cgs.get("done"):
        age = rel_time(cgs["done"])
        okc, flc = cgs.get("ok"), cgs.get("fail")
        verdict = (f"{GREEN}ok={okc}{RESET}" if not flc
                   else f"{GREEN}ok={okc}{RESET} {RED}fail={flc}{RESET}")
        print(kv("last run", f"{BOLD}{cgs['done']}{RESET}  {DIM}({age}){RESET}   {verdict}"))
    elif cgs.get("running"):
        print(kv("last run", f"{YELLOW}in progress since {cgs.get('start','?')}{RESET}"))
    else:
        print(kv("last run", f"{DIM}none recorded{RESET}"))

    def _knum(n: int) -> str:
        return f"{n/1000:.0f}k" if n >= 1000 else str(n)
    rparts = []
    for r in cgs.get("repos", []):
        mark = f"{GREEN}✓{RESET}" if r.get("ok") else f"{RED}✗{RESET}"
        tot = r.get("totals")
        suffix = (f" {DIM}({_knum(tot['nodes'])} nodes·{_knum(tot['edges'])} edges){RESET}"
                  if tot else "")
        rparts.append(f"{r['name']} {mark}{suffix}")
    if rparts:
        print(kv("repos", "   ".join(rparts)))

    print(rule())
    print()

# ─── DB snapshot ──────────────────────────────────────────────────────────────
if db_total:
    print(f"  {BOLD}DB SNAPSHOT{RESET}  {DIM}{db_total:,} total events{RESET}")
    print()
    for src in SOURCES:
        n  = db_by_src.get(src, 0)
        sc = SRC_COLOR.get(src, "")
        pct = f"{100 * n // db_total}%" if db_total else "0%"
        print(f"  {sc}{src:<12}{RESET}  {bar(n, db_total, 22)}  {n:>6,}  {DIM}{pct:>4}{RESET}")
    print()

# ─── recent runs ──────────────────────────────────────────────────────────────
print(f"  {BOLD}RECENT RUNS{RESET}  {DIM}last 6{RESET}")
print()
recent = sorted(runs, key=lambda r: r["done"], reverse=True)[:6]
if recent:
    for r in recent:
        sc    = SRC_COLOR.get(r["source"], "")
        t_lbl = ts_label(r["done"], today)
        age   = rel_time(r["done"])
        wflag = f"  {YELLOW}⚠{RESET}" if r["warnings"] else ""
        print(f"  {DIM}{t_lbl:<9}{RESET}  {sc}{BOLD}{r['source']:<12}{RESET}  {GREEN}+{r['new']:<5}{RESET}  {DIM}{r['dup']} dup{RESET}  {DIM}({age}){RESET}{wflag}")
else:
    print(f"  {DIM}none{RESET}")
print()

# ─── health footer ────────────────────────────────────────────────────────────
total_warns = sum(len(r["warnings"]) for r in last_per_src.values())
all_ran = all(read_marker(s) == today for s in SOURCES)

# Per-source ingest-down detection (mirrors per-lane logic above).
down_sources: list[tuple[str, str]] = []
for s in SOURCES:
    r = last_per_src.get(s) or {}
    for w in r.get("warnings", []):
        if "Cursor NOT updated" in w:
            m_msg = re.search(r"Cursor NOT updated[^\n]*", w)
            down_sources.append((s, m_msg.group(0)[:100] if m_msg else ""))
            break

# Per-source overrun detection (run duration vs fire interval).
overrun_fail: list[tuple[str, str]] = []
overrun_warn: list[tuple[str, str]] = []
for s in SOURCES:
    _sl, _fm = sched_info.get(s, ("", []))
    ov = overrun_for(s, _fm)
    if ov and ov["level"] == "fail":
        overrun_fail.append((s, ov["label"]))
    elif ov and ov["level"] == "warn":
        overrun_warn.append((s, ov["label"]))

print(f"  {BOLD}HEALTH{RESET}  ", end="")
if down_sources:
    names = ", ".join(s for s, _ in down_sources)
    print(pill("✗", f"INGEST DOWN: {names}", RED))
    for s, reason in down_sources:
        print(f"    {RED}{s:<12}{RESET}  {DIM}{reason}{RESET}")
elif running_src:
    print(pill("⚡", f"{running_src} currently running", YELLOW))
elif all_ran:
    print(pill("●", "all sources ran today", GREEN))
else:
    missing = [s for s in SOURCES if read_marker(s) != today]
    print(pill("◐", f"pending: {', '.join(missing)}", YELLOW))

# Overrun banner: a run taking longer than its fire interval collides with
# the next fire (launchd SIGTERMs it). Surfaced even when sources ran fine.
if overrun_fail:
    names = ", ".join(s for s, _ in overrun_fail)
    print(f"  {'':8}{pill('✗', f'OVERRUN: {names} (run ≥ fire interval)', RED)}")
    for s, lbl in overrun_fail:
        print(f"    {RED}{s:<12}{RESET}  {DIM}{lbl}{RESET}")
if overrun_warn:
    names = ", ".join(s for s, _ in overrun_warn)
    print(f"  {'':8}{pill('⚠', f'near fire interval: {names}', YELLOW)}")
    for s, lbl in overrun_warn:
        print(f"    {YELLOW}{s:<12}{RESET}  {DIM}{lbl}{RESET}")

print(f"  {DIM}  {'no warnings' if not total_warns else f'{total_warns} warning(s) across last runs'}{RESET}")
print()

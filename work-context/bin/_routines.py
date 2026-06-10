#!/usr/bin/env python3
"""Scheduled-agent (\"routine\") status — shared by cron-status.sh + dashboard.py.

Routines are Claude Code scheduled remote agents (the /schedule mechanism),
distinct from the launchd LaunchAgents ("crons") tracked elsewhere. Their
SKILL.md bodies live under ~/.claude/scheduled-tasks/<id>/SKILL.md, but the
structured schedule state (cron expression, enabled flag, last-run) lives in a
registry JSON the desktop app owns:

    ~/Library/Application Support/Claude/claude-code-sessions/*/*/scheduled-tasks.json

A plain script can't call the scheduled-tasks MCP, so we read that registry
directly (newest wins) and join each entry with its SKILL.md description. We
also parse the 5-field cron expression ourselves (no croniter in the venv) to
derive a human cadence + the next fire time.

Cron expressions are evaluated in the SAME local timezone the scheduler uses.
The owner's machine runs IST, so next-fire is computed in IST to match the rest
of cron-status.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

REGISTRY_GLOB = (
    "Library/Application Support/Claude/claude-code-sessions/*/*/scheduled-tasks.json"
)
SKILL_DIR = Path.home() / ".claude" / "scheduled-tasks"

_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # python weekday() order


# ── registry discovery ─────────────────────────────────────────────────────────

def registry_path() -> Path | None:
    """Newest scheduled-tasks.json under the app session dirs, or None."""
    matches = sorted(
        Path.home().glob(REGISTRY_GLOB),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def _skill_description(file_path: str | None, task_id: str) -> str:
    """Pull the `description:` frontmatter from a routine's SKILL.md."""
    p = Path(file_path) if file_path else (SKILL_DIR / task_id / "SKILL.md")
    if not p.exists():
        return ""
    try:
        head = p.read_text()[:2000]
    except Exception:
        return ""
    m = re.search(r"^description:\s*(.+)$", head, re.MULTILINE)
    return m.group(1).strip() if m else ""


# ── cron parsing (5-field: min hour dom month dow) ──────────────────────────────

def _expand_field(field: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field into the set of matching integers in [lo, hi]."""
    out: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part in ("*", ""):
            rng = range(lo, hi + 1)
        elif "-" in part:
            a, b = part.split("-", 1)
            rng = range(int(a), int(b) + 1)
        else:
            v = int(part)
            rng = range(v, v + 1)
        out.update(n for n in rng if lo <= n <= hi and (n - rng.start) % step == 0)
    return out


def parse_cron(expr: str) -> dict | None:
    """Return {minute,hour,dom,month,dow} sets, or None if not 5-field."""
    fields = (expr or "").split()
    if len(fields) != 5:
        return None
    try:
        return {
            "minute": _expand_field(fields[0], 0, 59),
            "hour":   _expand_field(fields[1], 0, 23),
            "dom":    _expand_field(fields[2], 1, 31),
            "month":  _expand_field(fields[3], 1, 12),
            # cron dow: 0 and 7 both = Sunday.
            "dow":    {d % 7 for d in _expand_field(fields[4], 0, 7)},
        }
    except ValueError:
        return None


def _cron_matches(c: dict, dt: datetime) -> bool:
    if dt.minute not in c["minute"]:
        return False
    if dt.hour not in c["hour"]:
        return False
    if dt.month not in c["month"]:
        return False
    # cron weekday: Sunday=0; python weekday(): Monday=0..Sunday=6.
    cron_dow = (dt.weekday() + 1) % 7
    dom_restricted = len(c["dom"]) != 31
    dow_restricted = len(c["dow"]) != 7
    dom_ok = dt.day in c["dom"]
    dow_ok = cron_dow in c["dow"]
    # Standard cron rule: if BOTH dom and dow are restricted, match either.
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def next_fire(expr: str, now: datetime | None = None) -> datetime | None:
    """Next datetime (IST) the cron fires after `now`, scanning ≤14 days."""
    c = parse_cron(expr)
    if not c:
        return None
    now = now or datetime.now(IST)
    cur = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(14 * 24 * 60):
        if _cron_matches(c, cur):
            return cur
        cur += timedelta(minutes=1)
    return None


def cron_human(expr: str) -> str:
    """Compact human cadence from a 5-field cron expression."""
    c = parse_cron(expr)
    if not c:
        return expr or "(no schedule)"
    fields = expr.split()
    mins, hours = sorted(c["minute"]), sorted(c["hour"])

    # Time-of-day descriptor.
    if len(mins) == 1 and len(hours) == 1:
        time_desc = f"{hours[0]:02d}:{mins[0]:02d}"
    elif len(hours) == 1:
        time_desc = f"{hours[0]:02d}h ·" + "+".join(f":{m:02d}" for m in mins)
    else:
        # Multiple hours: describe minute cadence + hour window.
        min_part = "+".join(f":{m:02d}" for m in mins)
        if hours == list(range(hours[0], hours[-1] + 1)):
            hour_part = f"{hours[0]:02d}h–{hours[-1]:02d}h"
        else:
            hour_part = ",".join(f"{h:02d}h" for h in hours)
        # Recognise the every-N-minutes idiom (e.g. "0,30").
        if len(mins) > 1 and len(set(mins[i + 1] - mins[i] for i in range(len(mins) - 1))) == 1:
            time_desc = f"every {mins[1] - mins[0]}m · {hour_part}"
        else:
            time_desc = f"{min_part} · {hour_part}"

    # Day descriptor.
    dow_restricted = len(c["dow"]) != 7
    if dow_restricted:
        days = sorted(c["dow"])  # cron order: 0=Sun
        # Re-map to python label order for readability.
        names = [["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d] for d in days]
        if days == [1, 2, 3, 4, 5]:
            day_desc = "Mon–Fri"
        else:
            day_desc = "+".join(names)
    elif fields[2] != "*":
        day_desc = f"dom {fields[2]}"
    else:
        day_desc = "daily"

    return f"{time_desc} · {day_desc} IST"


# ── relative-time helpers ───────────────────────────────────────────────────────

def _rel_past(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 90:    return "just now"
        if secs < 3600:  return f"{secs // 60}m ago"
        if secs < 86400: return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return ""


def _rel_future(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "?"
    now = now or datetime.now(IST)
    total_min = max(0, int((dt - now).total_seconds()) // 60)
    d, rem = divmod(total_min, 1440)
    h, mn = divmod(rem, 60)
    if d:
        return f"~{d}d {h}h"
    if h:
        return f"~{h}h {mn}m"
    return f"~{mn}m"


# ── public API ──────────────────────────────────────────────────────────────────

def load_routines(now: datetime | None = None) -> list[dict]:
    """List of routine status dicts, sorted enabled-first then by id.

    Each: {id, enabled, cron, sched_human, last_run_iso, last_run_rel,
           next_fire_iso, next_fire_rel, desc, cwd, stale}
    `stale` = enabled but lastRunAt older than ~2 expected intervals (best-effort).
    """
    reg = registry_path()
    if not reg:
        return []
    try:
        data = json.loads(reg.read_text())
    except Exception:
        return []

    now = now or datetime.now(IST)
    out: list[dict] = []
    for t in data.get("scheduledTasks", []):
        expr = t.get("cronExpression", "")
        enabled = bool(t.get("enabled"))
        last_iso = t.get("lastRunAt")
        nf = next_fire(expr, now) if enabled else None
        out.append({
            "id":            t.get("id", "?"),
            "enabled":       enabled,
            "cron":          expr,
            "sched_human":   cron_human(expr),
            "last_run_iso":  last_iso,
            "last_run_rel":  _rel_past(last_iso),
            "next_fire_iso": nf.isoformat() if nf else None,
            "next_fire_rel": _rel_future(nf, now) if nf else None,
            "desc":          _skill_description(t.get("filePath"), t.get("id", "")),
            "cwd":           t.get("cwd", ""),
        })

    out.sort(key=lambda r: (not r["enabled"], r["id"]))
    return out


if __name__ == "__main__":
    # Smoke test / quick CLI view.
    for r in load_routines():
        flag = "on " if r["enabled"] else "off"
        print(f"{flag}  {r['id']:<22}  {r['sched_human']:<28}  "
              f"last {r['last_run_rel'] or '—':<10}  next {r['next_fire_rel'] or '—'}")

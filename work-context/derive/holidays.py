#!/usr/bin/env python3
"""holidays.py — company holiday calendar loader (single source of truth).

Reads config/holidays-<year>.yaml (fetched from Darwinbox) and exposes
helpers consumed by both derive/render_leaves.py and bin/dashboard.py.
Company holidays are company-wide, not per-person — they are display-only
and are NOT written into the team_leaves table.

A holiday dict has: date (ISO), day (weekday name), type
("holiday" = fixed/mandatory, "optional" = optional/restricted), occasion.

All functions degrade gracefully when the YAML for a year is missing
(return empty lists / None) so callers never need a try/except.
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _REPO_ROOT / "config"


@lru_cache(maxsize=8)
def load(year: int) -> list[dict]:
    """All holidays for a year, sorted by date. Empty list if no config."""
    p = _CONFIG_DIR / f"holidays-{year}.yaml"
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except Exception:
        return []
    rows = []
    for r in (data.get("holidays") or []):
        if not r.get("date"):
            continue
        # PyYAML parses bare ISO dates into datetime.date; normalize to str
        # so every consumer can do plain string comparisons.
        r = {**r, "date": str(r["date"])}
        rows.append(r)
    return sorted(rows, key=lambda r: r["date"])


def _load_span(d_start: str, d_end: str) -> list[dict]:
    """All holidays whose ISO date falls in [d_start, d_end], across years."""
    y0, y1 = int(d_start[:4]), int(d_end[:4])
    out: list[dict] = []
    for y in range(y0, y1 + 1):
        out.extend(load(y))
    return [h for h in out if d_start <= h["date"] <= d_end]


def in_window(d_start: str, d_end: str) -> list[dict]:
    """Holidays within an inclusive ISO-date window (e.g. the gantt range)."""
    return _load_span(d_start, d_end)


def upcoming(today: str, days: int) -> list[dict]:
    """Holidays from today (inclusive) through today+`days`."""
    end = (date.fromisoformat(today) + _timedelta(days)).isoformat()
    return [h for h in _load_span(today, end) if h["date"] >= today]


def next_holiday(today: str, *, fixed_only: bool = False) -> dict | None:
    """The soonest holiday on or after `today`, or None if none configured.

    `fixed_only` skips optional/restricted holidays — handy for a headline
    "next company holiday everyone's off" line.
    """
    end = f"{int(today[:4]) + 1}-12-31"
    for h in _load_span(today, end):
        if h["date"] < today:
            continue
        if fixed_only and h.get("type") != "holiday":
            continue
        return h
    return None


def is_holiday(d: str) -> dict | None:
    """Return the holiday dict for ISO date `d`, or None.

    When a date is both (it never is here, but be safe), the fixed holiday
    wins over an optional one.
    """
    matches = [h for h in load(int(d[:4])) if h["date"] == d]
    if not matches:
        return None
    matches.sort(key=lambda h: 0 if h.get("type") == "holiday" else 1)
    return matches[0]


# Local import kept tiny so the module has no datetime.timedelta name clash
# with `date` above.
def _timedelta(days: int):
    from datetime import timedelta
    return timedelta(days=days)


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    print(f"today={t}")
    nh = next_holiday(t)
    nf = next_holiday(t, fixed_only=True)
    print(f"next holiday:       {nh}")
    print(f"next fixed holiday: {nf}")
    print(f"upcoming 60d:       {len(upcoming(t, 60))}")
    for h in upcoming(t, 60):
        print(f"  {h['date']}  {h['type']:8}  {h['occasion']}")

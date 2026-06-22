"""
pulse.py — concise 1:1 trend signals for a person: recent window vs the prior
equal window, leave-aware.

Built for the `/pulse <person>` skill. Where `person_profile` / `person_v3`
produce a full audit profile, `pulse` produces a SHORT delta read for 1:1 prep:
"how is X doing lately, and is that up or down vs before — adjusted for leave".

The leave-awareness is the whole point: a blind recent-vs-prior diff over a
fortnight will scream "productivity collapsed" the moment someone takes a few
days off. This computes effective working days per window and surfaces any
leave/OOO/sick mention in the recent window, so the consumer never mistakes
fewer working days for lower effort.

CLI
---
    .venv/bin/python derive/pulse.py --name grace
    .venv/bin/python derive/pulse.py --name grace --weeks 2
    .venv/bin/python derive/pulse.py --name alex --weeks 3 --asof 2026-05-31

JSON contract — top-level keys
------------------------------
    person, role, asof, weeks
    windows         — {recent: {since, until}, prior: {since, until}}
    working_days    — {recent, prior}  (distinct authored-event days)
    leave           — {recent: [{date_start, date_end, reason, excerpt, url}], ...]}
    metrics         — [{key, label, recent, prior, direction, note}]
    recent_work     — top recent authored items (PRs merged, tickets done) for citation
    flags           — list of plain-string caveats (short window, leave, etc.)
    meta            — computed_at, schema_version
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db  # noqa: E402
from derive.person_profile import _build_person_alias_map, _resolve_canonical  # noqa: E402
from derive.person_v3 import build_v3  # noqa: E402

SCHEMA_VERSION = "1"

# Leave / OOO / sick detection in the person's OWN slack messages.
LEAVE_RX = re.compile(
    r"(?i)\b(on leave|sick leave|taking leave|be on leave|will be off|"
    r"out of office|\booo\b|on vacation|taking the day off|taking off|"
    r"not feeling well|unwell|fever)\b"
)


def _ph(aliases: list[str]) -> str:
    return ",".join("?" * len(aliases))


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _working_days(conn, aliases, since, until, active_thresh: int = 5) -> dict:
    """Per-day authored-event counts → {touched: days with ≥1, active: days with
    ≥active_thresh}. `active` is the leave-adjusted denominator: a day with one
    auto/Jira event or a single 'I'm on leave' post is NOT a working day."""
    ph = _ph(aliases)
    rows = conn.execute(
        f"""SELECT substr(ts,1,10) d, COUNT(*) c FROM events
            WHERE actor IN ({ph}) AND ts >= ? AND ts < ?
            GROUP BY d""",
        (*aliases, since, until),
    ).fetchall()
    touched = len(rows)
    active = sum(1 for _, c in rows if c >= active_thresh)
    return {"touched": touched, "active": active}


def _leave_mentions(conn, aliases, since, until) -> list[dict]:
    """The person's own slack messages in-window that read as leave/OOO/sick."""
    ph = _ph(aliases)
    # Select the FULL body — the leave keyword routinely sits past the first
    # ~160 chars (subteam ping + cc-list precede it). Match on full body; trim
    # only the display excerpt, in Python. See standup_gather.py commit c316d42.
    rows = conn.execute(
        f"""SELECT ts, body, url FROM events
            WHERE source='slack' AND actor IN ({ph})
              AND ts >= ? AND ts < ? AND body IS NOT NULL
            ORDER BY ts""",
        (*aliases, since, until),
    ).fetchall()
    out = []
    seen = set()
    for ts, body, url in rows:
        if not body or not LEAVE_RX.search(body):
            continue
        key = (ts[:16], (body or "")[:40])
        if key in seen:
            continue
        seen.add(key)
        excerpt = body.strip()[:160]
        out.append({"ts": ts, "excerpt": excerpt, "url": url})
    return out


def _recent_work(conn, aliases, since, until, limit=8) -> list[dict]:
    """Concrete authored output in the recent window for citation — merged PRs
    and tickets moved to a done-ish state."""
    ph = _ph(aliases)
    rows = conn.execute(
        f"""SELECT ts, source, event_type, subject, COALESCE(title,'') t, url FROM events
            WHERE actor IN ({ph}) AND ts >= ? AND ts < ?
              AND event_type IN ('pr_merged','pr_opened','issue_created')
            ORDER BY ts DESC LIMIT ?""",
        (*aliases, since, until, limit),
    ).fetchall()
    return [
        {"ts": r[0], "source": r[1], "event_type": r[2],
         "subject": r[3], "title": r[4], "url": r[5]}
        for r in rows
    ]


def _g(d, *ks):
    for k in ks:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _direction(recent, prior, higher_is_better=True, pct_tol=0.10):
    """↑ / ↓ / → based on relative change, with a dead-band so noise reads flat.
    Returns a plain token; the skill maps it to words."""
    try:
        r = float(recent) if recent is not None else None
        p = float(prior) if prior is not None else None
    except (TypeError, ValueError):
        return "n/a"
    if r is None or p is None:
        return "n/a"
    if p == 0 and r == 0:
        return "flat"
    if p == 0:
        return "up" if r > 0 else "flat"
    change = (r - p) / abs(p)
    if abs(change) < pct_tol:
        return "flat"
    return "up" if change > 0 else "down"


def _trend(direction: str, higher_is_better: bool) -> str:
    """Map numeric direction → goodness so the renderer can't misread a metric
    where up is bad (rank number, latency, after-hours, abandons)."""
    if direction in ("n/a", "flat"):
        return direction
    if direction == "up":
        return "better" if higher_is_better else "worse"
    return "worse" if higher_is_better else "better"


def build_pulse(conn, name: str, weeks: int = 2, asof: str | None = None) -> dict:
    pmap = _build_person_alias_map()
    canon = _resolve_canonical(name, pmap)
    if not canon:
        return {"error": f"no person matches '{name}'"}
    info = pmap[canon]
    aliases = info["aliases"]

    # Windows.
    if asof:
        end = datetime.strptime(asof, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = end + timedelta(days=1)  # inclusive of asof day
    else:
        now = datetime.now(timezone.utc)
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    span = timedelta(weeks=weeks)
    recent_since = end - span
    prior_until = recent_since
    prior_since = prior_until - span

    R = build_v3(conn, canon, _iso(recent_since), _iso(end))
    P = build_v3(conn, canon, _iso(prior_since), _iso(prior_until))

    wd_r = _working_days(conn, aliases, _iso(recent_since), _iso(end))
    wd_p = _working_days(conn, aliases, _iso(prior_since), _iso(prior_until))
    leave = _leave_mentions(conn, aliases, _iso(recent_since), _iso(end))
    recent_work = _recent_work(conn, aliases, _iso(recent_since), _iso(end))

    # Metric spec: (key, label, path-into-v3, higher_is_better)
    spec = [
        ("sp_shipped",      "Story points shipped",   ("completion", "story_points_shipped"), True),
        ("tickets_attr",    "Tickets delivered",      ("v1_signals", "tickets_attributed"),   True),
        ("team_rank",       "Team SP rank",           ("v1_signals", "team_rank"),            False),
        ("prs_opened",      "PRs opened",             ("quality", "pr_count_in_window"),      True),
        ("pr_shipped",      "PRs shipped",            ("pace", "shipped"),                    True),
        ("pr_abandoned",    "PRs abandoned",          ("pace", "abandoned"),                  False),
        ("commits_in_pr",   "Commits into PRs",       ("contribution", "substantive_pr_commits"), True),
        ("reviews_total",   "Reviews given",          ("contribution", "pr_reviews_total"),   True),
        ("slack_replies",   "Substantive Slack replies", ("contribution", "substantive_slack_replies"), True),
        ("critical_flags",  "Critical code flags",    ("quality", "pr_matterai_critical_flags"), False),
        ("after_hours",     "After-hours share %",    ("behavioral", "after_hours_share_pct"), False),
        ("first_responder", "First-responder %",      ("behavioral", "first_responder_rate_pct"), True),
        ("p50_latency",     "Median reply latency (min)", ("behavioral", "p50_response_latency_min"), False),
        ("events_total",    "Total activity events",  ("behavioral", "samples", "all_events_n"), True),
    ]
    metrics = []
    for key, label, path, hib in spec:
        r = _g(R, *path)
        p = _g(P, *path)
        direction = _direction(r, p, hib)
        metrics.append({
            "key": key, "label": label,
            "recent": r, "prior": p,
            "direction": direction,
            "trend": _trend(direction, hib),
            "higher_is_better": hib,
        })

    # Flags / caveats.
    flags = []
    if leave:
        flags.append(
            f"{len(leave)} leave/OOO/sick mention(s) in the recent window — "
            "every 'down' arrow likely traces to fewer working days, not lower effort."
        )
    ar, ap = wd_r["active"], wd_p["active"]
    if ap:
        ratio = round(ar / ap, 2)
        flags.append(
            f"Effective working days: ~{ar} recent vs ~{ap} prior "
            f"(~{ratio}× the days) — normalise any volume drop against this."
        )
    flags.append(
        f"{weeks}-week windows are below the reliable tier/velocity threshold — "
        "read as a pulse, not a verdict; behavioural rates ride on a small sample."
    )

    return {
        "person": canon,
        "role": info.get("role"),
        "asof": (asof or end.strftime("%Y-%m-%d")),
        "weeks": weeks,
        "windows": {
            "recent": {"since": _iso(recent_since)[:10], "until": end.strftime("%Y-%m-%d")},
            "prior": {"since": _iso(prior_since)[:10], "until": _iso(prior_until)[:10]},
        },
        "work_mix": {"recent": _g(R, "rating", "window_work_mix"),
                     "prior": _g(P, "rating", "window_work_mix")},
        "working_days": {"recent": wd_r, "prior": wd_p},
        "leave": leave,
        "metrics": metrics,
        "recent_work": recent_work,
        "flags": flags,
        "meta": {"computed_at": _iso(datetime.now(timezone.utc)),
                 "schema_version": SCHEMA_VERSION},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--weeks", type=int, default=2)
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD; default = today (UTC)")
    args = ap.parse_args()
    conn = get_db()
    out = build_pulse(conn, args.name, args.weeks, args.asof)
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

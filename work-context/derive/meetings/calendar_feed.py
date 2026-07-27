#!/usr/bin/env python3
"""
calendar_feed.py — read the published Outlook ICS feed (meeting-intelligence P3).

The feed URL lives in config/sources.yaml `calendar.ics_url` (gitignored —
it is a bearer URL). Fetched copy is cached at state/calendar_feed.ics and
refreshed when older than --max-age (default 10 min), so repeated queries in
one session cost one network hit. Published Outlook feeds carry SUMMARY /
DTSTART / DTEND / LOCATION / DESCRIPTION(+Teams link) / STATUS / UID /
RRULE / RECURRENCE-ID / EXDATE — but NO attendee list (Microsoft strips it).

Recurrence: masters with RRULE are expanded via dateutil; RECURRENCE-ID
override instances replace their expanded occurrence; EXDATE + cancelled
overrides are dropped. This is the part naive readers get wrong and the
reason recurring standups would otherwise show at their original time.

CLI (all output is compact `HH:MM-HH:MM | title | teams? | uid` lines, IST):
  today                      today's meetings
  day <YYYY-MM-DD>           that day's meetings
  upcoming [N]               next N days (default 3)
  next                       the next meeting from now (for the record nudge)
  soon [MIN]                 machine-readable: meetings starting within MIN
                             minutes (default 5) — the PRE-CALL record nudge
  refresh                    force re-fetch now
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

from dateutil import rrule as du_rrule
from dateutil import tz as du_tz

WC = Path(__file__).resolve().parents[2]
CACHE = WC / "state" / "calendar_feed.ics"
IST = du_tz.gettz("Asia/Kolkata")


def _ics_url() -> str:
    import yaml

    with open(WC / "config" / "sources.yaml") as f:
        cfg = yaml.safe_load(f) or {}
    url = (cfg.get("calendar") or {}).get("ics_url")
    if not url:
        sys.exit("ERROR: calendar.ics_url missing from config/sources.yaml")
    return url


def _fetch(max_age_min: int = 10, force: bool = False) -> str:
    if (not force and CACHE.exists()
            and time.time() - CACHE.stat().st_mtime < max_age_min * 60):
        return CACHE.read_text(encoding="utf-8", errors="replace")
    url = _ics_url()
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(f".ics.tmp.{__import__('os').getpid()}")
    r = subprocess.run(["curl", "-sf", "--max-time", "60", url, "-o", str(tmp)])
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 100:
        tmp.unlink(missing_ok=True)
        if CACHE.exists():
            print("WARN: feed refresh failed — using stale cache", file=sys.stderr)
            return CACHE.read_text(encoding="utf-8", errors="replace")
        sys.exit("ERROR: calendar feed fetch failed and no cache present")
    tmp.replace(CACHE)
    return CACHE.read_text(encoding="utf-8", errors="replace")


# --- ICS parsing -------------------------------------------------------------

def _unfold(raw: str) -> str:
    return raw.replace("\r\n ", "").replace("\r\n\t", "").replace("\n ", "").replace("\n\t", "")


def _prop(ev: str, name: str) -> str | None:
    m = re.search(rf"^{name}[^:\n]*:(.*)$", ev, re.M)
    return m.group(1).strip() if m else None


def _parse_dt(ev: str, name: str) -> datetime | None:
    """DTSTART/DTEND with TZID=, UTC 'Z', or date-only forms → aware dt (IST)."""
    m = re.search(rf"^{name}(;[^:\n]*)?:(\d{{8}})(T(\d{{6}}))?(Z)?", ev, re.M)
    if not m:
        return None
    params, ymd, _, hms, zulu = m.groups()
    d = datetime.strptime(ymd, "%Y%m%d")
    if hms:
        d = d.replace(hour=int(hms[:2]), minute=int(hms[2:4]), second=int(hms[4:6]))
    if zulu:
        return d.replace(tzinfo=timezone.utc).astimezone(IST)
    # Published Outlook feeds emit Windows tz names; the feed is the owner's
    # mailbox tz (IST here). Treat any TZID / floating time as IST.
    return d.replace(tzinfo=IST)


def _events(raw: str) -> list[dict]:
    raw = _unfold(raw)
    out = []
    for ev in re.findall(r"BEGIN:VEVENT.*?END:VEVENT", raw, re.S):
        out.append({
            "uid": _prop(ev, "UID") or "?",
            "summary": (_prop(ev, "SUMMARY") or "(no title)").replace("\\,", ",").replace("\\;", ";"),
            "start": _parse_dt(ev, "DTSTART"),
            "end": _parse_dt(ev, "DTEND"),
            "status": (_prop(ev, "STATUS") or "").upper(),
            "recurrence_id": _parse_dt(ev, "RECURRENCE-ID"),
            "rrule": _prop(ev, "RRULE"),
            "exdates": [d for d in re.findall(r"^EXDATE[^:\n]*:(.+)$", ev, re.M)],
            "teams": "teams.microsoft.com" in ev,
            "allday": bool(re.search(r"^DTSTART;VALUE=DATE:", ev, re.M))
                      or "X-MICROSOFT-CDO-ALLDAYEVENT:TRUE" in ev,
        })
    # Outlook marks some cancellations only via a SUMMARY prefix (STATUS stays
    # CONFIRMED on the published feed) — normalize those to CANCELLED.
    for e in out:
        if re.match(r"^cancell?ed:", e["summary"], re.I):
            e["status"] = "CANCELLED"
    return [e for e in out if e["start"]]


def _occurrences(events: list[dict], win_start: datetime, win_end: datetime) -> list[dict]:
    """Expand recurrence into concrete occurrences inside [win_start, win_end)."""
    # Overrides indexed by (uid, original-occurrence-start).
    overrides = {(e["uid"], e["recurrence_id"]): e for e in events if e["recurrence_id"]}
    occs: list[dict] = []

    for e in events:
        if e["recurrence_id"]:
            continue  # handled via its master below (or standalone add later)
        dur = (e["end"] - e["start"]) if e["end"] else timedelta(hours=1)
        if not e["rrule"]:
            if win_start <= e["start"] < win_end and e["status"] != "CANCELLED":
                occs.append(e)
            continue
        # RRULE master: expand. UNTIL inside an rrulestr must stay comparable —
        # dateutil handles Z/naive mixes badly, so normalize UNTIL to UTC basic.
        try:
            rule = du_rrule.rrulestr(e["rrule"], dtstart=e["start"])
            hits = rule.between(win_start, win_end, inc=True)
        except Exception:
            # Un-expandable rule — show the master once if it's in-window.
            if win_start <= e["start"] < win_end:
                occs.append(e)
            continue
        exset = set()
        for exline in e["exdates"]:
            for tok in exline.split(","):
                tok = tok.strip()[:15]
                try:
                    exset.add(datetime.strptime(tok[:8], "%Y%m%d").date())
                except ValueError:
                    pass
        for h in hits:
            if h.date() in exset:
                continue
            ov = overrides.get((e["uid"], h))
            if ov is not None:
                if ov["status"] != "CANCELLED" and win_start <= ov["start"] < win_end:
                    occs.append(ov)
                continue
            if e["status"] == "CANCELLED":
                continue
            occs.append({**e, "start": h, "end": h + dur})

    # Moved/exception instances (RECURRENCE-ID) whose NEW time falls in-window.
    # Append every such override directly; the final dedup drops any that path 1
    # already added via its master. This is REQUIRED for the common Outlook case
    # where the published feed carries ONLY the moved exception, not the master
    # series — e.g. a recurring "Sprint Grooming" instance rescheduled 13:00->17:00
    # the SAME day: path 1 can't fire without a master, and the old guard also
    # skipped it because the original 13:00 slot was still in-window, so the
    # meeting vanished from Steno entirely (2026-07-27).
    for (uid, rid), ov in overrides.items():
        if ov["status"] == "CANCELLED" or ov["start"] is None:
            continue
        if win_start <= ov["start"] < win_end:
            occs.append(ov)

    # Dedup (an override can be appended twice via both paths) + sort.
    seen, uniq = set(), []
    for o in sorted(occs, key=lambda x: x["start"]):
        k = (o["uid"], o["start"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(o)
    return uniq


def _day_window(d: date) -> tuple[datetime, datetime]:
    s = datetime.combine(d, dtime(0, 0), tzinfo=IST)
    return s, s + timedelta(days=1)


def _render(occs: list[dict], skip_allday: bool = True) -> None:
    shown = 0
    for o in occs:
        if skip_allday and o["allday"]:
            continue
        t = f"{o['start'].astimezone(IST):%H:%M}-{o['end'].astimezone(IST):%H:%M}" if o["end"] else f"{o['start'].astimezone(IST):%H:%M}"
        day = f"{o['start'].astimezone(IST):%Y-%m-%d} " if o["start"].astimezone(IST).date() != datetime.now(IST).date() else ""
        print(f"  {day}{t} | {o['summary']} | {'teams' if o['teams'] else 'no-link'} | uid=…{o['uid'][-12:]}")
        shown += 1
    if not shown:
        print("  (no meetings)")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "refresh":
        _fetch(force=True)
        print("OK refreshed")
        return
    raw = _fetch()
    events = _events(raw)
    now = datetime.now(IST)
    if cmd == "today":
        w0, w1 = _day_window(now.date())
        _render(_occurrences(events, w0, w1))
    elif cmd == "day" and len(sys.argv) == 3:
        w0, w1 = _day_window(date.fromisoformat(sys.argv[2]))
        _render(_occurrences(events, w0, w1))
    elif cmd == "upcoming":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        w0, _ = _day_window(now.date())
        _, w1 = _day_window(now.date() + timedelta(days=days - 1))
        _render(_occurrences(events, w0, w1))
    elif cmd == "next":
        _, w1 = _day_window(now.date() + timedelta(days=7))
        fut = [o for o in _occurrences(events, now, w1) if not o["allday"]]
        _render(fut[:1])
    elif cmd == "soon":
        # Machine-readable UPCOMING state for the pre-call record nudge:
        #   SOON|<slug>|<start-epoch>|<end-epoch>|<uid-tail>|<mins-until>|<title>
        # (one line per meeting, soonest first) or NONE. Non-all-day events only
        # (skips Lunch/Busy blocks); includes IN-PERSON meetings (no Teams link)
        # — that is exactly the case the auto-recorder can't detect and the
        # reminder matters most for. Window spans today+tomorrow so a horizon
        # crossing midnight still resolves.
        mins = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        w0, _ = _day_window(now.date())
        _, w1 = _day_window(now.date() + timedelta(days=1))
        horizon = now + timedelta(minutes=mins)
        up = [o for o in _occurrences(events, w0, w1)
              if not o["allday"] and now < o["start"] <= horizon]
        if not up:
            print("NONE")
            return
        up.sort(key=lambda x: x["start"])
        for o in up:
            slug = re.sub(r"[^a-z0-9]+", "-", o["summary"].lower()).strip("-")[:48] or "meeting"
            end = o["end"] or o["start"] + timedelta(hours=1)
            mleft = int((o["start"] - now).total_seconds() // 60)
            print(f"SOON|{slug}|{int(o['start'].timestamp())}|{int(end.timestamp())}|"
                  f"{o['uid'][-12:]}|{mleft}|{o['summary']}")
    elif cmd == "now":
        # Machine-readable current-meeting state for the auto-recorder:
        #   ACTIVE|<slug>|<end-epoch>|<uid-tail>|<title>   or   NONE
        # Teams-linked, non-all-day meetings only (skips Lunch/Busy blocks);
        # 60s early-start grace so capture is rolling when the call begins.
        w0, w1 = _day_window(now.date())
        live = [o for o in _occurrences(events, w0, w1)
                if o["teams"] and not o["allday"]
                and o["start"] - timedelta(seconds=60) <= now < (o["end"] or o["start"] + timedelta(hours=1))]
        if not live:
            print("NONE")
            return
        # Primary guess: latest-starting live event. Overlapping events make
        # this a coin flip — every OTHER live event is emitted as an ALT line
        # so the UI can offer a "wrong meeting?" picker (human disambiguation,
        # same as Granola's per-event notepad choice).
        live.sort(key=lambda x: x["start"], reverse=True)
        for i, o in enumerate(live):
            slug = re.sub(r"[^a-z0-9]+", "-", o["summary"].lower()).strip("-")[:48] or "meeting"
            end = o["end"] or o["start"] + timedelta(hours=1)
            tag = "ACTIVE" if i == 0 else "ALT"
            print(f"{tag}|{slug}|{int(end.timestamp())}|{o['uid'][-12:]}|{o['summary']}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

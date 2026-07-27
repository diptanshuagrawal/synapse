"""derive/meetings/calendar_feed.py — recurrence/override occurrence expansion.

Pins the 2026-07-27 fix: a recurring instance rescheduled the SAME day (Outlook
publishes ONLY the moved RECURRENCE-ID exception, not the master series) must
still surface. It previously vanished — path 1 had no master to expand, and the
out-of-window guard skipped it because the original slot was still in-window.
"""
from __future__ import annotations

from datetime import date

from derive.meetings.calendar_feed import _day_window, _events, _occurrences


def _occ_for(vevents: str, d: date):
    w0, w1 = _day_window(d)
    return _occurrences(_events("BEGIN:VCALENDAR\n" + vevents + "END:VCALENDAR\n"), w0, w1)


MOVED_EXCEPTION = """BEGIN:VEVENT
UID:sprint-grooming-uid
SUMMARY:Sprint Grooming
DTSTART;TZID=India Standard Time:20260727T170000
DTEND;TZID=India Standard Time:20260727T180000
RECURRENCE-ID;TZID=India Standard Time:20260727T130000
STATUS:CONFIRMED
END:VEVENT
"""


def test_same_day_moved_exception_without_master_is_kept():
    # Master series absent (Outlook published only the moved instance); original
    # 13:00 slot still in-window. Must appear at its NEW 17:00 time exactly once.
    occs = _occ_for(MOVED_EXCEPTION, date(2026, 7, 27))
    mine = [o for o in occs if o["uid"] == "sprint-grooming-uid"]
    assert len(mine) == 1, f"expected the moved instance once, got {len(mine)}"
    assert mine[0]["start"].hour == 17 and mine[0]["start"].minute == 0


def test_cancelled_moved_exception_is_dropped():
    cancelled = MOVED_EXCEPTION.replace("STATUS:CONFIRMED", "STATUS:CANCELLED")
    occs = _occ_for(cancelled, date(2026, 7, 27))
    assert not [o for o in occs if o["uid"] == "sprint-grooming-uid"]


def test_plain_single_event_in_window_kept():
    ev = """BEGIN:VEVENT
UID:handover-uid
SUMMARY:Weekly Handover
DTSTART;TZID=India Standard Time:20260727T150000
DTEND;TZID=India Standard Time:20260727T160000
STATUS:CONFIRMED
END:VEVENT
"""
    occs = _occ_for(ev, date(2026, 7, 27))
    assert [o for o in occs if o["uid"] == "handover-uid"]

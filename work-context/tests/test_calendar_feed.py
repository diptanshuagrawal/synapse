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


# --- _fetch hardening: fail LOUD on a dead feed, never cache a non-ICS body ---

import pytest  # noqa: E402
from derive.meetings import calendar_feed as cf  # noqa: E402

_VALID_ICS = ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n"
              "BEGIN:VEVENT\nUID:a\nSUMMARY:One\nEND:VEVENT\n"
              "BEGIN:VEVENT\nUID:b\nSUMMARY:Two\nEND:VEVENT\nEND:VCALENDAR\n")


def _fake_curl(http_code, body):
    """Stand in for the curl subprocess: write `body` to the -o target (as curl
    would), and report `http_code` on stdout (as `-w %{http_code}` does)."""
    import subprocess as sp

    def run(cmd, capture_output=False, text=False, **kw):
        out = cmd[cmd.index("-o") + 1]
        if body is not None:
            with open(out, "w", encoding="utf-8") as f:
                f.write(body)
        return sp.CompletedProcess(cmd, 0, stdout=http_code, stderr="")
    return run


def _wire(monkeypatch, tmp_path, http_code, body):
    cache = tmp_path / "calendar_feed.ics"
    monkeypatch.setattr(cf, "CACHE", cache)
    monkeypatch.setattr(cf, "_ics_url", lambda: "https://example.test/cal.ics")
    monkeypatch.setattr(cf.subprocess, "run", _fake_curl(http_code, body))
    return cache


def test_fetch_success_updates_cache(monkeypatch, tmp_path):
    cache = _wire(monkeypatch, tmp_path, "200", _VALID_ICS)
    out = cf._fetch(force=True, strict=True)
    assert out.startswith("BEGIN:VCALENDAR")
    assert cache.read_text().count("BEGIN:VEVENT") == 2   # written to cache


def test_refresh_302_strict_fails_loud(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, "302", "")               # redirect, empty body
    with pytest.raises(SystemExit) as e:
        cf._fetch(force=True, strict=True)
    msg = str(e.value)
    assert "302" in msg and "FAILED" in msg               # loud + actionable


def test_fetch_302_nonstrict_serves_stale_with_warning(monkeypatch, tmp_path, capsys):
    cache = _wire(monkeypatch, tmp_path, "302", "")
    cache.write_text(_VALID_ICS)                           # pre-existing good cache
    out = cf._fetch(force=True, strict=False)
    assert out.startswith("BEGIN:VCALENDAR")               # falls back to stale
    assert "STALE" in capsys.readouterr().err             # but warns, not silent


def test_error_page_body_is_rejected_not_cached(monkeypatch, tmp_path):
    # HTTP 200 but the body is an HTML error page, not ICS → must NOT be accepted.
    cache = _wire(monkeypatch, tmp_path, "200", "<html>" + "x" * 200 + "</html>")
    cache.write_text(_VALID_ICS)                           # good cache must survive
    out = cf._fetch(force=True, strict=False)
    assert out.startswith("BEGIN:VCALENDAR")               # served good stale, not the HTML
    assert cache.read_text().count("BEGIN:VEVENT") == 2    # cache NOT overwritten

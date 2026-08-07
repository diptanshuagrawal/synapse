#!/usr/bin/env python3
"""
delete_meeting.py — permanently remove a recorded meeting (all its segments).

Usage: delete_meeting.py <stem> [<stem> ...]     (stem = 2026-07-17-<slug>)

For each stem:
  - events.db rows for subject meeting:<date>:<slug> via the shared
    delete_events helper (cascades events + refs + FTS — never a bare DELETE)
  - archived audio/transcript/sidecars: <stem>.{m4a,wav,json,txt,me.*,them.*,
    notes.md,links,people} under transcripts/archive/<month>/
  - the note + its sidecars: management/meetings/<stem>.{md,md.prev,cat,mom*}

Idempotent and best-effort per file. Prints a one-line summary.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ingest.common import get_db, delete_events  # noqa: E402

WC = Path(__file__).resolve().parents[2]
ARCHIVE = WC / "transcripts" / "archive"
INBOX = WC / "transcripts" / "inbox"
HOLD = WC / "transcripts" / "hold"
NOTES = WC.parent / "management" / "meetings"
# transcripts_process derives a meeting's date from the IST date of the file mtime;
# mirror it so raw-audio removal can disambiguate same-slug meetings across days.
IST = timezone(timedelta(hours=5, minutes=30))


def delete_stem(stem: str) -> tuple[int, int]:
    date, slug = stem[:10], stem[11:]
    subject = f"meeting:{date}:{slug}"
    conn = get_db()
    ids = [r["id"] for r in conn.execute("SELECT id FROM events WHERE subject = ?", (subject,))]
    removed = delete_events(conn, ids) if ids else 0

    files = 0
    month = stem[:7]
    for pat in (f"{stem}.*", f"{stem}.me.*", f"{stem}.them.*"):
        for f in (ARCHIVE / month).glob(pat):
            f.unlink(missing_ok=True)
            files += 1
    # also the raw m4a (named <slug>-HHMM.m4a in archive month dir, matched by stem.*)
    for f in NOTES.glob(f"{stem}.*"):
        f.unlink(missing_ok=True)
        files += 1
    # RAW audio keeps its ORIGINAL inbox basename (<slug>, NO date prefix) when it
    # is queued (inbox/hold) or archived (the mixed .m4a keeps its inbox name), so
    # the date-prefixed {stem}.* globs above MISS it. Left behind, the meeting
    # reappears as an untranscribed raw tile (meet_ui lists archive/inbox/hold
    # *.m4a) — or a still-in-inbox file re-sweeps. Remove it by slug everywhere.
    for base in (INBOX, HOLD, ARCHIVE / month):
        for pat in (f"{slug}.m4a", f"{slug}.wav", f"{slug}.me.wav", f"{slug}.them.wav"):
            for f in base.glob(pat):
                # DISAMBIGUATE same-slug meetings across days (e.g. the daily
                # standup-1201): the raw file carries no date, so only remove it
                # when its own IST mtime-date matches THIS meeting's date —
                # otherwise a delete could nuke a different day's queued audio.
                try:
                    fdate = datetime.fromtimestamp(f.stat().st_mtime, IST).strftime("%Y-%m-%d")
                except OSError:
                    continue
                if fdate != date:
                    continue
                f.unlink(missing_ok=True)
                files += 1
    return removed, files


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: delete_meeting.py <stem> [<stem> ...]")
    tot_ev = tot_f = 0
    for stem in sys.argv[1:]:
        stem = "".join(c for c in stem if c.isalnum() or c in "-_")
        ev, f = delete_stem(stem)
        tot_ev += ev
        tot_f += f
        print(f"deleted {stem}: {ev} events, {f} files")
    print(f"OK total: {tot_ev} events, {tot_f} files")


if __name__ == "__main__":
    main()

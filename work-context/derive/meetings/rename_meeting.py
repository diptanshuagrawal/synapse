#!/usr/bin/env python3
"""
rename_meeting.py — relabel a recorded meeting (fix a mislabel).

The auto-recorder names a recording from the calendar/app at capture time and
sometimes guesses wrong (e.g. a Slack huddle overlapping a scheduled call). This
renames everything a meeting owns, atomically enough for a one-click UI fix:

  - archive files       <date>-<old>.*  ->  <date>-<new>.*   (+ the bare <old>.m4a)
  - events.db           re-ingest under the new subject, then delete the old one
  - note                management/meetings/<date>-<old>.md -> <date>-<new>.md

Usage:
  rename_meeting.py --mid <YYYY-MM-DD-oldslug> --to "<new label>"

`--to` is a human label (e.g. "Sanket sync"); it's kebab-sanitized and the
original HHMM suffix is preserved so the time still disambiguates. Prints the
new mid on success.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

WC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WC))
IST = timezone(timedelta(hours=5, minutes=30))

ARCHIVE = WC / "transcripts" / "archive"
NOTES = WC.parent / "management" / "meetings"

MID_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(.+)$")  # date + old-slug
HHMM_RE = re.compile(r"-(\d{4})$")  # trailing HHMM (recorder always writes 4 digits via `date +%H%M`); a non-time trailing number won't match → treated as no time


def _sanitize(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", required=True, help="current mid: YYYY-MM-DD-<slug>")
    ap.add_argument("--to", required=True, help="new human label (kebab-ized)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    m = MID_RE.match(args.mid)
    if not m:
        sys.exit(f"ERROR: --mid not in YYYY-MM-DD-<slug> form: {args.mid}")
    y, mo, d, old_slug = m.groups()
    date = f"{y}-{mo}-{d}"
    month = f"{y}-{mo}"

    base = _sanitize(args.to)
    if not base:
        sys.exit("ERROR: --to reduces to empty after sanitize")
    # Preserve the original HHMM suffix so re-labelled meetings keep their time key.
    hhmm = HHMM_RE.search(old_slug)
    new_slug = f"{base}-{hhmm.group(1)}" if hhmm and not HHMM_RE.search(base) else base
    if new_slug == old_slug:
        sys.exit(f"ERROR: new slug equals old ({old_slug}) — nothing to do")
    new_mid = f"{date}-{new_slug}"

    adir = ARCHIVE / month
    if not adir.is_dir():
        sys.exit(f"ERROR: archive month dir missing: {adir}")

    # Files to move: dated sidecars (<mid>.*) + the bare <old_slug>.m4a (+ its wavs).
    renames: list[tuple[Path, Path]] = []
    for p in sorted(adir.glob(f"{args.mid}.*")):
        renames.append((p, adir / p.name.replace(args.mid, new_mid, 1)))
    for p in sorted(adir.glob(f"{old_slug}.*")):  # bare m4a/wav (no date prefix)
        renames.append((p, adir / p.name.replace(old_slug, new_slug, 1)))
    note = NOTES / f"{args.mid}.md"
    if note.exists():
        renames.append((note, NOTES / f"{new_mid}.md"))

    if not renames:
        sys.exit(f"ERROR: no files found for mid {args.mid} in {adir}")

    print(f"{'DRY ' if args.dry_run else ''}rename {args.mid} -> {new_mid}")
    for src, dst in renames:
        print(f"  {src.name} -> {dst.name}")
        if not args.dry_run:
            if dst.exists():
                sys.exit(f"ERROR: target exists, refusing to clobber: {dst}")
            src.rename(dst)

    if args.dry_run:
        print("(dry-run: events.db untouched)")
        return

    # Re-ingest under the new subject from the (now-renamed) merged json.
    new_json = adir / f"{new_mid}.json"
    if new_json.exists():
        start = datetime.strptime(f"{date} {hhmm.group(1) if hhmm else '0000'}", "%Y-%m-%d %H%M").replace(tzinfo=IST)
        title = args.to.strip()
        r = subprocess.run(
            [sys.executable, str(WC / "derive" / "meetings" / "ingest_transcript.py"),
             "--json", str(new_json), "--slug", new_slug,
             "--start", start.isoformat(), "--title", title],
            capture_output=True, text=True,
        )
        sys.stdout.write(r.stdout)
        if r.returncode != 0:
            sys.exit(f"ERROR: re-ingest failed:\n{r.stderr}")
        # Drop the old subject's events (id embeds the subject, so a rename is a
        # delete+reinsert, not an UPDATE).
        from ingest.common import delete_events, get_db  # noqa: E402
        conn = get_db()
        old_subject = f"meeting:{date}:{old_slug}"
        ids = [row["id"] for row in conn.execute("SELECT id FROM events WHERE subject=?", (old_subject,))]
        removed = delete_events(conn, ids) if ids else 0
        print(f"events.db: new subject meeting:{date}:{new_slug} ingested; old {old_subject} removed ({removed})")
    else:
        print("WARN: no merged .json — files renamed, but events.db not updated (untranscribed meeting)")

    print(new_mid)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
meet_retention.py — audio-retention prune for the meeting-intelligence pipeline.

Reclaims disk by deleting heavy audio the pipeline no longer needs:

  1. Archived meeting audio (transcripts/archive/<month>/*.m4a) older than the
     retention window (default 14 days).
  2. Orphaned raw capture streams (.wav) the recorder / processor left behind
     (dual-stream speaker halves, crash-rescued buffers, dead capture files).

Transcripts, notes, links, .people/.cat sidecars, and every events.db row are
NEVER touched — only the audio is reclaimed. Transcripts stay searchable.

    python3 bin/meet_retention.py            # dry-run: report only (default)
    python3 bin/meet_retention.py --apply    # actually delete
    python3 bin/meet_retention.py --days 30  # override the 14-day window

STAR EXEMPTION
--------------
A meeting the owner starred in the Steno UI (the ★ toggle) writes a sidecar
    management/meetings/<stem>.star
Starred meetings keep their audio FOREVER: the prune skips them regardless of
age and reports the skip count explicitly, so "why is this old audio still
here?" is always answerable (answer: it is starred).

The archived .m4a keeps its original inbox basename (no date prefix); the star
sidecar / transcript use the date-prefixed meeting id. We reconstruct that id
from the .m4a the same way transcripts_process.sh did — IST date of the file's
mtime + slugified stem — so the star lookup matches exactly.

Local-only: reads/deletes only under work-context/transcripts/, reads the star
sidecars read-only. No network, no events.db writes.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WC = REPO / "work-context"
CAP = WC / "transcripts" / ".capture"
ARCHIVE = WC / "transcripts" / "archive"
INBOX = WC / "transcripts" / "inbox"
NOTES_DIR = REPO / "management" / "meetings"
PIDF = CAP / "pid"

RETAIN_DAYS = 14
# transcripts_process.sh derives the archive date with `TZ=Asia/Kolkata`; mirror
# it so the reconstructed meeting id matches the transcript stem / star sidecar.
IST = timezone(timedelta(hours=5, minutes=30))


def _slug(stem: str) -> str:
    """Port of transcripts_process.sh's slug rule (tr a-z + squeeze non-alnum)."""
    s = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return s or "meeting"


def _meeting_stem(m4a: Path) -> str:
    """Meeting id for an archived .m4a: <IST-date-of-mtime>-<slug>.

    Reproduces the `prefix` transcripts_process.sh built, which is exactly the
    id meet_ui uses for /api/star/<mid> → <stem>.star. mv preserves mtime, so
    the file's mtime is still its process-time date.
    """
    d = datetime.fromtimestamp(m4a.stat().st_mtime, IST).strftime("%Y-%m-%d")
    return f"{d}-{_slug(m4a.stem)}"


def _is_starred(stem: str) -> bool:
    return (NOTES_DIR / f"{stem}.star").exists()


def _recording() -> bool:
    """Is a recording live? Mirrors meet-record's is_recording (pid alive)."""
    if not PIDF.exists():
        return False
    try:
        pid = int(PIDF.read_text().split()[0])
    except (ValueError, IndexError, OSError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # process exists, owned by someone else
        return True
    except OSError:
        return False


def _mb(nbytes: int) -> str:
    return f"{nbytes / 1_048_576:.1f} MB"


def prune_archive(cutoff: float, apply: bool, log) -> tuple[int, int, int]:
    """Delete archived .m4a older than cutoff, skipping starred meetings.

    Returns (deleted, skipped_starred, freed_bytes).
    """
    deleted = skipped_starred = freed = 0
    if not ARCHIVE.exists():
        return 0, 0, 0
    for m4a in sorted(ARCHIVE.glob("*/*.m4a")):
        try:
            st = m4a.stat()
        except FileNotFoundError:
            continue
        if st.st_mtime > cutoff:  # newer than the window → keep
            continue
        stem = _meeting_stem(m4a)
        rel = m4a.relative_to(WC)
        if _is_starred(stem):
            skipped_starred += 1
            log(f"  SKIP  starred ★  {rel}  (id={stem})")
            continue
        age_d = int((time.time() - st.st_mtime) / 86400)
        log(f"  {'DELETE' if apply else 'WOULD DELETE'}  {rel}  ({age_d}d, {_mb(st.st_size)})")
        if apply:
            try:
                m4a.unlink()
            except OSError as e:
                log(f"  ERROR  could not delete {rel}: {e}")
                continue
        deleted += 1
        freed += st.st_size
    return deleted, skipped_starred, freed


def prune_orphan_wavs(cutoff: float, apply: bool, log) -> tuple[int, int]:
    """Sweep stranded raw .wav streams older than cutoff.

    Only throwaway/stranded raw streams are targeted — a standalone .wav sitting
    in the inbox is a real (un-transcribed) recording and is left untouched:

      * <stem>.me.wav / <stem>.them.wav  — dual-stream speaker halves whose
        merge failed or whose .m4a was already archived (transcripts_process
        only clears them while processing the sibling .m4a).
      * rescued-*.wav                    — crash-recovered capture buffers.
      * .capture/sys.wav, .capture/mic.wav — dead capture files, only when no
        recording is currently live.

    Orphan .wav cleanup is star-INDEPENDENT: the .m4a is the audio archive the
    star protects; these raw halves are redundant/stranded intermediates, so
    removing them never loses a starred meeting's audio.

    Returns (removed, freed_bytes).
    """
    removed = freed = 0

    if INBOX.exists():
        for wav in sorted(INBOX.glob("*.wav")):
            name = wav.name
            is_half = name.endswith(".me.wav") or name.endswith(".them.wav")
            is_rescue = name.startswith("rescued-")
            if not (is_half or is_rescue):
                continue  # standalone .wav = a real recording still to process
            try:
                st = wav.stat()
            except FileNotFoundError:
                continue
            if st.st_mtime > cutoff:  # still fresh → pipeline may yet use it
                continue
            kind = "half" if is_half else "rescue-stream"
            log(f"  {'REMOVE' if apply else 'WOULD REMOVE'}  orphan {kind}  "
                f"{wav.relative_to(WC)}  ({_mb(st.st_size)})")
            if apply:
                try:
                    wav.unlink()
                except OSError as e:
                    log(f"  ERROR  could not remove {wav.relative_to(WC)}: {e}")
                    continue
            removed += 1
            freed += st.st_size

    if not _recording():
        for wav in (CAP / "sys.wav", CAP / "mic.wav"):
            if not wav.exists():
                continue
            st = wav.stat()
            if st.st_mtime > cutoff:
                continue
            log(f"  {'REMOVE' if apply else 'WOULD REMOVE'}  dead capture  "
                f"{wav.relative_to(WC)}  ({_mb(st.st_size)})")
            if apply:
                try:
                    wav.unlink()
                except OSError as e:
                    log(f"  ERROR  could not remove {wav.relative_to(WC)}: {e}")
                    continue
            removed += 1
            freed += st.st_size

    return removed, freed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Steno audio-retention prune.")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default: dry-run, report only)")
    ap.add_argument("--days", type=int, default=RETAIN_DAYS,
                    help=f"retention window in days (default {RETAIN_DAYS})")
    args = ap.parse_args(argv)

    cutoff = time.time() - args.days * 86400
    lines: list[str] = []
    log = lines.append

    log(f"audio-retention prune — window={args.days}d  "
        f"mode={'APPLY' if args.apply else 'DRY-RUN'}")
    log("archived audio (.m4a):")
    deleted, skipped, m4a_freed = prune_archive(cutoff, args.apply, log)
    log("orphan raw streams (.wav):")
    removed, wav_freed = prune_orphan_wavs(cutoff, args.apply, log)

    log("")
    verb = "deleted" if args.apply else "would delete"
    log(f"SUMMARY  m4a {verb}={deleted}  skipped-starred={skipped}  "
        f"orphan-wav {'removed' if args.apply else 'would remove'}={removed}  "
        f"freed={_mb(m4a_freed + wav_freed)}")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())

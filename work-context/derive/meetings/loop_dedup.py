#!/usr/bin/env python3
"""loop_dedup.py — deterministic whisper repetition-loop collapse.

Context-carry decoding (needed for the vocab prompt in transcribe.sh) can make
whisper latch into a repetition loop that repeats one line dozens/hundreds of
times ("No, I am going to dump it." x148). transcribe.sh already collapses this
in the display `.txt`, but the JSON that merge_streams + ingest_transcript
consume is a SEPARATE artifact — an uncollapsed loop survived into the merged
transcript AND into events.db. This module is the ONE place the collapse lives
so the `.txt`, the merged `.json`, and the events.db chunks are all deduped with
identical semantics.

Two guards, mirroring transcribe.sh's awk passes:
  * consecutive-dup  — keep at most CONSECUTIVE_MAX identical lines in a row
    (>2 consecutive identical lines is never real speech).
  * total-cap        — cap any identical line to TOTAL_MAX occurrences overall,
    which catches alternating A/B/A/B loops the consecutive guard misses.

Constants are kept in sync (by hand) with the awk collapses in bin/transcribe.sh
— change both together.
"""

from __future__ import annotations

# Keep in sync with the awk collapses in bin/transcribe.sh:
#   consecutive:  awk 'prev==$0 {c++; if (c<2) print ...}'   -> keep first 2
#   total-cap:    awk '{ if (++seen[$0] <= 6) print }'       -> cap at 6
CONSECUTIVE_MAX = 2  # keep at most this many identical lines in a row
TOTAL_MAX = 6        # cap any identical line to this many occurrences overall


class LoopCollapser:
    """Stateful per-transcript whisper loop collapser.

    Feed each segment's final text (post-strip, post-hallucination-filter,
    post-correction) to :meth:`keep`; it returns ``False`` for lines that a
    repetition loop produced and should be dropped. One instance per transcript
    stream — a loop is contained within a single whisper output, so dual-stream
    callers use a fresh collapser per stream.
    """

    def __init__(self) -> None:
        self._prev: str | None = None
        self._run = 0
        self._seen: dict[str, int] = {}

    def keep(self, text: str) -> bool:
        # Consecutive-dup guard first (matches transcribe.sh, which runs the
        # consecutive collapse BEFORE the total cap): lines dropped here do NOT
        # count toward the total cap.
        if text == self._prev:
            self._run += 1
            if self._run >= CONSECUTIVE_MAX:
                return False
        else:
            self._prev, self._run = text, 0
        n = self._seen.get(text, 0) + 1
        self._seen[text] = n
        return n <= TOTAL_MAX

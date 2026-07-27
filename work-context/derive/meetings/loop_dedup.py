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

import re
from collections import Counter

# Keep in sync with the awk collapses in bin/transcribe.sh:
#   consecutive:  awk 'prev==$0 {c++; if (c<2) print ...}'   -> keep first 2
#   total-cap:    awk '{ if (++seen[$0] <= 6) print }'       -> cap at 6
CONSECUTIVE_MAX = 2  # keep at most this many identical lines in a row
TOTAL_MAX = 6        # cap any identical line to this many occurrences overall

# Intra-line word-loop guard: a SINGLE segment dominated by one repeated token is
# whisper's word-level hallucination loop ("सब्सक्राइब सब्सक्राइब …" x14, "जुड़े
# जुड़े …" x24, "look look look …"). Distinct from the line-level dup guards above
# — that's repeated SEGMENTS; this is one segment that is mostly one word.
# Language-agnostic (counts whitespace tokens, no word list) so it catches
# Devanagari caption-junk the English HALLU_RE regex can't.
WORD_LOOP_MIN_REPEAT = 6   # top token must repeat at least this many times ...
WORD_LOOP_SHARE = 0.5      # ... and be at least half the line's tokens
_TOKEN_RE = re.compile(r"\S+")


def is_word_loop(text: str) -> bool:
    toks = _TOKEN_RE.findall(text)
    if len(toks) < WORD_LOOP_MIN_REPEAT:
        return False
    _, n = Counter(toks).most_common(1)[0]
    return n >= WORD_LOOP_MIN_REPEAT and n >= WORD_LOOP_SHARE * len(toks)


# Intra-line PHRASE-loop guard: whisper decoded without VAD (the diarization path
# needs real-time timestamps) latches into a repeating PHRASE, not a single token
# — "यह तो पूरी टीम है यह तो पूरी टीम है …" ×15. The token varies segment to
# segment (drift), so neither is_word_loop (one dominant token) nor the exact-dup
# line guards catch it. But a genuine 12+-token utterance has high lexical
# diversity; a phrase-loop reuses a tiny vocabulary. So: a long-enough line whose
# DISTINCT/total token ratio is very low is a loop. Language-agnostic; the min-
# token floor keeps short real filler ("haan haan theek hai") safe.
PHRASE_LOOP_MIN_TOKENS = 12    # only judge diversity once the line is long enough
PHRASE_LOOP_DISTINCT_RATIO = 0.35  # distinct/total below this = a repetition loop


def is_phrase_loop(text: str) -> bool:
    toks = _TOKEN_RE.findall(text)
    if len(toks) < PHRASE_LOOP_MIN_TOKENS:
        return False
    return len(set(toks)) / len(toks) < PHRASE_LOOP_DISTINCT_RATIO


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
        # Intra-line word/phrase loops → pure hallucination, drop outright (before
        # the line-level guards, which only see repeated whole segments).
        if is_word_loop(text) or is_phrase_loop(text):
            return False
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

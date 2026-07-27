#!/usr/bin/env python3
"""
merge_streams.py — merge per-speaker whisper transcripts into one timeline.

The recorder captures the owner's mic and system audio (everyone else) as
SEPARATE streams. Transcribing each separately gives ground-truth speaker
separation for free — no diarization model:

    Me:   <line from the mic stream>
    Them: <line from the system-audio stream>

Usage (dual-stream CALL): merge_streams.py --me <me.json> --them <them.json> --out <prefix>

On a CALL the system-audio ('them') stream is CLEAN digital audio (no room echo)
mixing ALL remote/in-room participants. Diarizing it splits the far side into
distinct voices instead of one lumped `Them`:

    Me:               <line from the mic stream>
    Them · Speaker 1: <line from far-side voice A>
    Them · Speaker 2: <line from far-side voice B>

Usage (dual-stream CALL, far-side diarized): merge_streams.py --me <me.json> \
    --them <them.json> --them-diarize <them.diar.json> --out <prefix>
  (no/empty --them-diarize → flat `Them:`, today's dual-stream behavior).

For IN-PERSON meetings everyone is on the ONE room mic and the system-audio
stream is silent, so the Me:/Them: trick gives no separation. There a pyannote
diarization pass (derive/meetings/diarize.py) supplies WHO-spoke-WHEN turns, and
this maps each whisper segment onto the dominant turn by timestamp overlap:

    Speaker 1: <line>
    Speaker 2: <line>

Usage (single-mic DIARIZED): merge_streams.py --single <whisper.json> \
    [--diarize <turns.json>] --out <prefix>
  (no/empty --diarize → labels are omitted, i.e. today's plain transcript).

All modes write <prefix>.json (whisper-shaped, texts prefixed) + <prefix>.txt.

Caveat (documented for the notes skill): on SPEAKERS the mic also hears the
remote side, so a "Me:" line may duplicate a "Them:" line — with earphones
(the owner's norm) the separation is clean. Speaker N numbering is arbitrary
per meeting (Speaker 1 = whoever spoke first); it is NOT a name.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Callable

# Stream labels. `Me` is the owner (the mic stream) — never a diarized cluster.
# `Them` is the far side; diarized it becomes `Them · Speaker N`.
ME = "Me"
THEM = "Them"

# Whisper silence-hallucination artifacts (mirror of ingest_transcript.HALLU_RE).
HALLU_RE = re.compile(
    r"www\.|https?://|\.org(\.au)?|thanks for watching|please subscribe|सब्सक्राइ|"
    r"subtitles?\s+(by|provided)|amara\.org|for more information,?\s+visit|fema\.gov",
    re.I,
)

try:
    from correct import correct_text as _correct
except Exception:
    def _correct(s: str) -> str:
        return s

try:
    from loop_dedup import LoopCollapser  # run as script: sibling on sys.path[0]
except ImportError:
    from derive.meetings.loop_dedup import LoopCollapser  # imported as package (pytest)


def _load_json(path: str) -> dict:
    # whisper-cli can emit invalid UTF-8 on noisy/garbled audio — decode
    # tolerantly (replace bad bytes) so the whole merge doesn't crash and drop
    # the recording. A single mangled char beats a lost meeting.
    with open(path, encoding="utf-8", errors="replace") as f:
        return json.load(f)


def _load_turns(diar_path: str | None) -> list[dict]:
    """Read the `turns` list from a diarization json; [] on missing/soft-fail."""
    if not diar_path:
        return []
    try:
        with open(diar_path, encoding="utf-8", errors="replace") as f:
            return (json.load(f) or {}).get("turns", []) or []
    except Exception:
        return []


def _overlap_tagger(
    turns: list[dict], label_fmt: Callable[[int], str], fallback: str | None
) -> Callable[[int, int], str | None]:
    """Build `tag_for(frm, to)` mapping a whisper segment onto a speaker label.

    Each segment is assigned the diarization turn with the greatest timestamp
    overlap; a segment in a diarization gap takes the nearest turn by start.
    Clusters are numbered by first appearance in the TURNS (identical to
    voice_gallery._display_numbering) so the transcript labels line up with the
    speakers.json sidecar the UI + notes read. `label_fmt(n)` renders the Nth
    speaker. When `turns` is empty every segment gets `fallback` (None = emit
    the text UNLABELLED), so a soft-failed diarizer degrades cleanly.
    """
    display: dict[str, str] = {}
    for t in sorted(turns, key=lambda x: (int(x["start_ms"]), int(x["end_ms"]))):
        c = t["speaker"]
        if c not in display:
            display[c] = label_fmt(len(display) + 1)

    def tag_for(frm: int, to: int) -> str | None:
        if not turns:
            return fallback
        best, best_ov = None, 0
        for t in turns:
            ov = min(to, int(t["end_ms"])) - max(frm, int(t["start_ms"]))
            if ov > best_ov:
                best_ov, best = ov, t["speaker"]
        if best is None:  # segment sits in a diarization gap → nearest turn by start
            best = min(turns, key=lambda t: abs(int(t["start_ms"]) - frm))["speaker"]
        return display.get(best, best)

    return tag_for


def _tag_segments(data: dict, tag_for: Callable[[int, int], str | None]) -> list[dict]:
    """Filter hallucinations, collapse whisper loops, and prefix each surviving
    segment with `tag_for`'s label (unlabelled when it returns None)."""
    out: list[dict] = []
    collapser = LoopCollapser()  # collapse whisper repetition loops in THIS stream
    for seg in data.get("transcription", []):
        text = (seg.get("text") or "").strip()
        if not text or HALLU_RE.search(text):
            continue
        text = _correct(text)
        if not collapser.keep(text):
            continue
        offs = seg.get("offsets") or {}
        frm, to = int(offs.get("from", 0)), int(offs.get("to", 0))
        tag = tag_for(frm, to)
        out.append({
            "timestamps": seg.get("timestamps", {}),
            "offsets": {"from": frm, "to": to},
            "text": f"{tag}: {text}" if tag else text,
        })
    return out


def load(path: str, tag: str) -> list[dict]:
    """Flat single-stream load: every segment prefixed `<tag>:` (Me / Them)."""
    return _tag_segments(_load_json(path), lambda frm, to: tag)


def load_diarized(single_path: str, diar_path: str | None) -> list[dict]:
    """Tag one single-mic whisper transcript with `Speaker N:` from a diarization
    turns file (in-person path). No/empty turns → text emitted UNLABELLED."""
    tagger = _overlap_tagger(_load_turns(diar_path), lambda n: f"Speaker {n}", None)
    return _tag_segments(_load_json(single_path), tagger)


def load_them_diarized(them_path: str, diar_path: str | None) -> list[dict]:
    """Split the far-side (system-audio) stream into `Them · Speaker N:` using a
    diarization turns file. No/empty turns (diarizer soft-failed, or a lone voice
    that produced no turns) → flat `Them:`, so the dual-stream call never regresses."""
    tagger = _overlap_tagger(
        _load_turns(diar_path), lambda n: f"{THEM} · Speaker {n}", THEM
    )
    return _tag_segments(_load_json(them_path), tagger)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--me", help="mic-stream whisper json (dual-stream CALL)")
    ap.add_argument("--them", help="system-audio whisper json (dual-stream CALL)")
    ap.add_argument("--them-diarize", dest="them_diarize",
                    help="diarization turns json for --them (splits the far side)")
    ap.add_argument("--single", help="single whisper json to diarize-label (in-person)")
    ap.add_argument("--diarize", help="diarization turns json for --single (optional)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.single:
        merged = load_diarized(args.single, args.diarize)
    elif args.me and args.them:
        them = (load_them_diarized(args.them, args.them_diarize)
                if args.them_diarize else load(args.them, THEM))
        merged = load(args.me, ME) + them
    else:
        ap.error("provide --me/--them (dual-stream) or --single (diarized)")
    merged.sort(key=lambda s: s["offsets"]["from"])

    with open(f"{args.out}.json", "w", encoding="utf-8") as f:
        json.dump({"transcription": merged}, f)
    with open(f"{args.out}.txt", "w", encoding="utf-8") as f:
        for seg in merged:
            f.write(f" {seg['text']}\n")
    print(f"OK merged {len(merged)} segments -> {args.out}.json")


if __name__ == "__main__":
    main()

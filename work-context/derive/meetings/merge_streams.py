#!/usr/bin/env python3
"""
merge_streams.py — merge per-speaker whisper transcripts into one timeline.

The recorder captures the owner's mic and system audio (everyone else) as
SEPARATE streams. Transcribing each separately gives ground-truth speaker
separation for free — no diarization model:

    Me:   <line from the mic stream>
    Them: <line from the system-audio stream>

Usage (dual-stream CALL): merge_streams.py --me <me.json> --them <them.json> --out <prefix>

For IN-PERSON meetings everyone is on the ONE room mic and the system-audio
stream is silent, so the Me:/Them: trick gives no separation. There a pyannote
diarization pass (derive/meetings/diarize.py) supplies WHO-spoke-WHEN turns, and
this maps each whisper segment onto the dominant turn by timestamp overlap:

    Speaker 1: <line>
    Speaker 2: <line>

Usage (single-mic DIARIZED): merge_streams.py --single <whisper.json> \
    [--diarize <turns.json>] --out <prefix>
  (no/empty --diarize → labels are omitted, i.e. today's plain transcript).

Both modes write <prefix>.json (whisper-shaped, texts prefixed) + <prefix>.txt.

Caveat (documented for the notes skill): on SPEAKERS the mic also hears the
remote side, so a "Me:" line may duplicate a "Them:" line — with earphones
(the owner's norm) the separation is clean. Speaker N numbering is arbitrary
per meeting (Speaker 1 = whoever spoke first); it is NOT a name.
"""

from __future__ import annotations

import argparse
import json
import re

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


def load(path: str, tag: str) -> list[dict]:
    # whisper-cli can emit invalid UTF-8 on noisy/garbled audio — decode
    # tolerantly (replace bad bytes) so the whole dual-stream merge doesn't
    # crash and drop the recording. A single mangled char beats a lost meeting.
    with open(path, encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    out = []
    collapser = LoopCollapser()  # collapse whisper loops in THIS stream's json
    for seg in data.get("transcription", []):
        text = (seg.get("text") or "").strip()
        if not text or HALLU_RE.search(text):
            continue
        text = _correct(text)
        if not collapser.keep(text):
            continue
        offs = seg.get("offsets") or {}
        out.append({
            "timestamps": seg.get("timestamps", {}),
            "offsets": {"from": int(offs.get("from", 0)), "to": int(offs.get("to", 0))},
            "text": f"{tag}: {text}",
        })
    return out


def load_diarized(single_path: str, diar_path: str | None) -> list[dict]:
    """Tag one whisper transcript with `Speaker N:` from a diarization turns file.

    Each whisper segment is assigned the speaker with the greatest timestamp
    overlap; segments falling in a diarization gap take the nearest turn by start.
    Speaker labels are numbered by first appearance in time (Speaker 1 = whoever
    spoke first) — arbitrary per meeting, never a name. If turns are missing or
    empty the text is emitted UNLABELLED (today's plain single-stream transcript),
    so a soft-failed diarizer degrades cleanly.
    """
    with open(single_path, encoding="utf-8", errors="replace") as f:
        data = json.load(f)

    turns: list[dict] = []
    if diar_path:
        try:
            with open(diar_path, encoding="utf-8", errors="replace") as f:
                turns = (json.load(f) or {}).get("turns", []) or []
        except Exception:
            turns = []

    # Number speakers by first appearance in the diarization TURNS (identical to
    # voice_gallery._display_numbering) so the transcript's `Speaker N` labels
    # line up with the speakers.json sidecar the UI + notes read.
    cluster_display: dict[str, str] = {}
    for t in sorted(turns, key=lambda x: (int(x["start_ms"]), int(x["end_ms"]))):
        c = t["speaker"]
        if c not in cluster_display:
            cluster_display[c] = f"Speaker {len(cluster_display) + 1}"

    def tag_for(frm: int, to: int) -> str | None:
        if not turns:
            return None
        best, best_ov = None, 0
        for t in turns:
            ov = min(to, int(t["end_ms"])) - max(frm, int(t["start_ms"]))
            if ov > best_ov:
                best_ov, best = ov, t["speaker"]
        if best is None:  # whisper seg sits in a diarization gap → nearest turn
            best = min(turns, key=lambda t: abs(int(t["start_ms"]) - frm))["speaker"]
        return cluster_display.get(best, best)

    out: list[dict] = []
    collapser = LoopCollapser()  # consecutive-dup + total-cap loop collapse
    for seg in data.get("transcription", []):
        text = (seg.get("text") or "").strip()
        if not text or HALLU_RE.search(text):
            continue
        text = _correct(text)
        # Collapse whisper loop artifacts (the single-stream path doesn't get
        # transcribe.sh's awk dedup on this rewrite of the json).
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--me", help="mic-stream whisper json (dual-stream CALL)")
    ap.add_argument("--them", help="system-audio whisper json (dual-stream CALL)")
    ap.add_argument("--single", help="single whisper json to diarize-label (in-person)")
    ap.add_argument("--diarize", help="diarization turns json for --single (optional)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.single:
        merged = load_diarized(args.single, args.diarize)
    elif args.me and args.them:
        merged = load(args.me, "Me") + load(args.them, "Them")
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

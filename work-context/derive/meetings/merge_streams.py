#!/usr/bin/env python3
"""
merge_streams.py — merge per-speaker whisper transcripts into one timeline.

The recorder captures the owner's mic and system audio (everyone else) as
SEPARATE streams. Transcribing each separately gives ground-truth speaker
separation for free — no diarization model:

    Me:   <line from the mic stream>
    Them: <line from the system-audio stream>

Usage: merge_streams.py --me <me.json> --them <them.json> --out <prefix>
Writes <prefix>.json (whisper-shaped, texts prefixed) + <prefix>.txt.

Caveat (documented for the notes skill): on SPEAKERS the mic also hears the
remote side, so a "Me:" line may duplicate a "Them:" line — with earphones
(the owner's norm) the separation is clean.
"""

from __future__ import annotations

import argparse
import json
import re

# Whisper silence-hallucination artifacts (mirror of ingest_transcript.HALLU_RE).
HALLU_RE = re.compile(
    r"www\.|https?://|\.org(\.au)?|thanks for watching|please subscribe|"
    r"subtitles?\s+(by|provided)|amara\.org|for more information,?\s+visit|fema\.gov",
    re.I,
)

try:
    from correct import correct_text as _correct
except Exception:
    def _correct(s: str) -> str:
        return s


def load(path: str, tag: str) -> list[dict]:
    # whisper-cli can emit invalid UTF-8 on noisy/garbled audio — decode
    # tolerantly (replace bad bytes) so the whole dual-stream merge doesn't
    # crash and drop the recording. A single mangled char beats a lost meeting.
    with open(path, encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    out = []
    for seg in data.get("transcription", []):
        text = (seg.get("text") or "").strip()
        if not text or HALLU_RE.search(text):
            continue
        text = _correct(text)
        offs = seg.get("offsets") or {}
        out.append({
            "timestamps": seg.get("timestamps", {}),
            "offsets": {"from": int(offs.get("from", 0)), "to": int(offs.get("to", 0))},
            "text": f"{tag}: {text}",
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--me", required=True)
    ap.add_argument("--them", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    merged = load(args.me, "Me") + load(args.them, "Them")
    merged.sort(key=lambda s: s["offsets"]["from"])

    with open(f"{args.out}.json", "w", encoding="utf-8") as f:
        json.dump({"transcription": merged}, f)
    with open(f"{args.out}.txt", "w", encoding="utf-8") as f:
        for seg in merged:
            f.write(f" {seg['text']}\n")
    print(f"OK merged {len(merged)} segments -> {args.out}.json")


if __name__ == "__main__":
    main()

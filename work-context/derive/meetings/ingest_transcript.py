#!/usr/bin/env python3
"""
ingest_transcript.py — write a whisper transcript into events.db (source=meeting).

Usage:
  python3 ingest_transcript.py --json <whisper.json> --slug <slug> \
      [--start <ISO8601>] [--title <title>] [--dry-run]

Design (mirrors the other ingest scripts):
  - subject       meeting:<date>:<slug>  (date derived from --start, IST)
  - events        one `transcript_segment` per ~1200-char chunk of consecutive
                  whisper segments (chunking keeps FTS + embedding granularity
                  in line with slack messages instead of one 30 KB blob), plus
                  one `meeting_recorded` meta event carrying the header.
  - actor         the owner (org.owner_email from config/sources.yaml) — the
                  recorder. Per-speaker attribution is a synthesis-time concern
                  (/meeting-notes marks uncertain speakers unattributed); the
                  DB row records who captured it, which is always known.
  - re-ingest     idempotent: all prior events for the subject are removed via
                  the shared delete_events helper (events + refs + FTS cascade)
                  before insert, so a re-run never duplicates or orphans.
  - refs          enrich_refs() picks tickets / PRs / pages out of the spoken
                  text (people mentioned by name are NOT guessed into refs).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ingest.common import (  # noqa: E402
    Event,
    append_raw,
    delete_events,
    enrich_refs,
    get_db,
    insert_event,
)

IST = timezone(timedelta(hours=5, minutes=30))
CHUNK_TARGET = 1200  # chars of transcript text per event

# Whisper silence-hallucination artifacts (shared with merge_streams.py).
HALLU_RE = re.compile(
    r"www\.|https?://|\.org(\.au)?|thanks for watching|please subscribe|सब्सक्राइ|"
    r"subtitles?\s+(by|provided)|amara\.org|for more information,?\s+visit|fema\.gov",
    re.I,
)

try:
    from correct import correct_text as _correct
except Exception:  # correction is best-effort — never block ingest on it
    def _correct(s: str) -> str:
        return s

try:
    from loop_dedup import LoopCollapser  # run as script: sibling on sys.path[0]
except ImportError:
    from derive.meetings.loop_dedup import LoopCollapser  # imported as package (pytest)


def _load_owner_email() -> str:
    import yaml

    cfg = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"
    with open(cfg) as f:
        data = yaml.safe_load(f) or {}
    email = (data.get("org") or {}).get("owner_email")
    if not email:
        sys.exit("ERROR: org.owner_email missing from config/sources.yaml")
    return email


def _person_refs(json_path: Path, owner_email: str) -> list[str]:
    """Canonical person refs for this meeting: the owner (always attended —
    it's his recorder) + the manual participants sidecar (<stem>.people),
    resolved via people.yaml names. Links meetings into the person graph so
    /ask, /pulse and the standup see meeting participation."""
    import yaml

    try:
        people = (yaml.safe_load(open(Path(__file__).resolve().parents[2] / "config" / "people.yaml")) or {}).get("people", [])
    except Exception:
        return []
    by_name: dict[str, str] = {}
    owner_canon = None
    for p in people:
        canon = p.get("canonical")
        if not canon:
            continue
        nm = (p.get("name") or "").strip().lower()
        if nm:
            by_name[nm] = canon
            by_name.setdefault(nm.split()[0], canon)
        if p.get("email") == owner_email:
            owner_canon = canon
    refs = {owner_canon} if owner_canon else set()
    sidecar = json_path.parent / (json_path.name[: -len(".json")] + ".people")
    if sidecar.exists():
        for line in sidecar.read_text().splitlines():
            canon = by_name.get(line.strip().lower())
            if canon:
                refs.add(canon)
    return sorted(refs)


def _fmt_offset(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def _read_segments(whisper_json: Path) -> list[dict]:
    """Whisper.cpp -oj output → [{from_ms, to_ms, text}], empty text dropped."""
    with open(whisper_json) as f:
        data = json.load(f)
    out = []
    collapser = LoopCollapser()  # consecutive-dup + total-cap loop collapse
    for seg in data.get("transcription", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        # Drop Whisper silence-hallucination artifacts (YouTube/caption junk)
        # that leak past the silence gate — never real bank-meeting speech.
        if HALLU_RE.search(text):
            continue
        text = _correct(text)  # fuzzy-fix rare names/jargon (garbled name → real teammate)
        # Collapse whisper repetition loops (same line x18..x148 on looped audio)
        # so events.db chunks never carry the loop — shared with merge_streams +
        # transcribe.sh's .txt collapse.
        if not collapser.keep(text):
            continue
        offs = seg.get("offsets") or {}
        out.append(
            {
                "from_ms": int(offs.get("from", 0)),
                "to_ms": int(offs.get("to", 0)),
                "text": text,
            }
        )
    return out


def _chunk(segments: list[dict]) -> list[dict]:
    """Greedy-pack consecutive segments into ~CHUNK_TARGET-char chunks.

    Each chunk keeps its start/end offsets and renders lines as
    `[mm:ss] text` so quotes in notes can cite a position in the audio.
    """
    chunks: list[dict] = []
    cur_lines: list[str] = []
    cur_len = 0
    cur_from = cur_to = 0
    for seg in segments:
        line = f"[{_fmt_offset(seg['from_ms'])}] {seg['text']}"
        if cur_lines and cur_len + len(line) > CHUNK_TARGET:
            chunks.append({"from_ms": cur_from, "to_ms": cur_to, "text": "\n".join(cur_lines)})
            cur_lines, cur_len = [], 0
        if not cur_lines:
            cur_from = seg["from_ms"]
        cur_lines.append(line)
        cur_len += len(line) + 1
        cur_to = seg["to_ms"]
    if cur_lines:
        chunks.append({"from_ms": cur_from, "to_ms": cur_to, "text": "\n".join(cur_lines)})
    return chunks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True, help="whisper-cli -oj output file")
    ap.add_argument("--slug", required=True, help="meeting slug (kebab-case)")
    ap.add_argument("--start", help="meeting start ISO8601 (default: audio file mtime unavailable here — now)")
    ap.add_argument("--title", help="human meeting title (default: slug)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    slug = re.sub(r"[^a-z0-9-]+", "-", args.slug.lower()).strip("-")
    if not slug:
        sys.exit("ERROR: slug reduces to empty after sanitization")

    start = (
        datetime.fromisoformat(args.start.replace("Z", "+00:00"))
        if args.start
        else datetime.now(timezone.utc)
    )
    date_ist = start.astimezone(IST).strftime("%Y-%m-%d")
    subject = f"meeting:{date_ist}:{slug}"
    title = args.title or slug.replace("-", " ")
    owner = _load_owner_email()

    segments = _read_segments(Path(args.json))
    if not segments:
        sys.exit(f"ERROR: no transcription segments in {args.json}")
    chunks = _chunk(segments)
    duration = _fmt_offset(segments[-1]["to_ms"])

    conn = get_db()

    # Re-ingest safety: drop every prior event for this subject first.
    prior = [r["id"] for r in conn.execute("SELECT id FROM events WHERE subject = ?", (subject,))]
    removed = delete_events(conn, prior) if prior else 0

    events: list[Event] = []
    meta = Event(
        id=f"{subject}:meta",
        source="meeting",
        event_type="meeting_recorded",
        ts=start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        actor=owner,
        subject=subject,
        title=title,
        body=f"Meeting recording ingested: {title} — duration {duration}, "
        f"{len(chunks)} transcript segments. Transcript source: {Path(args.json).name}.",
        url=None,
    )
    events.append(meta)

    for i, ch in enumerate(chunks, 1):
        seg_ts = start + timedelta(milliseconds=ch["from_ms"])
        events.append(
            Event(
                id=f"{subject}:c{i:03d}",
                source="meeting",
                event_type="transcript_segment",
                ts=seg_ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                actor=owner,
                subject=subject,
                title=f"{title} (part {i}/{len(chunks)})",
                body=ch["text"],
                url=None,
            )
        )

    people_refs = _person_refs(Path(args.json), owner)
    inserted = 0
    for ev in events:
        enrich_refs(ev)
        ev.refs.people = sorted(set(ev.refs.people) | set(people_refs))
        append_raw(ev, dry_run=args.dry_run)
        if insert_event(conn, ev, dry_run=args.dry_run):
            inserted += 1

    print(
        f"OK subject={subject} title={title!r} duration={duration} "
        f"chunks={len(chunks)} inserted={inserted} replaced={removed}"
        + (" (dry-run)" if args.dry_run else "")
    )


if __name__ == "__main__":
    main()

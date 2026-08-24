#!/usr/bin/env python3
"""
voice_gallery.py — local speaker voiceprint gallery for cross-meeting ID (P5.1).

Diarization (diarize.py) separates voices into anonymous clusters + emits a
256-d voiceprint per cluster. This turns those into NAMES over time:

  - `resolve`  — match each diarized cluster to a known person in the gallery
                 and write a per-meeting `speakers.json` sidecar (auto-suggestion
                 + confidence), preserving any owner-confirmed name.
  - `enroll` / `enroll-confirmed` — when the OWNER confirms a speaker is a
                 person (in the Steno UI), add that cluster's voiceprint to the
                 person's running-mean centroid. Corrections become training
                 data → future meetings auto-recognise the voice.

Gallery: ~/.steno-diarize/voices.json  { <handle>: {"centroid": [256], "n": int} }
LOCAL-ONLY: voiceprints are biometric — they never leave the machine (same
posture as the audio). Runs in the diarize venv (needs numpy).

Suggestions are confidence-gated and always owner-overridable — a low-confidence
voice match is a SUGGESTION, never a silently-applied name.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
DIAR_HOME = Path(os.environ.get("STENO_DIARIZE_HOME", HOME / ".steno-diarize"))
GALLERY = DIAR_HOME / "voices.json"
# Threshold to PRE-FILL a name suggestion. The old 0.55 came from a clean TTS A/B
# (cross-speaker ~0.29) that did NOT hold on real phone-quality far-end audio:
# measured 2026-08-24 on live huddles, DIFFERENT people reach cosine ~0.66 and
# false matches fired at 0.65-0.69 (a speaker's voice prefilled as an unrelated
# enrolled person). Same- and
# different-speaker similarities overlap in ~0.60-0.66 on this audio, so no cutoff
# gives clean recall — bias hard for PRECISION: a wrong prefilled name is worse
# than none (the owner would rather assign once, which enrolls a real sample and
# sharpens the gallery). 0.72 sits above the observed false-positive band; tune
# with STENO_VOICE_MATCH_THRESHOLD. Suggestion only — never a silently-applied name.
DEFAULT_THRESHOLD = float(os.environ.get("STENO_VOICE_MATCH_THRESHOLD", "0.72"))


def _np():
    import numpy as np

    return np


def load_gallery() -> dict:
    try:
        return json.loads(GALLERY.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_gallery(g: dict) -> None:
    GALLERY.parent.mkdir(parents=True, exist_ok=True)
    tmp = GALLERY.with_suffix(f".json.tmp.{os.getpid()}")  # PID-tmp atomic write
    tmp.write_text(json.dumps(g), encoding="utf-8")
    tmp.replace(GALLERY)


def _cos(np, a, b) -> float:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def identify(np, gallery: dict, emb, threshold: float) -> tuple[str | None, float]:
    """Nearest person by cosine. Returns (handle|None, best_score); None when the
    best score is below threshold."""
    best, score = None, 0.0
    for handle, rec in gallery.items():
        c = rec.get("centroid")
        if not c:
            continue
        s = _cos(np, emb, c)
        if s > score:
            best, score = handle, s
    return (best, score) if (best is not None and score >= threshold) else (None, score)


def enroll(np, gallery: dict, handle: str, emb) -> None:
    """Fold a voiceprint into <handle>'s running-mean centroid."""
    emb = [float(x) for x in emb]
    rec = gallery.get(handle)
    if not rec or not rec.get("centroid"):
        gallery[handle] = {"centroid": emb, "n": 1}
        return
    n = int(rec.get("n", 1))
    c = np.asarray(rec["centroid"], dtype=float)
    newc = (c * n + np.asarray(emb, dtype=float)) / (n + 1)
    gallery[handle] = {"centroid": [float(x) for x in newc], "n": n + 1}


def _display_numbering(turns: list[dict]) -> dict[str, str]:
    """cluster → 'Speaker N' by first appearance in time (matches merge_streams)."""
    order, seen = [], set()
    for t in sorted(turns, key=lambda x: (x["start_ms"], x["end_ms"])):
        s = t["speaker"]
        if s not in seen:
            seen.add(s)
            order.append(s)
    return {c: f"Speaker {i + 1}" for i, c in enumerate(order)}


def cmd_resolve(a) -> None:
    np = _np()
    diar = json.loads(Path(a.diar).read_text(encoding="utf-8"))
    emb = diar.get("embeddings", {})
    disp = _display_numbering(diar.get("turns", []))
    existing = {}
    if Path(a.speakers).exists():
        try:
            existing = json.loads(Path(a.speakers).read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    gallery = load_gallery()

    out: dict = {}
    for cluster in sorted(set(list(emb) + list(disp) + list(existing))):
        prev = existing.get(cluster, {})
        entry = {
            "display": disp.get(cluster, prev.get("display", cluster)),
            "auto": None,
            "score": 0.0,
            # owner-confirmed identity is NEVER overwritten by re-resolve
            "handle": prev.get("handle"),
            "name": prev.get("name"),
        }
        if cluster in emb:
            handle, score = identify(np, gallery, emb[cluster], a.threshold)
            entry["auto"] = handle
            entry["score"] = round(score, 3)
        out[cluster] = entry

    Path(a.speakers).write_text(json.dumps(out, indent=2), encoding="utf-8")
    n_auto = sum(1 for e in out.values() if e["auto"])
    print(f"OK resolve {len(out)} clusters ({n_auto} auto-matched) -> {a.speakers}")


def cmd_enroll(a) -> None:
    np = _np()
    emb = json.loads(Path(a.diar).read_text(encoding="utf-8")).get("embeddings", {}).get(a.cluster)
    if not emb:
        sys.exit(f"no voiceprint for cluster {a.cluster} in {a.diar}")
    g = load_gallery()
    enroll(np, g, a.handle, emb)
    save_gallery(g)
    print(f"OK enrolled {a.cluster} -> {a.handle} (n={g[a.handle]['n']})")


def cmd_enroll_confirmed(a) -> None:
    np = _np()
    emb = json.loads(Path(a.diar).read_text(encoding="utf-8")).get("embeddings", {})
    spk = json.loads(Path(a.speakers).read_text(encoding="utf-8"))
    g = load_gallery()
    n = 0
    for cluster, e in spk.items():
        handle = e.get("handle")
        if handle and cluster in emb:
            enroll(np, g, handle, emb[cluster])
            n += 1
    save_gallery(g)
    print(f"OK enrolled {n} confirmed voiceprint(s)")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve", help="match clusters to gallery → speakers.json")
    r.add_argument("diar")
    r.add_argument("speakers")
    r.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    r.set_defaults(fn=cmd_resolve)

    e = sub.add_parser("enroll", help="add one cluster's voiceprint to a person")
    e.add_argument("diar")
    e.add_argument("cluster")
    e.add_argument("handle")
    e.set_defaults(fn=cmd_enroll)

    ec = sub.add_parser("enroll-confirmed", help="enroll every confirmed cluster in speakers.json")
    ec.add_argument("diar")
    ec.add_argument("speakers")
    ec.set_defaults(fn=cmd_enroll_confirmed)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
diarize.py — local speaker diarization for single-mic (in-person) meetings.

The OVERLAY half of Steno's diarization (P5). whisper.cpp still does ALL the
transcription (every tuned flag preserved — silero VAD, vocab prompt, -mc 0
loop recovery, silence gate); this only answers "who spoke when". merge_streams.py
maps each whisper segment onto the dominant speaker turn here by timestamp
overlap, yielding `Speaker 1: / Speaker 2:` labels for meetings captured on ONE
room mic — the in-person case, where the dual-stream Me:/Them: trick gives no
separation because nobody dialed in (the system-audio stream is silent).

Runs pyannote speaker-diarization-3.1 and emits speaker turns as JSON:
    {"turns": [{"start_ms": int, "end_ms": int, "speaker": "SPEAKER_00"}, ...]}

LOCAL-ONLY: audio never leaves the machine (the whole reason Steno exists vs
Granola). Models are SIDE-LOADED (Zscaler blocks the HuggingFace CDN) into the
HF cache under STENO_DIARIZE_HOME — see bin/steno-diarize-setup.sh. HF_HUB_OFFLINE
is forced so a missing model fails fast and locally instead of hitting the block.

SOFT DEPENDENCY: if torch/pyannote or the models are absent, this exits 3 so the
caller (diarize.sh → the sweep) falls back to the un-diarized transcript. A
missing diarizer must NEVER cost a meeting.

Exit codes: 0 ok · 1 bad input · 3 deps/models unavailable (soft) · 4 run failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Side-loaded model cache (AirDropped; see setup script). Force offline BEFORE
# importing anything HF-aware so a missing model resolves from the local cache
# and fails fast rather than reaching for the Zscaler-blocked CDN.
_HOME = Path(os.path.expanduser("~"))
DIAR_HOME = Path(os.environ.get("STENO_DIARIZE_HOME", _HOME / ".steno-diarize"))
os.environ.setdefault("HF_HOME", str(DIAR_HOME / "hf"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# Prefer a side-loaded LOCAL pipeline config if present (models AirDropped past
# the Zscaler HF-CDN block, config.yaml rewritten to point at local checkpoints)
# — it loads fully offline with no cache-layout juggling. Falls back to the HF id
# (resolved from the HF_HOME cache) otherwise. STENO_DIARIZE_PIPELINE overrides.
_LOCAL_CFG = DIAR_HOME / "models" / "config.yaml"
PIPELINE_ID = os.environ.get("STENO_DIARIZE_PIPELINE") or (
    str(_LOCAL_CFG) if _LOCAL_CFG.exists() else "pyannote/speaker-diarization-3.1"
)


def _fail(msg: str, code: int = 3) -> None:
    print(f"diarize: {msg}", file=sys.stderr)
    sys.exit(code)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True, help="16 kHz mono wav (diarize.sh downmixes for you)")
    ap.add_argument("--out", required=True, help="turns JSON output path")
    ap.add_argument("--num-speakers", type=int, default=None, help="exact speaker count if known")
    ap.add_argument("--min-speakers", type=int, default=None)
    ap.add_argument("--max-speakers", type=int, default=None)
    args = ap.parse_args()

    wav = Path(args.wav)
    if not wav.is_file():
        _fail(f"wav not found: {wav}", code=1)

    try:
        import torch
        from pyannote.audio import Pipeline
    except Exception as e:  # deps not installed → soft skip
        _fail(f"pyannote/torch not installed ({e}) — run bin/steno-diarize-setup.sh")

    # torch ≥2.6 flipped torch.load(weights_only=True) by default, which rejects
    # the pyannote 3.1 checkpoints (they pickle TorchVersion / omegaconf globals).
    # These models are TRUSTED — owner side-loaded them from the official pyannote
    # HF repos (verified real torch archives) — so restore weights_only=False.
    _orig_load = torch.load
    torch.load = lambda *a, **k: _orig_load(*a, **{**k, "weights_only": False})

    try:
        pipeline = Pipeline.from_pretrained(PIPELINE_ID)
    except Exception as e:  # models not side-loaded / gating not accepted → soft skip
        _fail(f"could not load {PIPELINE_ID} ({e}) — side-load the gated models (see setup)")
    if pipeline is None:
        # pyannote returns None (not an exception) when the gated model wasn't
        # accepted / the token is missing — treat as soft-unavailable.
        _fail(f"{PIPELINE_ID} unavailable (gated model not accepted / not side-loaded)")

    kw: dict[str, int] = {}
    if args.num_speakers:
        kw["num_speakers"] = args.num_speakers
    if args.min_speakers:
        kw["min_speakers"] = args.min_speakers
    if args.max_speakers:
        kw["max_speakers"] = args.max_speakers

    def _run(device: str | None):
        if device:
            pipeline.to(torch.device(device))
        # return_embeddings gives a per-speaker 256-d voiceprint (aligned to
        # diarization.labels()) — the raw material for cross-meeting speaker ID
        # (voice_gallery.py). Fall back gracefully on older pyannote without it.
        try:
            return pipeline(str(wav), return_embeddings=True, **kw)
        except TypeError:
            return pipeline(str(wav), **kw)

    # Prefer Apple-Silicon MPS; fall back to CPU if MPS is absent or an op is
    # unsupported there (pyannote has a few). Correctness over speed.
    try:
        use_mps = bool(getattr(torch.backends, "mps", None)) and torch.backends.mps.is_available()
    except Exception:
        use_mps = False
    try:
        result = _run("mps" if use_mps else None)
    except Exception as e:
        if use_mps:
            print(f"diarize: MPS run failed ({e}) — retrying on CPU", file=sys.stderr)
            try:
                result = _run("cpu")
            except Exception as e2:
                _fail(f"diarization run failed ({e2})", code=4)
        else:
            _fail(f"diarization run failed ({e})", code=4)

    diarization, embeddings = result if isinstance(result, tuple) else (result, None)

    turns = [
        {"start_ms": int(turn.start * 1000), "end_ms": int(turn.end * 1000), "speaker": speaker}
        for turn, _, speaker in diarization.itertracks(yield_label=True)
    ]
    turns.sort(key=lambda t: (t["start_ms"], t["end_ms"]))

    # Per-cluster voiceprint: embeddings[i] ↔ diarization.labels()[i]. Skip rows
    # with NaN (a speaker with too little speech to embed).
    emb_map: dict[str, list[float]] = {}
    if embeddings is not None:
        import numpy as np

        labels = list(diarization.labels())
        for i, lab in enumerate(labels):
            if i >= len(embeddings):
                break
            row = embeddings[i]
            if row is not None and not bool(np.isnan(row).any()):
                emb_map[lab] = [float(x) for x in row]

    out: dict = {"turns": turns}
    if emb_map:
        out["embeddings"] = emb_map
    Path(args.out).write_text(json.dumps(out), encoding="utf-8")
    n_spk = len({t["speaker"] for t in turns})
    print(f"OK diarize {len(turns)} turns / {n_spk} speakers / {len(emb_map)} voiceprints -> {args.out}")


if __name__ == "__main__":
    main()

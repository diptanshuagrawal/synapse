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


# Over-split guard: pyannote sometimes splits ONE speaker into several clusters
# on imperfect audio (a lone far-side voice on a dual-stream call came back as
# Speaker 1 + Speaker 2). Two clusters that are really the same person have
# near-identical 256-d voiceprints, so merge any pair whose cosine similarity is
# >= a HIGH threshold. Conservative by design: the default only fires on
# near-identical voiceprints, so a genuine multi-speaker call is never collapsed.
# Tune with STENO_DIARIZE_MERGE_SIM; skipped entirely when the caller passed an
# explicit --num-speakers (we then trust the requested count).
MERGE_SIM = float(os.environ.get("STENO_DIARIZE_MERGE_SIM", "0.82"))
# Second over-split guard: a fragment too SHORT to embed gets no voiceprint, so
# _merge_oversplit (voiceprint-only) can't catch it — it survives as a phantom
# speaker (a lone far-side voice came back as a 27-segment real cluster + a
# 2-segment phantom → "Them · Speaker 1" AND "Speaker 2" for one person). Any
# cluster with less than this much TOTAL speech is folded into a surviving one.
# A real participant who speaks <2.5s across a whole meeting is negligible; tune
# with STENO_DIARIZE_MIN_CLUSTER_SEC (0 disables).
MIN_CLUSTER_SEC = float(os.environ.get("STENO_DIARIZE_MIN_CLUSTER_SEC", "2.5"))


def _merge_oversplit(turns: list[dict], emb_map: dict[str, list[float]], threshold: float):
    """Collapse clusters with near-identical voiceprints. Returns
    (turns, emb_map, merges) — merges is a list of (absorbed, kept, sim)."""
    import numpy as np

    labels = list(emb_map)
    if len(labels) < 2:
        return turns, emb_map, []
    unit = {}
    for lab in labels:
        v = np.asarray(emb_map[lab], dtype=float)
        n = float(np.linalg.norm(v))
        unit[lab] = v / n if n else v
    parent = {lab: lab for lab in labels}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    merges = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            a, b = labels[i], labels[j]
            sim = float(unit[a] @ unit[b])
            if sim >= threshold and find(a) != find(b):
                parent[find(b)] = find(a)
                merges.append((b, a, round(sim, 3)))
    if not merges:
        return turns, emb_map, []
    remap = {lab: find(lab) for lab in labels}
    new_turns = [{**t, "speaker": remap.get(t["speaker"], t["speaker"])} for t in turns]
    new_emb = {}
    for lab in labels:  # keep one voiceprint per surviving root
        new_emb.setdefault(remap[lab], emb_map[remap[lab]])
    return new_turns, new_emb, merges


def _absorb_tiny_clusters(turns: list[dict], emb_map: dict[str, list[float]], min_sec: float):
    """Fold negligible clusters (total speech < min_sec) into a surviving one.
    Unlike _merge_oversplit this is duration-based, so it catches fragments that
    were too short to embed (no voiceprint) and thus escape the similarity merge.
    Routes each tiny cluster to its nearest cluster by voiceprint when it has one,
    else to the dominant (longest-talking) cluster. Returns (turns, emb_map, absorbed)."""
    import collections
    import numpy as np

    if min_sec <= 0:
        return turns, emb_map, []
    dur: dict[str, float] = collections.defaultdict(float)
    for t in turns:
        dur[t["speaker"]] += max(0, t.get("end_ms", 0) - t.get("start_ms", 0)) / 1000.0
    if len(dur) < 2:
        return turns, emb_map, []
    dominant = max(dur, key=dur.get)
    # Speakers that will be absorbed. Routing targets are drawn ONLY from
    # survivors (not in `tiny`), so a remap target is never itself absorbed —
    # that guarantees no remap chain/cycle when two tiny clusters are mutually
    # nearest (the single-pass remap below can't follow a chain).
    tiny = {sp for sp, d in dur.items() if sp != dominant and d < min_sec}

    def _unit(lab):
        v = np.asarray(emb_map[lab], dtype=float)
        n = float(np.linalg.norm(v))
        return v / n if n else v

    remap: dict[str, str] = {}
    absorbed = []
    for sp in sorted(tiny):
        target = dominant  # default: fold into the longest-talking speaker
        if sp in emb_map:  # if it did embed, prefer its nearest SURVIVING peer
            best, best_sim = None, -1.0
            for other in dur:
                if other == sp or other in tiny or other not in emb_map:
                    continue
                sim = float(_unit(sp) @ _unit(other))
                if sim > best_sim:
                    best_sim, best = sim, other
            if best is not None:
                target = best
        remap[sp] = target
        absorbed.append((sp, target, round(dur[sp], 1)))
    if not absorbed:
        return turns, emb_map, []
    new_turns = [{**t, "speaker": remap.get(t["speaker"], t["speaker"])} for t in turns]
    new_emb = {k: v for k, v in emb_map.items() if k not in remap}
    return new_turns, new_emb, absorbed


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

    # Collapse an over-split single speaker (unless an exact count was requested).
    if not args.num_speakers and len(emb_map) >= 2:
        turns, emb_map, merges = _merge_oversplit(turns, emb_map, MERGE_SIM)
        for absorbed, kept, sim in merges:
            print(f"diarize: merged over-split {absorbed} -> {kept} (voiceprint sim {sim} >= {MERGE_SIM})", file=sys.stderr)
    # Duration-based pass — catches short phantom fragments that never embedded
    # (so _merge_oversplit's voiceprint check couldn't reach them).
    if not args.num_speakers:
        turns, emb_map, tiny = _absorb_tiny_clusters(turns, emb_map, MIN_CLUSTER_SEC)
        for sp, target, secs in tiny:
            print(f"diarize: absorbed tiny cluster {sp} ({secs}s < {MIN_CLUSTER_SEC}s) -> {target}", file=sys.stderr)

    out: dict = {"turns": turns}
    if emb_map:
        out["embeddings"] = emb_map
    Path(args.out).write_text(json.dumps(out), encoding="utf-8")
    n_spk = len({t["speaker"] for t in turns})
    print(f"OK diarize {len(turns)} turns / {n_spk} speakers / {len(emb_map)} voiceprints -> {args.out}")


if __name__ == "__main__":
    main()

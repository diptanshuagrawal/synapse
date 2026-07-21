#!/usr/bin/env bash
# steno-diarize-setup.sh — one-time local setup for Steno speaker diarization (P5).
#
# Creates an ISOLATED python venv for pyannote/torch — kept OUT of the lean
# work-context venv and OUT of the repo (torch is ~2 GB; the repo is public) —
# and installs deps from PyPI (reachable here; only the HuggingFace *model* CDN
# is Zscaler-blocked).
#
# The gated pyannote MODELS are side-loaded separately (mirror of the
# whisper/silero AirDrop workaround) — see the printed steps at the end. Until
# they land, diarize.sh is a soft no-op and the pipeline keeps today's behavior.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

DIAR_HOME="${STENO_DIARIZE_HOME:-$HOME/.steno-diarize}"
VENV="$DIAR_HOME/venv"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$DIAR_HOME/hf"

PYBASE="${PYTHON3:-python3}"
command -v "$PYBASE" >/dev/null || { echo "ERROR: python3 not found"; exit 1; }

if [ ! -x "$VENV/bin/python3" ]; then
  echo "creating diarize venv → $VENV"
  "$PYBASE" -m venv "$VENV"
fi
"$VENV/bin/python3" -m pip install --upgrade pip >/dev/null
echo "installing torch + torchaudio + pyannote.audio (PyPI)…"
# Version matrix matters (learned the hard way 2026-07-21):
#  - pyannote.audio 3.x (NOT 4.x): 4.x changed the checkpoint loader and can't
#    read the side-loaded speaker-diarization-3.1 models.
#  - torch/torchaudio 2.7.x (NOT ≥2.8): torchaudio 2.8 removed AudioMetaData,
#    which pyannote 3.x imports. 2.7.1 is the last series that keeps it.
#  - (torch ≥2.6 also defaults torch.load(weights_only=True); diarize.py
#    restores weights_only=False for the trusted local checkpoints.)
"$VENV/bin/python3" -m pip install "torch==2.7.1" "torchaudio==2.7.1" "pyannote.audio>=3.1,<4" "huggingface_hub>=0.23"

MODELS="$DIAR_HOME/models"
mkdir -p "$MODELS"
cat <<EOF

── diarize deps installed → $VENV ──────────────────────────────────
Next: side-load 3 model files (Zscaler blocks the HF CDN here). This is the
LOOSE-FILE recipe that works with the pyannote/ repos + the torch embedding path
(no onnxruntime, no HF cache-layout juggling). Verified 2026-07-21.

Download on a device WITH HuggingFace access (iPhone Safari on cellular, etc.):
  A. GATED — log in to huggingface.co and click "Agree and access" first:
       https://huggingface.co/pyannote/speaker-diarization-3.1  → config.yaml
       https://huggingface.co/pyannote/segmentation-3.0         → pytorch_model.bin
     Direct links (append ?download=true; must be logged in or you get a
     133-byte "please log in" page):
       .../speaker-diarization-3.1/resolve/main/config.yaml?download=true
       .../segmentation-3.0/resolve/main/pytorch_model.bin?download=true
  B. UNGATED — no login needed:
       https://huggingface.co/pyannote/wespeaker-voxceleb-resnet34-LM/resolve/main/pytorch_model.bin?download=true

Place them on THIS Mac as (rename so the embedding path has NO "wespeaker" in it
— else pyannote routes it to the ONNX loader and fails):
     $MODELS/config.yaml           (from speaker-diarization-3.1)
     $MODELS/segmentation.bin      (segmentation-3.0 pytorch_model.bin)
     $MODELS/embedding.bin         (wespeaker pytorch_model.bin)

Then rewrite the two model refs in config.yaml to the LOCAL files:
     sed -i '' \\
       -e "s|embedding: pyannote/wespeaker-voxceleb-resnet34-LM|embedding: $MODELS/embedding.bin|" \\
       -e "s|segmentation: pyannote/segmentation-3.0|segmentation: $MODELS/segmentation.bin|" \\
       "$MODELS/config.yaml"

Verify (any 16 kHz mono wav — ffmpeg -i in.m4a -ar 16000 -ac 1 t.wav):
     $VENV/bin/python3 $REPO/work-context/derive/meetings/diarize.py \\
       --wav t.wav --out /tmp/turns.json && cat /tmp/turns.json

(Alt: diarize_fetch_models.py + a HF token over a hotspot builds the HF cache
 layout instead — but the loose-file recipe above needs no token.)
─────────────────────────────────────────────────────────────────────
EOF

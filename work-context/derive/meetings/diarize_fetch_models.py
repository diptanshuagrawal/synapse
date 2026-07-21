#!/usr/bin/env python3
"""
diarize_fetch_models.py — populate a portable HF cache with the gated pyannote
diarization models, to AirDrop onto the Zscaler-blocked Mac.

Steno keeps audio local, so the models must live on the machine. The HuggingFace
CDN is blocked here; run this on a machine/network WITH access, then AirDrop the
cache dir over (mirror of the whisper/silero side-load workaround).

Run AFTER accepting the gating on huggingface.co for BOTH:
    pyannote/speaker-diarization-3.1
    pyannote/segmentation-3.0

    HF_HOME=/tmp/steno-hf HUGGINGFACE_TOKEN=hf_xxx python3 diarize_fetch_models.py

Then AirDrop $HF_HOME to the Mac and:  rsync -a steno-hf/  ~/.steno-diarize/hf/
"""

from __future__ import annotations

import os
import sys

# speaker-diarization-3.1 loads segmentation-3.0 + the wespeaker embedding at
# runtime — all three must be in the cache for the offline pipeline to build.
REPOS = [
    "pyannote/speaker-diarization-3.1",
    "pyannote/segmentation-3.0",
    "pyannote/wespeaker-voxceleb-resnet34-LM",
]


def main() -> None:
    token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    if not token:
        sys.exit("ERROR: set HUGGINGFACE_TOKEN=hf_... (accept model gating first)")
    if not os.environ.get("HF_HOME"):
        sys.exit("ERROR: set HF_HOME to a portable dir first (e.g. /tmp/steno-hf)")
    try:
        from huggingface_hub import snapshot_download
    except Exception:
        sys.exit("ERROR: pip install huggingface_hub first")

    for repo in REPOS:
        print(f"downloading {repo} …", flush=True)
        try:
            snapshot_download(repo_id=repo, token=token)
        except Exception as e:
            sys.exit(
                f"ERROR downloading {repo}: {e}\n"
                "  → did you accept the gating on huggingface.co for this repo?"
            )
    print(f"done → {os.environ['HF_HOME']}  (AirDrop this whole dir to the Mac)")


if __name__ == "__main__":
    main()

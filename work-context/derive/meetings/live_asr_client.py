#!/usr/bin/env python3
"""
live_asr_client.py — one-shot client for the resident whisper-server
(live-copilot v2 layer). Sends a wav slice to the warm server (model already
loaded — no per-slice reload tax), applies the correction stack in-process
(phrase map + fuzzy names), prints the corrected text.

Usage: live_asr_client.py <slice.wav> [prompt]
Exit 0 with text on stdout; exit 3 if the server is unreachable (caller falls
back to whisper-cli).
"""

from __future__ import annotations

import json
import sys
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from correct import correct_text  # noqa: E402

SERVER = "http://127.0.0.1:8790/inference"


def main() -> None:
    wav = Path(sys.argv[1])
    prompt = sys.argv[2] if len(sys.argv) > 2 else ""

    boundary = uuid.uuid4().hex
    parts = []

    def field(name: str, value: str) -> None:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )

    field("response_format", "json")
    field("language", "en")   # 4s slices misfire auto-detect (Arabic hallucination 2026-07-19); live=en, final transcript stays auto
    field("temperature", "0.0")
    if prompt:
        field("prompt", prompt)
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{wav.name}\"\r\nContent-Type: audio/wav\r\n\r\n".encode()
        + wav.read_bytes() + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        SERVER, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception:
        sys.exit(3)  # server down → caller falls back to whisper-cli

    text = (data.get("text") or "").replace("\n", " ").strip()
    if text:
        print(correct_text(text))


if __name__ == "__main__":
    main()

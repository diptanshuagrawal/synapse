#!/usr/bin/env python3
"""
live_them_daemon.py — persistent live transcriber for the THEM stream
(copilot v3). One process for the whole meeting: slices new sys.wav audio
every ~2s (raw-byte slicing — mid-write WAV headers are stale), posts to the
resident whisper-server (warm model), corrects in-process, appends
"[mm:ss] Them:" lines stamped with AUDIO-clock time.

Me-side runs via whisper-stream (live_me_stream.sh). Consumers sort the
merged file by the [mm:ss] stamp — arrival order is NOT spoken order.
Exits when the recording ends. Self-locks via .livethem.lock dir.
"""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from correct import correct_text  # noqa: E402

WC = Path(__file__).resolve().parents[2]
CAP = WC / "transcripts" / ".capture"
LIVE = CAP / "live_transcript.txt"
# stream selected by argv: `me` (mic.wav) or `them` (sys.wav, default)
_stream = sys.argv[1] if len(sys.argv) > 1 else "them"
SRC = CAP / ("mic.wav" if _stream == "me" else "sys.wav")
LABEL = "Me" if _stream == "me" else "Them"
LOCK = CAP / f".live{_stream}.lock"
SERVER = "http://127.0.0.1:8790/inference"
HALLU = re.compile(r"www\.|https?://|thanks for watching|for more information|subtitle", re.I)
MIN_NEW_S = 2.0


def recording() -> bool:
    try:
        pid = int((CAP / "pid").read_text().split()[0])
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def probe_fmt(path: Path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_name,sample_rate,channels,bits_per_sample",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout.strip().splitlines()
    if not out:
        return None
    codec, rate, ch, bits = out[0].split(",")
    fmt = {"pcm_s16le": "s16le", "pcm_s24le": "s24le"}.get(codec, "f32le")
    return fmt, int(rate), int(ch), int(bits)


def asr(wav_bytes: bytes) -> str:
    boundary = uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f"name=\"{name}\"\r\n\r\n{value}\r\n".encode())

    field("response_format", "json")
    field("language", "en")
    field("temperature", "0.0")
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f"filename=\"s.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode()
                 + wav_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        SERVER, data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return (json.load(r).get("text") or "").replace("\n", " ").strip()


def to_wav16k(raw: bytes, fmt: str, rate: int, ch: int) -> bytes:
    p = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", fmt, "-ar", str(rate), "-ac", str(ch),
         "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
        input=raw, capture_output=True)
    return p.stdout


def main() -> None:
    try:
        LOCK.mkdir()
    except FileExistsError:
        return
    try:
        run()
    finally:
        try:
            LOCK.rmdir()
        except Exception:
            pass


def run() -> None:
    if not recording():
        return
    rec_start = int((CAP / "pid").read_text().split()[2])
    # one writer truncates per recording (same marker the Me-side uses)
    marker = CAP / ".live_rec_id"
    prev = marker.read_text().strip() if marker.exists() else ""
    if prev != str(rec_start):
        marker.write_text(str(rec_start))
        LIVE.write_text(f"live transcription started {time.strftime('%H:%M:%S')}\n")

    fmt = None
    done_s = 0.0
    last_text = ""
    while recording():
        time.sleep(1.0)
        if not SRC.exists() or SRC.stat().st_size < 8192:
            continue
        if fmt is None:
            fmt = probe_fmt(SRC)
            if fmt is None:
                continue
        f, rate, ch, bits = fmt
        bps = rate * ch * bits // 8
        total = (SRC.stat().st_size - 4096) / bps
        new = total - done_s
        if new < MIN_NEW_S:
            continue
        span = min(new, 20.0)
        off = 44 + int(done_s * bps)
        cnt = int(span * bps)
        with open(SRC, "rb") as fh:
            fh.seek(off)
            raw = fh.read(cnt)
        try:
            text = asr(to_wav16k(raw, f, rate, ch))
        except Exception:
            time.sleep(2)  # server hiccup — retry next loop, don't advance
            continue
        done_s += span
        text = text.strip()
        # consecutive-repeat suppression: whisper repeats a phrase on quiet
        # audio ("Thank you." floods) — same line twice in a row = drop.
        if text and text == last_text:
            continue
        if len(text) >= 4 and not HALLU.search(text):
            last_text = text
            m, s = int(done_s - span) // 60, int(done_s - span) % 60
            with open(LIVE, "a") as out:
                out.write(f"[{m:02d}:{s:02d}] {LABEL}: {correct_text(text)}\n")


if __name__ == "__main__":
    main()

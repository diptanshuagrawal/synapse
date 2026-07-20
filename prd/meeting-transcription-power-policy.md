# Meeting transcription — battery / power policy (long-term fix)

**Problem.** Whisper `large-v3` on the Metal GPU (M1 Pro) is power-heavy, and the
sweep fires with **no power / meeting / thermal awareness**: 65s after every call,
via 3 triggers (`meet-record` stop-kick, the `transcripts-watch` launchd
agent, notes routine). Back-to-back calls → whisper runs *during your next meeting*,
on battery. A merge-crash retry loop amplified it (fixed 2026-07-20:
`merge_streams.py` tolerant UTF-8 decode). The manual pause flag
(`transcripts/.transcription_paused`) is a band-aid, not the fix.

**Principle.** Never *drop* a recording; *defer* transcription to a cheap moment.
Recordings already show on Steno raw (untranscribed) — so deferral is invisible to
the user. Transcribe at the right **time** (plugged in, not mid-meeting), at the
right **cost** (lighter model / hardware accel).

---

## Policy (steady state) — replaces per-call immediate sweeps

`transcripts_process.sh` transcribes a queued recording only when ALL hold:

1. **On AC power AND charge ≥ floor** — `pmset -g ps` contains "AC Power" and battery
   %% ≥ `TRANSCRIBE_BATTERY_FLOOR` (default 30). Off AC → defer. On AC but below the
   floor → still defer: a heavy GPU job at very low charge can out-draw a weak charger
   (net drain while "charging"). Deferred files stay in the inbox (shown raw on Steno).
2. **No recording active** — `meet-record status` ≠ RECORDING. Don't fight a live
   meeting for GPU.
3. **Not hard-paused** — `transcripts/.transcription_paused` absent.

Overrides:
- `FORCE_TRANSCRIBE=1` (Steno **Transcribe** button) bypasses 1–3 — explicit intent.
- Hard pause flag bypasses nothing — it's the master off switch.

The queue is drained by the existing `transcripts-watch` launchd agent
— it's already a periodic `StartInterval` watcher, so with the power gate it drains
the queue automatically the moment you're on AC (day or night while charging). No
separate nightly job needed.

## Model tiering — NOT changed (respect the A/B)

`transcribe.sh` documents an owner A/B (2026-07-18): `large-v3` chosen over turbo
("equal-or-better accuracy, 2× slower but irrelevant for background"). Once
transcription is AC-gated, large-v3's higher energy never touches the battery — so
turbo is unnecessary and the quality decision stands. `WHISPER_MODEL` env already
exists as the override. Turbo remains an *optional* on-battery fallback only if we
ever choose to transcribe on battery.

## Deeper lever (stretch) — ANE via CoreML

Current whisper-cli (homebrew ggml 0.16.0) is **Metal-only**, no CoreML flag. Rebuild
whisper.cpp with `WHISPER_COREML=1` + generate the CoreML encoder
(`ggml-large-v3-encoder.mlmodelc`) → encoder runs on the **Neural Engine**: large
power drop + faster. Safe to rebuild (unlike the capture binary, which is TCC-bound —
do not touch). Biggest hardware win; requires a source build + model gen.

---

## Implementation status

1. **Power + meeting gate** in `transcripts_process.sh` (early-exit → defer).
   ✅ DONE 2026-07-20 — single choke point; `FORCE_TRANSCRIBE=1` overrides.
2. ~~Nightly backstop~~ — NOT needed; `transcripts-watch` is already periodic.
3. ~~Model default → turbo~~ — NOT changed; respects the 2026-07-18 large-v3 A/B
   (AC-gating makes its energy moot).
4. **Steno UI** (optional, todo): surface "N queued · deferred (on battery)" + promote
   the manual pause flag to a real toggle button.
5. **(stretch)** CoreML/ANE whisper build — the deep power lever.

## Net behaviour

Record freely on battery all day → recordings visible immediately (raw) → they
transcribe automatically the moment you're plugged in and not in a meeting (turbo,
low cost), or overnight. Manual button for "I need this one now." Battery drain: gone
by construction, not by remembering to toggle a flag.

# Meeting transcription — battery / power policy (long-term fix)

**Problem.** Whisper `large-v3` on the Metal GPU (M1 Pro) is power-heavy, and the
sweep fires with **no power / meeting / thermal awareness**: 65s after every call,
via 3 triggers (`meet-record` stop-kick, the `transcripts-watch` launchd
agent, notes routine). Back-to-back calls → whisper runs *during your next meeting*,
on battery. A merge-crash retry loop amplified it (fixed 2026-07-20:
`merge_streams.py` tolerant UTF-8 decode). The manual pause flag
(`transcripts/.transcription_paused`) is a band-aid, not the fix.

**Principle.** Never *drop* a recording, and never *drain* for one. Transcribe at the
right **cost** — the lighter model (turbo) on battery, `large-v3` on AC — rather than
deferring for hours. Right after the meeting, not mid-meeting; below a low-charge
floor, defer to AC. Recordings show on Steno raw meanwhile, so any deferral is visible.

---

## Policy (steady state) — replaces per-call immediate sweeps

`transcripts_process.sh` transcribes a queued recording when ALL hold:

1. **Power** (revised 2026-07-27 — transcribe right after the meeting, not "when
   plugged in"):
   - **On AC** — battery %% ≥ `TRANSCRIBE_BATTERY_FLOOR` (default 30) → `large-v3`
     (best quality; energy irrelevant on AC). Below the floor → defer: a heavy GPU
     job at very low charge can out-draw a weak charger (net drain while "charging").
   - **On battery** — %% ≥ `TRANSCRIBE_BATTERY_TURBO_FLOOR` (default 20) → transcribe
     **now with `large-v3-turbo`** (4-6× faster = a fraction of the energy; ~1-2 %%
     battery for a 30-min meeting). Below the turbo floor → defer until AC.
   - Deferred files stay in the inbox (shown raw on Steno); `transcripts-watch` drains
     them the moment you're on AC.
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

## Model tiering — quality on AC, turbo on battery

`transcribe.sh` documents an owner A/B (2026-07-18): `large-v3` chosen over turbo
("equal-or-better accuracy, 2× slower but irrelevant for background"). That holds
**on AC**, where energy is free — so AC transcription keeps `large-v3`.

**On battery** the trade flips (revised 2026-07-27): waiting for AC meant notes
weren't ready right after the meeting. `large-v3` is the drain; turbo is 4-6× faster
for a fraction of the energy. So on battery (≥ turbo floor) the sweep runs turbo —
immediacy for a marginal quality cost. `transcripts_process.sh` sets
`TRANSCRIBE_ON_BATTERY=1`, which the model-selection block honours over the backlog
heuristic; an explicit `WHISPER_MODEL` from the caller still wins outright.

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
   ✅ REVISED 2026-07-27 — on battery ≥ 20 %% no longer defers; transcribes now
   with turbo (`TRANSCRIBE_BATTERY_TURBO_FLOOR`, `TRANSCRIBE_ON_BATTERY=1`).
2. ~~Nightly backstop~~ — NOT needed; `transcripts-watch` is already periodic.
3. **Model default → turbo on battery** ✅ DONE 2026-07-27 — AC keeps large-v3
   (A/B stands where energy is free); battery uses turbo for immediacy.
4. **Steno UI** (optional, todo): surface "N queued · deferred (on battery)" + promote
   the manual pause flag to a real toggle button.
5. **(stretch)** CoreML/ANE whisper build — the deep power lever.

## Net behaviour

Record freely on battery → recordings visible immediately (raw) → they transcribe
**right after the meeting** with turbo (low cost, ~1-2 %% battery), or with large-v3
the moment you're on AC. Only a genuinely low battery (< 20 %%) defers to AC. Manual
button still forces "I need this one now." Immediacy without the drain.

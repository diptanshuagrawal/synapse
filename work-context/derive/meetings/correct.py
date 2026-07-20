#!/usr/bin/env python3
"""
correct.py — post-ASR fuzzy correction against the full domain vocabulary.

The whisper prompt only BIASES decoding (and is capped at ~224 tokens), so
rare names/jargon still slip through phonetically (a garbled first name →
the real teammate, "lein"→lien, a mangled product name → its canonical
spelling). This pass runs AFTER transcription and
fuzzy-matches each suspicious token against the UNLIMITED vocabulary (names
from people.yaml + terms from transcribe.yaml), replacing close matches.

Deterministic (difflib, no model). Conservative by design:
  - only touches tokens length ≥ 4 that are NOT common English words
  - NAMES match at ratio ≥ 0.72 (the main problem; rarely collide with words)
  - TERMS match at ratio ≥ 0.84 (tighter — avoid mangling real words)
  - preserves surrounding punctuation and the original's capitalisation intent

Shared by ingest_transcript.py (→ events.db) and merge_streams.py (→ txt/UI).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

WC = Path(__file__).resolve().parents[2]

# Small stop-set: frequent words that are close to some jargon but must never
# be "corrected". Keep tight — the length+ratio gates do most of the work.
_COMMON = {
    "about", "would", "could", "should", "there", "their", "which", "these",
    "those", "where", "while", "being", "doing", "going", "thing", "think",
    "people", "please", "before", "after", "today", "release", "report",
    "review", "record", "number", "account", "payment", "balance",
    # interjections / very common short words — must never become a name
    "yeah", "yes", "yep", "okay", "sure", "right", "well", "hmm", "nope",
    "cool", "good", "great", "thanks", "then", "than", "them", "they", "this",
    "that", "here", "hear", "team", "seen", "been", "done", "just", "some",
    "much", "many", "also", "even", "over", "into", "back", "next", "with",
}


@lru_cache(maxsize=1)
def _vocab() -> tuple[tuple[str, float], ...]:
    """(term, threshold) pairs — names loose, terms tight. Cached per process."""
    import yaml

    out: dict[str, float] = {}
    # Names — first + full — loosest threshold.
    try:
        for p in (yaml.safe_load(open(WC / "config" / "people.yaml")) or {}).get("people", []):
            nm = (p.get("name") or "").strip()
            if nm:
                out[nm.split()[0]] = 0.72
                if " " in nm:
                    out.setdefault(nm, 0.78)
    except Exception:
        pass
    # Curated terms — tighter.
    try:
        for t in (yaml.safe_load(open(WC / "config" / "transcribe.yaml")) or {}).get("vocab", []):
            t = str(t).strip()
            if len(t) >= 4:
                out.setdefault(t, 0.84)
    except Exception:
        pass
    return tuple(out.items())


def _best(token: str) -> str | None:
    tl = token.lower()
    if len(tl) < 5 or tl in _COMMON:
        # Short tokens (≤4) fuzzy-match far too easily ("Yeah"→"Yash");
        # only correct tokens of length ≥5, where a near-match is meaningful.
        return None
    # Token IS a known-correct term → never touch it. Without this, a correct
    # name gets 'corrected' to a similar-sounding teammate name (the exact
    # match is excluded from candidates, the near-name wins). Real bug 2026-07-18.
    if any(term.lower() == tl for term, _ in _vocab()):
        return None
    best, best_r = None, 0.0
    for term, thr in _vocab():
        if abs(len(term) - len(token)) > 3:
            continue
        # Extra caution on shortish tokens — require a tighter match.
        eff = thr + (0.06 if len(tl) < 7 else 0.0)
        r = SequenceMatcher(None, tl, term.lower()).ratio()
        if r >= eff and r > best_r and term.lower() != tl:
            best, best_r = term, r
    return best


_WORD = re.compile(r"[A-Za-z][A-Za-z']+")


@lru_cache(maxsize=1)
def _phrase_map() -> tuple[tuple[re.Pattern, str], ...]:
    """Exact multi-word corrections from config/transcribe_corrections.yaml —
    the owner-maintained fix list for recurring mishears the fuzzy token
    matcher can't reach ("Osprey plus" → OSPlus)."""
    import yaml

    out = []
    try:
        data = yaml.safe_load(open(WC / "config" / "transcribe_corrections.yaml")) or {}
        for wrong, right in (data.get("phrases") or {}).items():
            out.append((re.compile(re.escape(str(wrong)), re.I), str(right)))
    except Exception:
        pass
    return tuple(out)


def correct_text(text: str) -> str:
    # 1. exact phrase map (multi-word, owner-curated)
    for pat, right in _phrase_map():
        text = pat.sub(right, text)

    # 2. fuzzy single-token pass (names/jargon)
    def sub(m: re.Match) -> str:
        repl = _best(m.group(0))
        return repl if repl else m.group(0)

    return _WORD.sub(sub, text)


if __name__ == "__main__":
    import sys

    for line in sys.stdin:
        sys.stdout.write(correct_text(line))

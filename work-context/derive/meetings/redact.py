#!/usr/bin/env python3
"""
redact.py — deterministic PII redaction for shareable meeting exports (Steno P5).

Steno notes are private (management/ is gitignored, never published). Before a
note / MoM leaves the machine it must be scrubbed of identifiers. This masks the
high-risk PII a bank meeting leaks — email, phone, account/card numbers,
PAN / Aadhaar / IFSC, and long ID runs — DETERMINISTICALLY (no model, no cloud;
the audio-stays-local principle extends to the text).

Person NAMES are masked only on request (--mask-names): minutes usually WANT the
attendee names, but a wider share may not. Off by default ("names optional").

NEVER auto-sends. Produces a redacted copy + a report of what was masked, for the
OWNER to review and share by hand (permission-required action — see meeting-share).

Deliberately NOT touched (masking them would gut a shareable MoM, and they carry
no personal identifier here): Jira keys (ABC-123), [mm:ss] transcript offsets,
ISO dates, money amounts, URLs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

WC = Path(__file__).resolve().parents[2]

# Order matters: most-specific / longest first so a card isn't half-eaten by the
# generic long-digit rule, and mobiles are labelled `phone` before the catch-all
# `account` claims them. Placeholders contain no digits, so a later digit rule
# never re-matches an already-masked span. All masks preserve the surrounding
# markdown; only the identifier token is replaced.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email",   re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # 16-digit card written with space/hyphen groups (a bare 16-run → account).
    ("card",    re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}\b")),
    ("ifsc",    re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")),
    ("pan",     re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("aadhaar", re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b")),
    # Indian mobile: optional +91, exactly 10 digits starting 6-9. Digit-guards
    # so it never eats a slice of a longer account run.
    ("phone",   re.compile(r"(?<!\d)(?:\+?91[-\s]?)?[6-9]\d{9}(?!\d)")),
    # Catch-all: any 9–18 digit run (account / CIF / customer id).
    ("account", re.compile(r"(?<!\d)\d{9,18}(?!\d)")),
]


@lru_cache(maxsize=1)
def _roster() -> tuple[str, ...]:
    """Team names (full + first) from people.yaml, longest-first so a full name
    masks before its first-name substring. Length ≥3 to avoid masking initials
    that collide with words."""
    import yaml

    names: set[str] = set()
    try:
        for p in (yaml.safe_load(open(WC / "config" / "people.yaml")) or {}).get("people", []):
            nm = (p.get("name") or "").strip()
            if nm:
                names.add(nm)
                names.add(nm.split()[0])
    except Exception:
        pass
    return tuple(sorted((n for n in names if len(n) >= 3), key=len, reverse=True))


def _mask_names(text: str) -> tuple[str, int]:
    total = 0
    for nm in _roster():
        text, c = re.compile(rf"\b{re.escape(nm)}\b", re.I).subn("[name]", text)
        total += c
    return text, total


def redact_text(text: str, mask_names: bool = False) -> tuple[str, dict[str, int]]:
    """Return (redacted_text, {kind: count}). Deterministic; safe to re-run
    (placeholders are digit-free so nothing is double-masked)."""
    report: dict[str, int] = {}

    def repl(kind: str):
        def _f(_m: re.Match) -> str:
            report[kind] = report.get(kind, 0) + 1
            return f"[{kind}]"
        return _f

    for kind, pat in _PATTERNS:
        text = pat.sub(repl(kind), text)
    if mask_names:
        text, n = _mask_names(text)
        if n:
            report["name"] = n
    return text, report


def summarize(report: dict[str, int]) -> str:
    if not report:
        return "nothing"
    return ", ".join(f"{v} {k}{'s' if v > 1 else ''}" for k, v in sorted(report.items()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("infile", help="markdown note / MoM to redact")
    ap.add_argument("--out", help="output path (default: <infile>.share.md)")
    ap.add_argument("--mask-names", action="store_true", help="also mask team names")
    ap.add_argument("--report-json", action="store_true", help="print {out, masked} as JSON")
    args = ap.parse_args()

    src = Path(args.infile)
    if not src.is_file():
        sys.exit(f"ERROR: not found: {src}")
    redacted, report = redact_text(
        src.read_text(encoding="utf-8", errors="replace"), args.mask_names
    )
    out = Path(args.out) if args.out else src.with_suffix(".share.md")
    out.write_text(redacted, encoding="utf-8")

    if args.report_json:
        print(json.dumps({"out": str(out), "masked": report}))
    else:
        print(f"OK redacted → {out}  (masked: {summarize(report)})")


if __name__ == "__main__":
    main()

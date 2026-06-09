"""
verify_render.py — Layer C gate for the deterministic /ask render.

PARALLEL VERSION — not wired into live /ask.

Takes a render manifest (from person_v4_manifest.py) and a written narrative
.md file, and asserts every must-appear token in `verify_manifest` is present
in the prose. This makes "did all required facts land" deterministic even
though the wording is model-generated.

Token kinds in verify_manifest:
  - jira ids / PR refs / page ids  → must appear verbatim in the file
  - flag:<kind>                     → the flag's evidence subject(s) OR a
                                      recognised phrase must appear
  - caveat:<kind>                   → a recognised marker for that caveat
                                      must appear

Exit 0 = all present. Exit 1 = missing items listed on stdout (the parallel
skill surfaces these and regenerates — per owner decision, SURFACE not silent).

Usage:
    .venv/bin/python derive/verify_render.py --manifest m.json --file out.md
"""

from __future__ import annotations

import argparse
import json
import sys


# Recognised prose markers per flag/caveat kind. The token passes if EITHER an
# evidence subject is cited OR one of these markers appears (case-insensitive).
# Markers must be SPECIFIC to the flagged signal — generic words ("workload",
# "risk") false-pass when a narrative discusses the topic abstractly without
# surfacing the actual evidence. Keep these tight to the phenomenon itself.
_FLAG_MARKERS = {
    "workload_sentiment": ["overwhelm", "swamped", "burnt out", "burning out",
                           "overloaded", "too much on", "asked for time",
                           "asked for breathing"],
    "commit_without_pr": ["commits into", "commit into", "no pr of his own",
                          "no own pr", "zero own pr", "opened no pr",
                          "opened none", "without opening"],
    "risk_callout": ["race condition", "deadlock", "data loss", "panic",
                     "idempot", "double payout", "double charge"],
}
_CAVEAT_MARKERS = {
    "sp_attribution_fallback": ["attribution", "creation", "point credit",
                                "less certain"],
    "no_own_prs": ["no own pr", "opened no", "zero pr", "no pull request",
                   "no code-quality", "no quality signal", "merge-speed",
                   "merge speed"],
}


def verify(manifest: dict, text: str) -> list[str]:
    low = text.lower()
    titles = manifest.get("cite_titles", {})
    missing: list[str] = []
    for token in manifest.get("verify_manifest", []):
        if token.startswith("flag:"):
            kind = token[5:]
            markers = _FLAG_MARKERS.get(kind, [kind.replace("_", " ")])
            ev_ok = any(
                any(e.get("subject", "") in text for e in f.get("evidence", []))
                for f in manifest.get("flags", []) if f.get("kind") == kind
            )
            if not (ev_ok or any(m in low for m in markers)):
                missing.append(token)
        elif token.startswith("caveat:"):
            kind = token[7:]
            markers = _CAVEAT_MARKERS.get(kind, [kind.replace("_", " ")])
            if not any(m in low for m in markers):
                missing.append(token)
        elif token.startswith("page:"):
            # confluence: match by numeric id (covers /pages/NNNN/ urls) OR by
            # the page title (prose often names the doc, not the id).
            pid = token.split(":", 1)[1]
            title = (titles.get(token) or "").lower().strip()
            if pid in text:
                continue
            if title and title in low:
                continue
            missing.append(token)
        else:
            # jira id / PR ref — must appear verbatim (they do in real prose).
            if token not in text:
                missing.append(token)
    return missing


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--file", required=True)
    args = ap.parse_args()

    manifest = json.load(open(args.manifest))
    text = open(args.file).read()
    missing = verify(manifest, text)

    if missing:
        print(f"VERIFY FAIL — {len(missing)} manifest item(s) missing from narrative:")
        for m in missing:
            print(f"  - {m}")
        sys.exit(1)
    print(f"VERIFY PASS — all {len(manifest.get('verify_manifest', []))} manifest items present.")


if __name__ == "__main__":
    main()

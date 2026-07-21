#!/usr/bin/env python3
"""
correct_llm.py — mechanics for the CONTEXT-AWARE (in-session LLM) transcript
correction pass. The reasoning lives in /meeting-notes STEP 2.5 (the session IS
the LLM, no API key); THIS script only does the deterministic parts so the
guardrails are enforced in code, not by trusting the model:

  context  --stem <stem>            dump the context bundle for the session to
                                     reason over (attendees, topic, recent
                                     events.db activity, roster + vocab).
  apply    --stem <stem> --map FILE  apply the session-produced correction MAP
                                     deterministically, preserving every offset
                                     and line, then re-ingest + feed the loop.

Why a MAP, not a rewritten transcript: the model emits only
{wrong, right, kind, scope, confidence} pairs. This script substitutes them —
so offsets/line-count are preserved BY CONSTRUCTION and there is no path for the
model to summarize, rewrite, or hallucinate content into the transcript. It also
keeps model output tiny (a map, not a re-emitted transcript).

Deterministic + conservative:
  - kind must be name|term; anything else is dropped (NEVER content edits).
  - substitution is word-bounded regex, case-insensitive, applied to segment
    text only — offsets in the .json and the segment/line COUNT never change.
  - apply ABORTS (restores the raw backup) if line-count or any offset drifts.
  - only scope=global & confidence=high entries are persisted to
    transcribe_corrections.yaml (the cheap difflib layer catches them next time);
    meeting-scoped / low-confidence fixes touch THIS transcript only.

Local-only: reads gitignored config/ + index/events.db; commits no identifiers.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

WC = Path(__file__).resolve().parents[2]
ARCHIVE = WC / "transcripts" / "archive"
DB = WC / "index" / "events.db"
CORRECTIONS_YAML = WC / "config" / "transcribe_corrections.yaml"

# Generic meeting words that make useless topic-search keywords.
_STOP = {
    "standup", "sync", "huddle", "call", "meeting", "weekly", "daily", "adhoc",
    "review", "with", "and", "the", "for", "1-1", "one", "catch", "up", "chat",
    "teams", "slack", "google", "meet", "discussion", "session", "monthly",
}


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------
def _resolve(stem: str) -> Path:
    """Archive .txt path for a <date>-<slug> stem (globs YYYY-MM subdirs)."""
    hits = sorted(glob.glob(str(ARCHIVE / "*" / f"{stem}.txt")))
    if not hits:
        sys.exit(f"ERROR: no archived transcript for stem '{stem}' under {ARCHIVE}")
    return Path(hits[0])


def _slug_date(stem: str) -> tuple[str, str]:
    """<date>-<slug> → (date, slug). Date is the leading YYYY-MM-DD."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})-(.+)$", stem)
    if not m:
        sys.exit(f"ERROR: stem '{stem}' is not <YYYY-MM-DD>-<slug>")
    return m.group(1), m.group(2)


def _keywords(text: str) -> list[str]:
    seen, out = set(), []
    for tok in re.split(r"[^A-Za-z0-9]+", text or ""):
        tl = tok.lower()
        if len(tl) >= 3 and tl not in _STOP and tl not in seen:
            seen.add(tl)
            out.append(tok)
    return out[:4]


def _people_yaml() -> list[dict]:
    import yaml

    try:
        return (yaml.safe_load(open(WC / "config" / "people.yaml")) or {}).get("people", [])
    except Exception:
        return []


def _vocab() -> list[str]:
    import yaml

    try:
        return [str(t) for t in (yaml.safe_load(open(WC / "config" / "transcribe.yaml")) or {}).get("vocab", [])]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------
def cmd_context(stem: str) -> None:
    txt = _resolve(stem)
    date, slug = _slug_date(stem)
    subject = f"meeting:{date}:{slug}"

    # Title + attendees (calendar/DB title + the <stem>.people ground-truth sidecar)
    title = slug.replace("-", " ")
    if DB.exists():
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = conn.execute("SELECT title FROM events WHERE id = ?", (f"{subject}:meta",)).fetchone()
        if row and row[0]:
            title = row[0]
        conn.close()
    people_sidecar = txt.with_suffix("").parent / f"{stem}.people"
    attendees = []
    if people_sidecar.exists():
        attendees = [l.strip() for l in people_sidecar.read_text().splitlines() if l.strip()]

    # Recent events.db activity for the topic (reuse the meeting-brief keyword idea).
    kws = _keywords(f"{title} {slug}")
    activity: list[tuple] = []
    if DB.exists() and kws:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        like = " OR ".join(["title LIKE ?"] * len(kws))
        params = [f"%{k}%" for k in kws]
        q = (
            f"SELECT source, title, ts FROM events "
            f"WHERE source IN ('jira','github','slack','confluence') AND ({like}) "
            f"ORDER BY ts DESC LIMIT 12"
        )
        try:
            activity = conn.execute(q, params).fetchall()
        except sqlite3.Error:
            activity = []
        conn.close()

    # Roster (names + first names) + vocab — the correction targets.
    names = []
    for p in _people_yaml():
        nm = (p.get("name") or "").strip()
        if nm:
            names.append(nm)

    print("# CONTEXT BUNDLE (for STEP 2.5 correction — reason, do NOT paste back)")
    print(f"stem: {stem}")
    print(f"subject: {subject}")
    print(f"title: {title}")
    print(f"transcript: {txt}")
    print(f"attendees (.people sidecar, ground truth): {', '.join(attendees) or '(none — infer from transcript)'}")
    print(f"topic keywords: {', '.join(kws) or '(none)'}")
    print("\n## recent events.db activity on this topic (source | title | ts)")
    if activity:
        for src, t, ts in activity:
            print(f"- {src} | {t} | {ts}")
    else:
        print("- (no topic hits — correct from roster + vocab + transcript context alone)")
    print("\n## people.yaml roster (canonical spellings for name fixes)")
    print(", ".join(names) or "(empty)")
    print("\n## transcribe.yaml vocab (canonical jargon for term fixes)")
    print(", ".join(_vocab()) or "(empty)")


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
def _valid_entries(raw: list) -> list[dict]:
    """Keep only well-formed name/term corrections. Drops anything that could
    be a content edit (kind not in name|term) or a no-op."""
    out, seen = [], set()
    for e in raw:
        if not isinstance(e, dict):
            continue
        wrong = str(e.get("wrong", "")).strip()
        right = str(e.get("right", "")).strip()
        kind = str(e.get("kind", "")).strip().lower()
        if not wrong or not right or wrong == right:
            continue
        if kind not in ("name", "term"):
            continue  # GUARDRAIL: only names/terms, never content
        # GUARDRAIL: a name/term is a single line — reject any newline/control
        # char so a substitution can never split, merge, or reflow a line.
        if any(c in wrong or c in right for c in "\n\r\t"):
            continue
        key = (wrong.lower(), right)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "wrong": wrong,
            "right": right,
            "kind": kind,
            "scope": str(e.get("scope", "meeting")).strip().lower(),
            "confidence": str(e.get("confidence", "low")).strip().lower(),
        })
    return out


def _compiled(entries: list[dict]) -> list[tuple[re.Pattern, str]]:
    pats = []
    for e in entries:
        # word-bounded on alphanumerics so "rate" never hits "grateful", but
        # tokens with punctuation (e.g. CVS/CVST, hyphenated names) still match.
        pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(e['wrong'])}(?![A-Za-z0-9])", re.I)
        pats.append((pat, e["right"]))
    return pats


def _apply_lines(lines: list[str], pats: list[tuple[re.Pattern, str]]) -> list[str]:
    out = []
    for ln in lines:
        for pat, right in pats:
            ln = pat.sub(right, ln)
        out.append(ln)
    return out


def cmd_apply(stem: str, map_path: str) -> None:
    txt_path = _resolve(stem)
    json_path = txt_path.with_suffix(".json")
    raw_txt = txt_path.parent / f"{stem}.raw.txt"
    date, slug = _slug_date(stem)
    subject = f"meeting:{date}:{slug}"

    entries = _valid_entries(json.loads(Path(map_path).read_text()))
    if not entries:
        print("apply: no valid corrections in map — nothing to do")
        return
    pats = _compiled(entries)

    # --- .txt (display / Steno / notes source) — per-line, count preserved ---
    orig_text = txt_path.read_text(encoding="utf-8", errors="replace")
    orig_lines = orig_text.splitlines(keepends=True)
    new_lines = _apply_lines(orig_lines, pats)
    new_text = "".join(new_lines)
    # physical newline count too — catches a stray newline slipping into a value.
    if len(new_lines) != len(orig_lines) or new_text.count("\n") != orig_text.count("\n"):
        sys.exit("ABORT: .txt line count drifted — refusing to write (guardrail)")

    # --- .json (canonical offsets) — text-only substitution, offsets frozen ---
    data = None
    if json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
        segs = data.get("transcription", [])
        for seg in segs:
            before_off = dict(seg.get("offsets") or {})
            seg["text"] = _apply_lines([seg.get("text", "")], pats)[0]
            if dict(seg.get("offsets") or {}) != before_off:
                sys.exit("ABORT: a .json offset changed — refusing to write (guardrail)")

    # --- back up the raw once (earliest wins — mirrors the note .prev pattern) ---
    if not raw_txt.exists():
        raw_txt.write_text("".join(orig_lines), encoding="utf-8")

    txt_path.write_text(new_text, encoding="utf-8")
    if data is not None:
        json_path.write_text(json.dumps(data), encoding="utf-8")

    # --- re-ingest from the corrected .json, preserving the subject key -------
    reingested = False
    if json_path.exists() and DB.exists():
        start = f"{date}T12:00:00Z"
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = conn.execute("SELECT ts FROM events WHERE id = ?", (f"{subject}:meta",)).fetchone()
        conn.close()
        if row and row[0]:
            start = row[0]
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "ingest_transcript.py"),
             "--json", str(json_path), "--slug", slug, "--start", start],
            capture_output=True, text=True,
        )
        reingested = r.returncode == 0
        if not reingested:
            print(f"apply: WARN re-ingest failed (display .txt still updated): {r.stderr.strip()[:200]}")

    # --- feedback loop: persist only global + high-confidence to the yaml -----
    persisted = _persist_global(entries)

    # --- worth-it metric: how many were NEW beyond the deterministic layer ----
    new_beyond = _count_new_beyond_difflib(entries)

    print(
        f"apply: corrections={len(entries)} persisted_global_highconf={persisted} "
        f"new_beyond_difflib={new_beyond} reingested={reingested} raw_backup={raw_txt.name}"
    )


def _persist_global(entries: list[dict]) -> int:
    """Append global+high-confidence garbles to transcribe_corrections.yaml
    (phrases map). Append-only, dedup on existing keys."""
    import yaml

    keep = {e["wrong"]: e["right"] for e in entries
            if e["scope"] == "global" and e["confidence"] == "high"}
    if not keep:
        return 0
    data = {}
    if CORRECTIONS_YAML.exists():
        try:
            data = yaml.safe_load(CORRECTIONS_YAML.read_text()) or {}
        except Exception:
            data = {}
    phrases = data.get("phrases") or {}
    existing = {k.lower() for k in phrases}
    added = 0
    for wrong, right in keep.items():
        if wrong.lower() in existing:
            continue
        phrases[wrong] = right
        existing.add(wrong.lower())
        added += 1
    if added:
        data["phrases"] = phrases
        CORRECTIONS_YAML.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return added


def _count_new_beyond_difflib(entries: list[dict]) -> int:
    """A correction is 'new' if the deterministic layer does NOT already turn
    <wrong> into <right>. If this trends to ~0, the LLM pass isn't earning its
    in-session cost — rely on the (fatter) difflib layer instead."""
    try:
        from correct import correct_text
    except Exception:
        return len(entries)
    new = 0
    for e in entries:
        try:
            if correct_text(e["wrong"]).strip() != e["right"]:
                new += 1
        except Exception:
            new += 1
    return new


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("context", help="dump the context bundle for STEP 2.5")
    c.add_argument("--stem", required=True, help="<YYYY-MM-DD>-<slug>")
    a = sub.add_parser("apply", help="apply a correction map deterministically")
    a.add_argument("--stem", required=True, help="<YYYY-MM-DD>-<slug>")
    a.add_argument("--map", required=True, help="path to the correction-map JSON")
    args = ap.parse_args()

    if args.cmd == "context":
        cmd_context(args.stem)
    elif args.cmd == "apply":
        cmd_apply(args.stem, args.map)


if __name__ == "__main__":
    main()

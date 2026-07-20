#!/usr/bin/env python3
"""
build_vocab.py — assemble whisper's domain-vocabulary prompt (one line out).

Sources, in PRIORITY order (whisper's prompt budget is ~224 tokens, so the
cap truncates the tail — put must-haves first):
  1. curated terms        config/transcribe.yaml `vocab:`
  2. team first names     config/people.yaml scope:team
  3. service names        events.db github subjects (repo basenames, 90d,
                          most-active first) — internal service names are
                          spoken in almost every technical meeting
  4. active epic words    events.db jira Epic titles (90d) → distinctive
                          tokens ("sunset", "positive", "clearing"…)
  5. project keywords     config/projects.yaml

Output: comma-joined term list on stdout (empty on total failure — the
caller falls back to the static file). Used by bin/transcribe.sh.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

WC = Path(__file__).resolve().parents[2]
# Whisper's initial-prompt budget is ~224 tokens on EVERY model size (the
# decoder context is architectural — large-v3 gets no more than turbo).
# ~85 short terms fit; cap below that with a char guard for long terms.
CAP = 80
CHAR_BUDGET = 700  # ≈ 175 tokens of terms, leaving room for the carrier sentence

STOP = {
    "with", "from", "this", "that", "into", "over", "for", "and", "the",
    "phase", "part", "support", "update", "updates", "new", "old", "misc",
    "tech", "team", "task", "epic", "sprint", "poc", "flow", "flows",
    # generic ticket-title words — they waste prompt budget
    "status", "progress", "done", "enhancement", "refactor", "development",
    "improvement", "improvements", "implementation", "changes", "issues",
    "setup", "config", "feature", "features", "fixes", "testing",
}


def main() -> None:
    import yaml

    terms: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        t = str(t).strip()
        k = t.lower()
        if t and k not in seen and re.match(r"^[\w][\w .&-]{0,23}[A-Za-z0-9]$", t):
            seen.add(k)
            terms.append(t)

    # 1. curated
    try:
        for t in (yaml.safe_load(open(WC / "config" / "transcribe.yaml")) or {}).get("vocab", []):
            add(t)
    except Exception:
        pass

    # 2. team first names
    try:
        for p in (yaml.safe_load(open(WC / "config" / "people.yaml")) or {}).get("people", []):
            if p.get("scope") == "team" and p.get("name"):
                add(p["name"].split()[0])
    except Exception:
        pass

    # 3 + 4. events.db — service names + active epic vocabulary
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
        conn = sqlite3.connect(f"file:{WC / 'index' / 'events.db'}?mode=ro", uri=True)
        conn.execute("PRAGMA busy_timeout = 5000")

        svc = Counter()
        for (subj,) in conn.execute(
            "SELECT subject FROM events WHERE source='github' AND ts>=? "
            "AND subject LIKE '%/%'", (since,)):
            # subject form: owner/repo#N → repo basename (e.g. "orders-svc")
            m = re.match(r"[^/]+/([A-Za-z0-9._-]+)#", subj or "")
            if m:
                svc[m.group(1)] += 1
        for name, _ in svc.most_common(8):
            add(name)

        words = Counter()
        for (title,) in conn.execute(
            "SELECT DISTINCT title FROM events WHERE source='jira' "
            "AND issue_type='Epic' AND ts>=? AND title IS NOT NULL", (since,)):
            for w in re.findall(r"[A-Za-z]{4,}", title):
                if w.lower() not in STOP:
                    words[w.lower()] += 1
        for w, _ in words.most_common(12):
            add(w)
    except Exception:
        pass

    # 5. project keywords
    try:
        prj = yaml.safe_load(open(WC / "config" / "projects.yaml")) or []
        entries = prj if isinstance(prj, list) else prj.get("projects", [])
        kws = []
        for e in entries:
            if isinstance(e, dict):
                kws += [k for k in (e.get("keywords") or [])[:2] if isinstance(k, str)]
        for k in kws[:12]:
            add(k)
    except Exception:
        pass

    out, used = [], 0
    for t in terms[:CAP]:
        if used + len(t) + 2 > CHAR_BUDGET:
            break
        out.append(t)
        used += len(t) + 2
    print(", ".join(out))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
meet_ui.py — local Granola-style UI for the meeting-intelligence pipeline.

    python3 bin/meet_ui.py          → http://127.0.0.1:8787

Left: today's calendar + recorded meetings. Right: live recording banner with
a scratchpad (typed bullets land next to the audio and drive /meeting-notes),
or the selected meeting's note + transcript.

Local-only by construction: binds 127.0.0.1, serves data straight off disk
(events.db not required). No external assets, no telemetry, nothing written
outside the existing pipeline paths (.capture/live.notes.md).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WC = REPO / "work-context"
CAP = WC / "transcripts" / ".capture"
ARCHIVE = WC / "transcripts" / "archive"
INBOX = WC / "transcripts" / "inbox"
HOLD = WC / "transcripts" / "hold"
NOTES_DIR = REPO / "management" / "meetings"
SIGNALS = WC / "state" / "meeting_signals.json"
PORT = 8788  # 8787 is taken by another local server on this machine

# Tiny TTL memo. The two per-request costs that made the UI stall on every
# tab switch were (a) spawning calendar_feed.py (python cold-start + ICS parse)
# on /api/today + /api/live, and (b) re-scanning ~600 archive files in
# meetings(). Cache both for a few seconds — the data barely changes.
_CACHE: dict = {}


def _cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


def _venv_py() -> str:
    for cand in (WC / ".venv" / "bin" / "python3", Path(sys.executable)):
        if Path(cand).exists():
            return str(cand)
    return "python3"


def rec_status() -> dict:
    pidf = CAP / "pid"
    out = {"recording": False}
    try:
        pid_s, label, started = pidf.read_text().split()
        import os
        os.kill(int(pid_s), 0)
        log = (CAP / "capture.log").read_text(errors="replace") if (CAP / "capture.log").exists() else ""
        out = {
            "recording": True,
            "label": label,
            "elapsed": int(time.time()) - int(started),
            "mode": "mic-only" if "mic-only" in log else "full",
            "auto": (CAP / "auto").exists(),
        }
    except Exception:
        pass
    # Persistent in-person nudge (meet_watch writes it while a calendar meeting is
    # live but nothing is recording) → surfaced as a Steno banner so a missed
    # macOS notification doesn't mean a mislabeled/unrecorded meeting.
    if not out.get("recording"):
        try:
            title, nend = (CAP / "nudge").read_text().strip().rsplit("|", 1)
            if int(nend) > time.time():
                out["nudge"] = title
        except Exception:
            pass
        # Pre-call nudge (meet_watch writes it before a scheduled meeting starts)
        # → a "meeting soon — arm to record?" banner. Same missed-notification
        # backstop as the live nudge. Only while the meeting is still upcoming.
        try:
            parts = (CAP / "prenudge").read_text().strip().split("|")
            if int(parts[1]) > time.time():
                out["prenudge"] = parts[0]
                out["prenudge_mins"] = parts[2] if len(parts) > 2 else ""
        except Exception:
            pass
    return out


def _search_stems(q: str) -> set[str]:
    """Meeting stems matching q: transcript FTS (events.db) + note-file grep."""
    hits: set[str] = set()
    q = q.strip()
    if not q:
        return hits
    try:
        import sqlite3
        conn = sqlite3.connect(str(WC / "index" / "events.db"))
        conn.execute("PRAGMA busy_timeout = 5000")
        safe = '"' + q.replace('"', '""') + '"'
        for (subj,) in conn.execute(
            "SELECT DISTINCT e.subject FROM events_fts f JOIN events e ON e.rowid=f.rowid "
            "WHERE events_fts MATCH ? AND e.source='meeting'", (safe,)):
            # meeting:<date>:<slug> → <date>-<slug>
            parts = (subj or "").split(":", 2)
            if len(parts) == 3:
                hits.add(f"{parts[1]}-{parts[2]}")
    except Exception:
        pass
    ql = q.lower()
    for note in NOTES_DIR.glob("*.md"):
        try:
            if ql in note.read_text(errors="replace").lower():
                hits.add(note.stem)
        except Exception:
            pass
    return hits


def meetings(q: str = "", limit: int | None = None) -> list[dict]:
    """One row per MEETING. A call that dropped/restarted produces several
    audio segments (same slug, different stop-times) — group them so the
    sidebar shows 'positive pay handover · 2 parts', not three rows.
    With q: filter to title matches + transcript/note content hits.
    Raw (not-yet-transcribed) recordings from the inbox/hold queue are listed
    too, badged transcribed:False — so a meeting shows up the moment it's
    recorded, not only after transcription."""
    rows = _meetings_rows(q) if q else _cached("meetings", 8, lambda: _meetings_rows(""))
    return rows[: (limit or (100 if q else 60))]


def _meetings_rows(q: str = "") -> list[dict]:
    stem_hits = _search_stems(q) if q else None
    # Dominant project slug per meeting — enrich_refs tags transcript segments
    # with projects.yaml slugs at ingest; the top slug makes meetings sliceable
    # by project like every other synapse source.
    proj_by_stem: dict = {}
    try:
        import sqlite3
        conn = sqlite3.connect(str(WC / "index" / "events.db"))
        conn.execute("PRAGMA busy_timeout = 5000")
        for subj, slug, _ in conn.execute(
            "SELECT e.subject, r.ref_value, COUNT(*) c FROM events e "
            "JOIN event_refs r ON r.event_id=e.id AND r.ref_type='project' "
            "WHERE e.source='meeting' GROUP BY 1,2 ORDER BY c ASC"):
            parts = (subj or "").split(":", 2)
            if len(parts) == 3:
                proj_by_stem[f"{parts[1]}-{parts[2]}"] = slug  # last row = max count
    except Exception:
        pass
    groups: dict = {}
    for txt in sorted(ARCHIVE.glob("*/*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:600]:
        # Skip the per-speaker stream transcripts (<stem>.me.txt/.them.txt) —
        # only the merged <stem>.txt is a real meeting row.
        if txt.name.endswith((".me.txt", ".them.txt")):
            continue
        stem = txt.stem  # 2026-07-17-<slug>[-HHMM]
        date, slug = stem[:10], stem[11:]
        base = re.sub(r"-\d{4,6}$", "", slug)
        title = base.replace("-", " ").strip() or slug
        m = re.search(r"-(\d{2})(\d{2})(?:\d{2})?$", slug)
        seg = {
            "id": stem,
            "has_note": (NOTES_DIR / f"{stem}.md").exists(),
            "size": txt.stat().st_size,
            "time": f"{m.group(1)}:{m.group(2)}" if m else "",
        }
        g = groups.setdefault((date, base), {"date": date, "title": title, "segs": []})
        g["segs"].append(seg)
        if stem in proj_by_stem:
            g.setdefault("proj", proj_by_stem[stem])
        # Owner's manual category sidecar beats the AI classification.
        cat_f = NOTES_DIR / f"{stem}.cat"
        if cat_f.exists():
            g["cat"] = cat_f.read_text().strip()
            g["cat_manual"] = True
        # The generated note's H1 is the best display title — it carries the
        # inferred counterpart for huddles ("Huddle with Alex").
        if seg["has_note"] and "note_title" not in g:
            try:
                head = (NOTES_DIR / f"{stem}.md").read_text(errors="replace").lstrip()
                cm = re.search(r"<!--\s*category:\s*([\w-]+)\s*-->", head[:400])
                if cm and not g.get("cat_manual"):
                    g["cat"] = cm.group(1)
                first = head.splitlines()[0]
                if first.startswith("#"):
                    t = first.lstrip("# ").strip()
                    # Notes sometimes carry dates/stems in the H1 — strip them,
                    # cut on a word boundary, drop dangling punctuation.
                    t = re.sub(r"\s*[(\[—–-]*\s*\d{4}-\d{2}-\d{2}.*$", "", t)
                    t = re.sub(r"[-a-z0-9]*\d{4,6}\s*$", "", t).strip(" -—–(,")
                    if len(t) > 58:
                        t = t[:58].rsplit(" ", 1)[0] + "…"
                    if t:
                        g["note_title"] = t
            except Exception:
                pass

    rows = []
    for g in groups.values():
        segs = sorted(g["segs"], key=lambda s: (s["has_note"], s["size"]), reverse=True)
        ordered = sorted(g["segs"], key=lambda s: s["id"])
        if stem_hits is not None:
            title_all = (g.get("note_title") or "") + " " + g["title"]
            if not (q.lower() in title_all.lower()
                    or any(s["id"] in stem_hits for s in g["segs"])):
                continue
        rows.append({
            "id": segs[0]["id"],                    # best segment opens by default
            "date": g["date"],
            "title": g.get("note_title") or g["title"],
            "cat": g.get("cat", ""),
            "proj": g.get("proj", ""),
            "time": ordered[0]["time"],
            "has_note": any(s["has_note"] for s in segs),
            "transcribed": True,
            "starred": any((NOTES_DIR / f"{s['id']}.star").exists() for s in segs),
            "n": len(segs),
            "segs": [s["id"] for s in ordered],
        })

    # Raw recordings still awaiting transcription (inbox = auto/manual queue,
    # hold = deliberately set aside). List them so a meeting is visible the
    # instant it's captured — "worst case, show it even without a transcript".
    # An archive m4a whose merged transcript already exists is skipped via
    # `seen` (its transcribed row was built above).
    import datetime as _dt
    seen = {sid for r in rows for sid in r["segs"]}
    for d in (INBOX, HOLD, *sorted(ARCHIVE.glob("*"))):
        if not d.is_dir():
            continue
        for m4a in d.glob("*.m4a"):
            stem = m4a.stem
            date = _dt.datetime.fromtimestamp(m4a.stat().st_mtime).strftime("%Y-%m-%d")
            rid = f"{date}-{stem}"
            if rid in seen:
                continue
            seen.add(rid)
            base = re.sub(r"-\d{4,6}$", "", stem)
            title = base.replace("-", " ").strip() or stem
            mt = re.search(r"-(\d{2})(\d{2})(?:\d{2})?$", stem)
            row = {
                "id": rid, "date": date,
                "title": title, "cat": "", "proj": "",
                "time": f"{mt.group(1)}:{mt.group(2)}" if mt else "",
                "has_note": False, "transcribed": False,
                "n": 1, "segs": [rid],
            }
            if stem_hits is None or q.lower() in title.lower():
                rows.append(row)

    rows.sort(key=lambda r: (r["date"], r["time"]), reverse=True)
    return rows


# Voice-match confidence gate for PRE-FILLING a name suggestion in the UI
# (mirror of voice_gallery.DEFAULT_THRESHOLD). A suggestion is never auto-applied
# to a note — the owner confirms it here first.
VOICE_THRESHOLD = 0.55
DIAR_PY = Path.home() / ".steno-diarize" / "venv" / "bin" / "python3"


def _roster() -> list:
    """[{handle, name}] from people.yaml, for the speaker-assignment dropdown."""
    import yaml

    try:
        ppl = (yaml.safe_load(open(WC / "config" / "people.yaml")) or {}).get("people", [])
    except Exception:
        return []
    out = [{"handle": p.get("canonical"), "name": (p.get("name") or "").strip() or p.get("canonical")}
           for p in ppl if p.get("canonical")]
    out.sort(key=lambda r: r["name"].lower())
    return out


def _name_for(handle: str, roster: list) -> str:
    for r in roster:
        if r["handle"] == handle:
            return r["name"]
    return handle


def _speakers_payload(mid: str, month: str) -> dict | None:
    """Parse <mid>.speakers.json into a UI payload: per-speaker current identity +
    voice-match suggestion + the roster for the dropdown. None if not diarized."""
    f = ARCHIVE / month / f"{mid}.speakers.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(errors="replace"))
    except Exception:
        return None
    roster = _roster()
    lst = []
    for cluster, e in sorted(data.items(), key=lambda kv: kv[1].get("display", kv[0])):
        name, handle = e.get("name"), e.get("handle")
        auto, score = e.get("auto"), float(e.get("score") or 0)
        if name:
            effective = name
        elif handle:
            effective = _name_for(handle, roster)
        else:
            effective = e.get("display", cluster)
        # A voice suggestion only surfaces when unconfirmed and above the gate.
        suggestion = (_name_for(auto, roster)
                      if auto and score >= VOICE_THRESHOLD and not handle and not name else None)
        lst.append({
            "cluster": cluster, "display": e.get("display", cluster),
            "handle": handle, "name": name, "effective": effective,
            "score": round(score, 2), "suggestion": suggestion, "suggestion_handle": auto,
        })
    return {"list": lst, "roster": roster}


def meeting_detail(mid: str) -> dict:
    mid = re.sub(r"[^a-zA-Z0-9_-]", "", mid)
    month = mid[:7]
    txt = ARCHIVE / month / f"{mid}.txt"
    note = NOTES_DIR / f"{mid}.md"
    scratch = next(iter(ARCHIVE.glob(f"*/{mid}.notes.md")), None)
    links_f = ARCHIVE / month / f"{mid}.links"
    links = [l.strip() for l in links_f.read_text().splitlines() if l.strip()] if links_f.exists() else []
    import re as _re
    cat = ""
    cat_f = NOTES_DIR / f"{mid}.cat"
    if cat_f.exists():
        cat = cat_f.read_text().strip()
    elif note.exists():
        cm = _re.search(r"<!--\s*category:\s*([\w-]+)\s*-->", note.read_text(errors="replace")[:400])
        cat = cm.group(1) if cm else ""
    ppl_f = ARCHIVE / month / f"{mid}.people"
    participants = [l.strip() for l in ppl_f.read_text().splitlines() if l.strip()] if ppl_f.exists() else []
    return {
        "id": mid,
        "cat": cat,
        "participants": participants,
        "starred": (NOTES_DIR / f"{mid}.star").exists(),
        "transcribed": txt.exists(),
        "transcript": txt.read_text(errors="replace") if txt.exists() else "(not transcribed yet)",
        "note": note.read_text(errors="replace") if note.exists() else "",
        "scratchpad": scratch.read_text(errors="replace") if scratch else "",
        "links": links,
        # note deleted by a regen request → the routine will rebuild it;
        # keep showing the previous version (dimmed) until the new one lands.
        "queued": not note.exists() and (NOTES_DIR / f"{mid}.md.prev").exists(),
        "note_prev": (NOTES_DIR / f"{mid}.md.prev").read_text(errors="replace")
        if not note.exists() and (NOTES_DIR / f"{mid}.md.prev").exists() else "",
        "mom": (NOTES_DIR / f"{mid}.mom.md").read_text(errors="replace")
        if (NOTES_DIR / f"{mid}.mom.md").exists() else "",
        "mom_queued": (NOTES_DIR / f"{mid}.mom.request").exists(),
        # Redacted-for-sharing export (owner reviews, then sends by hand — never
        # auto-sent). Shown as its own tab once generated via the Share button.
        "share": (NOTES_DIR / f"{mid}.share.md").read_text(errors="replace")
        if (NOTES_DIR / f"{mid}.share.md").exists() else "",
        # Diarized-speaker identities (in-person meetings) — None if not diarized.
        "speakers": _speakers_payload(mid, month),
    }


def _valid_link(u: str) -> str:
    u = u.strip()
    if re.match(r"^https?://\S+$", u) or re.match(r"^[A-Z][A-Z0-9]+-\d+$", u):
        return u  # URL or bare Jira key
    return ""


def today() -> list[str]:
    return _cached("today", 60, _today_uncached)


def _today_uncached() -> list[str]:
    import datetime
    d = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
    try:
        r = subprocess.run(
            [_venv_py(), str(WC / "derive" / "meetings" / "calendar_feed.py"), "day", d],
            capture_output=True, text=True, timeout=90)
        # Sidebar shows real calls only — Lunch/Busy/focus blocks have no
        # teams link and are noise here.
        lines = [l.strip() for l in r.stdout.splitlines()
                 if l.strip() and "| teams |" in l]
        return lines[:8]
    except Exception as e:
        return [f"(calendar unavailable: {e})"]


def live_events() -> list[dict]:
    """All calendar events live right now (ACTIVE + ALT lines from the feed)."""
    return _cached("live", 20, _live_uncached)


def _live_uncached() -> list[dict]:
    try:
        r = subprocess.run(
            [_venv_py(), str(WC / "derive" / "meetings" / "calendar_feed.py"), "now"],
            capture_output=True, text=True, timeout=90)
        out = []
        for line in r.stdout.splitlines():
            parts = line.strip().split("|")
            if len(parts) >= 5 and parts[0] in ("ACTIVE", "ALT"):
                out.append({"slug": parts[1], "title": parts[4]})
        return out
    except Exception:
        return []


def signals() -> dict:
    try:
        return json.loads(SIGNALS.read_text())
    except Exception:
        return {"commitments": [], "asks": [], "untracked": []}


# ── "My action items" (To-do) ────────────────────────────────────────────────
# Feature paused 2026-07-24 (owner request: refine transcription/notes first).
# Flip to True to restore; store backup: state/meeting_signals.json.disabled-2026-07-24
TODO_FEATURE = False

# The signal STORE and its owner-facing filter live in derive/meetings/signals.py
# — we load that module by path (derive/ has no package __init__) and REUSE its
# logic rather than reimplementing the JSON shape or the attribution rules here.
_MODS: dict = {}


def _load_mod(path: Path, name: str):
    import importlib.util
    if name not in _MODS:
        spec = importlib.util.spec_from_file_location(name, str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _MODS[name] = mod
    return _MODS[name]


def _sig():
    return _load_mod(WC / "derive" / "meetings" / "signals.py", "meet_signals")


def _owner_handle() -> str | None:
    """Owner's canonical handle: config org.owner_email → people.yaml canonical —
    the same identity resolution ingest uses. None if unresolved (no people.yaml
    or no match); the filter then never guesses an item is the owner's."""
    def resolve():
        try:
            sc = _load_mod(WC / "derive" / "sources_config.py", "meet_sources_config")
            email = sc.owner_email()
        except Exception:
            return None
        try:
            import yaml
            ppl = (yaml.safe_load((WC / "config" / "people.yaml").read_text()) or {}).get("people", [])
        except Exception:
            return None
        for p in ppl:
            if p.get("email") == email and p.get("canonical"):
                return p["canonical"]
        return None
    return _cached("owner_handle", 300, resolve)


def _todo_meeting_map() -> dict:
    """stem/base → {title, open, segs} for every listed meeting, so a signal's
    subject (meeting:<date>:<slug>) resolves to a display title + a clickable id."""
    def build():
        idx: dict = {}
        for r in meetings():
            info = {"title": r["title"], "open": r["id"], "segs": r["segs"]}
            for stem in r["segs"]:
                idx[stem] = info
                idx.setdefault(re.sub(r"-\d{4,6}$", "", stem), info)  # base (no -HHMM)
        return idx
    return _cached("todo_mmap", 8, build)


def _enrich_meeting(subject: str, idx: dict) -> dict:
    if not subject.startswith("meeting:"):
        return {"title": "", "open": "", "segs": []}
    key = subject[len("meeting:"):].replace(":", "-", 1)  # meeting:D:S → D-S
    info = idx.get(key)
    if not info:
        for k, v in idx.items():
            if k.startswith(key + "-") or key.startswith(k):
                info = v
                break
    if info:
        return {"title": info["title"], "open": info["open"], "segs": info["segs"]}
    slug = key[11:] if re.match(r"\d{4}-\d\d-\d\d-", key) else key  # drop date prefix
    return {"title": slug.replace("-", " ").strip(), "open": "", "segs": []}


def todos() -> dict:
    if not TODO_FEATURE:
        return {"items": [], "follow_up": [], "suggestions": [], "done": [],
                "untracked": [], "count": 0, "follow_up_count": 0,
                "suggestion_count": 0, "done_count": 0,
                "owner_resolved": True, "disabled": True}
    owner = _owner_handle()
    try:
        sig = _sig()
        items = sig.owner_facing_todos(owner)
        follow_up = sig.follow_up_items(owner, with_evidence=True)
        suggestions = sig.owner_suggestions()
        done = sig.owner_facing_todos(owner, status="done")
        untracked = sig.owner_untracked()
    except Exception:
        items, follow_up, suggestions, done, untracked = [], [], [], [], []
    idx = _todo_meeting_map()
    for it in items + follow_up + suggestions + done + untracked:
        it["meeting"] = _enrich_meeting(it.get("subject", ""), idx)
    # Follow-ups show the teammate's display name, not the raw handle.
    roster = _roster()
    for it in follow_up:
        it["who_name"] = _name_for(it.get("who", ""), roster)
    # Completed view = most-recently-resolved first.
    done.sort(key=lambda it: it.get("resolved_ts", ""), reverse=True)
    return {"items": items, "follow_up": follow_up, "suggestions": suggestions,
            "done": done, "untracked": untracked,
            "count": len(items), "follow_up_count": len(follow_up),
            "suggestion_count": len(suggestions), "done_count": len(done),
            "owner_resolved": bool(owner)}


def _todo_action(iid: str, verb: str) -> dict:
    if not TODO_FEATURE:
        return {"ok": False}
    iid = re.sub(r"[^a-zA-Z0-9-]", "", iid)
    if not iid:
        return {"ok": False}
    r = subprocess.run(
        [_venv_py(), str(WC / "derive" / "meetings" / "signals.py"), verb, iid],
        capture_output=True, text=True)
    return {"ok": r.returncode == 0}


def todo_resolve(iid: str) -> dict:
    return _todo_action(iid, "resolve")


def todo_reopen(iid: str) -> dict:
    return _todo_action(iid, "reopen")


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>Steno</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--paper:#f7f7f7;--ink:#1a1a1a;--muted:#8b8b8b;--line:#e6e6e6;--card:#ffffff;
--accent:#c2410c;--good:#4d7c0f;--sel:#eeeeee;--recbg:#fdf0ea;--recline:#f0c9b6;--raw:#d4d4d4}
html[data-theme=dark]{--paper:#161616;--ink:#ececec;--muted:#9a9a9a;--line:#2c2c2c;
--card:#1e1e1e;--accent:#f97316;--good:#84cc16;--sel:#2a2a2a;--recbg:#2a1c13;--recline:#5c3524;--raw:#4a4a4a}
*{box-sizing:border-box;margin:0}
body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,sans-serif;
background:var(--paper);color:var(--ink);display:flex;height:100vh;overflow:hidden}
#side{width:300px;min-width:300px;border-right:1px solid var(--line);padding:18px 14px;
overflow-y:auto;background:var(--card)}
#main{flex:1;overflow-y:auto;padding:26px 34px}
h1{font-size:17px;font-weight:700;letter-spacing:-.02em;margin-bottom:14px;display:flex;align-items:center}
h2{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;
letter-spacing:.08em;margin:18px 0 8px}
#theme{margin-left:auto;border:1px solid var(--line);background:var(--paper);color:var(--muted);
border-radius:8px;padding:2px 9px;font-size:11.5px;cursor:pointer}
/* calendar timeline */
.crow{display:flex;align-items:center;gap:8px;padding:4px 6px;border-radius:8px;font-size:12.5px}
.crow .ct{width:76px;min-width:76px;text-align:right;color:var(--muted);
font-variant-numeric:tabular-nums;font-size:11.5px}
.crow .cbar{width:3px;align-self:stretch;border-radius:2px;background:var(--line)}
.crow .cname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.crow.past{opacity:.45}
.crow.now .cbar{background:var(--accent)}
.crow.now .cname{font-weight:700}
.crow.now{background:var(--sel)}
/* meeting list */
.mt{padding:8px 10px;border-radius:10px;cursor:pointer;margin-bottom:2px}
.mt:hover{background:var(--paper)}
.mt.sel{background:var(--sel)}
.mt .title{font-size:13.5px;font-weight:600;display:flex;align-items:baseline;gap:7px}
.mt .mtime{color:var(--muted);font-weight:400;font-size:11.5px;font-variant-numeric:tabular-nums;min-width:34px}
.mt .tt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-transform:capitalize}
.mt .sub{font-size:11.5px;color:var(--muted);margin-left:41px}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-left:auto;flex-shrink:0}
.dot.note{background:var(--good)}
.dot.raw{background:var(--raw)}
#rec{display:none;background:var(--recbg);border:1px solid var(--recline);border-radius:14px;
padding:14px 18px;margin-bottom:22px;align-items:center;gap:12px}
#rec.on{display:flex}
.pulse{width:12px;height:12px;border-radius:50%;background:var(--accent);
animation:pu 1.6s infinite}
@keyframes pu{0%{box-shadow:0 0 0 0 rgba(194,65,12,.45)}70%{box-shadow:0 0 0 10px rgba(194,65,12,0)}100%{box-shadow:0 0 0 0 rgba(194,65,12,0)}}
#rec .lbl{font-weight:700;text-transform:capitalize}
#rec .meta{color:var(--muted);font-size:13px}
#rec button{margin-left:auto;border:1px solid var(--line);background:var(--card);color:var(--ink);
border-radius:9px;padding:6px 14px;font-size:13px;cursor:pointer}
#rec select{background:var(--card);color:var(--ink)}
#pad{width:100%;min-height:180px;border:1px solid var(--line);border-radius:12px;
background:var(--card);color:var(--ink);padding:14px 16px;font:14px/1.6 ui-monospace,Menlo,monospace;
resize:vertical;outline:none;display:none}
#pad:focus{border-color:var(--accent)}
#padhint{display:none;font-size:12px;color:var(--muted);margin:6px 2px 0}
#linkin{color:var(--ink)}
#view h1{font-size:22px;text-transform:capitalize;margin-bottom:2px}
#view .date{color:var(--muted);font-size:13px;margin-bottom:18px}
.tabs{display:flex;gap:6px;margin-bottom:14px}
.tabs button{border:1px solid var(--line);background:var(--card);border-radius:9px;
padding:5px 14px;font-size:13px;cursor:pointer;color:var(--muted)}
.tabs button.on{background:var(--ink);color:var(--paper);border-color:var(--ink)}
#content{max-width:760px}
#content .md{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:24px 28px}
#content pre{white-space:pre-wrap;font:13px/1.7 ui-monospace,Menlo,monospace;
background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px 24px}
#content textarea{color:var(--ink)}
.md h3{margin:16px 0 6px;font-size:15px}
.md li{margin-left:20px;margin-bottom:3px}
.md p{margin-bottom:8px}
.empty{color:var(--muted);padding:60px 0;text-align:center}
a{color:var(--accent)}
.cardgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;max-width:1000px}
.mcard{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;cursor:pointer;transition:border-color .12s}
.mcard:hover{border-color:var(--muted)}
.back{border:none;background:none;color:var(--muted);cursor:pointer;font-size:13px;padding:0;margin-bottom:10px}
.back:hover{color:var(--accent)}
/* To-do nav (sidebar) + rows */
#todonav{display:flex;align-items:center;gap:7px;padding:7px 10px;border-radius:9px;cursor:pointer;
font-size:13px;font-weight:600;margin-bottom:14px;border:1px solid var(--line);background:var(--paper)}
#todonav:hover{border-color:var(--muted)}
#todonav.on{background:var(--ink);color:var(--paper);border-color:var(--ink)}
#todocount{margin-left:auto;background:var(--accent);color:#fff;border-radius:11px;
padding:0 8px;font-size:11px;font-weight:700;min-width:20px;text-align:center;display:none}
.tdgrp{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
color:var(--muted);margin:22px 0 9px}
.tdgrp.over{color:#b91c1c}
.todorow{display:flex;align-items:flex-start;gap:12px;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:11px 14px;margin-bottom:8px;max-width:820px}
.todorow .txt{flex:1;font-size:14px;line-height:1.4}
.todorow .sub{font-size:11.5px;color:var(--muted);margin-top:5px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.kbadge{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
border-radius:6px;padding:1px 7px;border:1px solid currentColor}
.tddone{border:1px solid var(--line);background:var(--paper);color:var(--good);border-radius:9px;
width:30px;height:30px;flex-shrink:0;font-size:15px;cursor:pointer;line-height:1}
.tddone:hover{background:var(--good);color:#fff;border-color:var(--good)}
.tdsrc{color:var(--accent);text-decoration:none}.tdsrc:hover{text-decoration:underline}
</style></head><body>
<div id="side">
  <h1><svg width="20" height="20" viewBox="0 0 1024 1024" style="vertical-align:-4px;margin-right:6px"><rect width="1024" height="1024" rx="232" fill="#1f1d1a"/><g stroke="#c2410c" stroke-width="58" stroke-linecap="round" fill="none"><path d="M 232 547 v -70"/><path d="M 340 607 v -190"/><path d="M 448 572 v -120"/></g><g stroke="#faf6ee" stroke-width="58" stroke-linecap="round" fill="none"><path d="M 560 392 h 232"/><path d="M 560 512 h 232"/><path d="M 560 632 h 150"/></g></svg>Steno <span style="color:var(--muted);font-weight:400">· local</span><button id="theme" onclick="cycleTheme()">auto</button></h1>
  <!-- TODO-DISABLED: restore onclick="showTodos()" + original title to re-enable -->
  <div id="todonav" style="opacity:.4;cursor:default;pointer-events:none" title="To-do is paused for now">
    <span>✓ To-do</span><span id="todocount"></span></div>
  <h2>Today</h2><div id="today" style="margin-bottom:14px"></div>
  <div id="sidecal" style="margin-bottom:16px"></div>
  <h2 style="display:flex;align-items:center">Search
    <button onclick="showLib()" style="margin-left:auto;border:1px solid var(--line);background:var(--paper);color:var(--muted);border-radius:6px;padding:1px 8px;font-size:10px;cursor:pointer;letter-spacing:0;text-transform:none">browse all ↗</button></h2>
  <input id="msearch" placeholder="search titles, transcripts, notes…"
    style="width:100%;border:1px solid var(--line);border-radius:9px;padding:6px 10px;
    font-size:12.5px;background:var(--paper);color:var(--ink);outline:none;margin-bottom:6px">
  <div id="list"></div>
</div>
<div id="main">
  <div id="nudge" style="display:none;align-items:center;gap:10px;margin-bottom:14px;
    background:var(--recbg);border:1px solid var(--recline);border-radius:14px;padding:12px 16px">
    <div style="flex:1;font-size:13.5px;color:var(--accent)"><b id="nudgetitle"></b> is live — not recording.</div>
    <button onclick="nudgeRec()" style="border:1px solid var(--recline);background:var(--card);
      color:var(--accent);border-radius:9px;padding:6px 16px;font-size:13px;font-weight:600;cursor:pointer">● Record it</button>
  </div>
  <div id="prenudge" style="display:none;align-items:center;gap:10px;margin-bottom:14px;
    background:var(--card);border:1px dashed var(--recline);border-radius:14px;padding:12px 16px">
    <div style="flex:1;font-size:13.5px;color:var(--accent)">Upcoming: <b id="prenudgetitle"></b> <span id="prenudgemins" style="opacity:.7"></span></div>
    <button onclick="prenudgeRec()" style="border:1px solid var(--recline);background:var(--card);
      color:var(--accent);border-radius:9px;padding:6px 16px;font-size:13px;font-weight:600;cursor:pointer">● Arm &amp; record</button>
  </div>
  <div id="startrow" style="display:none;align-items:center;gap:10px;margin-bottom:22px;
    background:var(--card);border:1px dashed var(--line);border-radius:14px;padding:12px 16px">
    <input id="startlbl" placeholder="in-person meeting — name it (optional)"
      style="flex:1;border:none;background:transparent;outline:none;font-size:14px;color:var(--ink)"
      onkeydown="if(event.key==='Enter')startRec()">
    <button onclick="startRec()" style="border:1px solid var(--recline);background:var(--recbg);
      color:var(--accent);border-radius:9px;padding:6px 16px;font-size:13px;font-weight:600;cursor:pointer">● Record</button>
  </div>
  <div id="rec">
    <div class="pulse"></div>
    <div><div class="lbl" id="reclbl"></div><div class="meta" id="recmeta"></div></div>
    <select id="picker" style="display:none;margin-left:12px;border:1px solid var(--line);border-radius:8px;padding:5px 8px;font-size:12.5px;background:#fff" onchange="relabel(this.value)"></select>
    <button onclick="toggleRec()">Stop</button>
  </div>
  <textarea id="pad" placeholder="Jot rough notes here during the call — they'll shape the AI notes…"></textarea>
  <div id="linkrow" style="display:none;margin-top:8px">
    <input id="linkin" placeholder="attach context — slack thread / confluence / jira link, press Enter"
      style="width:100%;border:1px solid var(--line);border-radius:10px;padding:8px 12px;font-size:13px;background:var(--card);outline:none">
    <div id="livelinks" style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px"></div>
  </div>
  <div id="padhint">autosaves · merged into the meeting note after the call</div>
  <div id="view"><div class="empty">Select a meeting — or just be in one, recording starts itself.</div></div>
</div>
<script>
let sel=null, tab='note', padTimer=null, detail=null, recEl=0, recSuffix='', recOn=false;
const $=id=>document.getElementById(id);
// HTML-escape for values dropped into innerHTML (to-do text + titles come from
// LLM-extracted transcript content — never trust them in markup).
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fmt(s){const m=Math.floor(s/60);return m+':'+String(s%60).padStart(2,'0')}
function md(t){return t.replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h3>$1</h3>')
 .replace(/^- (.*)$/gm,'<li>$1</li>').replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')
 .replace(/\[(.+?)\]\((.+?)\)/g,'<a href="$2" target="_blank">$1</a>')
 .replace(/^(?!<h3|<li)(.+)$/gm,'<p>$1</p>')}
async function poll(){
 const s=await (await fetch('/api/status')).json();
 $('rec').className = s.recording ? 'on' : '';
 const nud = s.nudge && !s.recording;
 $('nudge').style.display = nud ? 'flex':'none';
 if(nud) $('nudgetitle').textContent = s.nudge;
 const pnud = s.prenudge && !s.recording;
 $('prenudge').style.display = pnud ? 'flex':'none';
 if(pnud){ $('prenudgetitle').textContent = s.prenudge;
   $('prenudgemins').textContent = s.prenudge_mins ? '· starts in '+s.prenudge_mins+'m' : ''; }
 $('startrow').style.display = s.recording ? 'none':'flex';
 $('pad').style.display = s.recording ? 'block':'none';
 $('padhint').style.display = s.recording ? 'block':'none';
 $('linkrow').style.display = s.recording ? 'block':'none';
 if(s.recording){const ls=await (await fetch('/api/livelinks')).json();
   $('livelinks').innerHTML=ls.map(u=>`<span style="font-size:11.5px;background:var(--card);border:1px solid var(--line);border-radius:7px;padding:2px 8px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${u}</span>`).join('')}
 recOn=s.recording;
 if(s.recording){
   recEl=s.elapsed;
   recSuffix=' · '+(s.mode==='full'?'both sides':'mic only')+(s.auto?' · auto':' · manual');
   $('reclbl').textContent=(s.label||'').replaceAll('-',' ');
   $('recmeta').textContent='recording · '+fmt(recEl)+recSuffix;
   // Overlapping calendar events → the label was a guess. Offer the picker.
   const live=await (await fetch('/api/live')).json();
   const pk=$('picker');
   if(live.length>1){
     pk.style.display='inline-block';
     pk.innerHTML='<option value="">wrong meeting?</option>'+live
       .filter(e=>e.slug!==s.label)
       .map(e=>`<option value="${e.slug}">${e.title}</option>`).join('');
   } else pk.style.display='none';
 }
}
function relabel(slug){if(!slug)return;
 fetch('/api/relabel',{method:'POST',body:slug}).then(()=>setTimeout(poll,800))}
async function load(){
 // Compact "next up" line (replaces the old Today timeline) — just the next
 // upcoming calendar meeting, or the one happening now.
 const t=await (await fetch('/api/today')).json();
 const nowHM=new Date().toTimeString().slice(0,5);
 // Today's agenda — every calendar meeting today, now-highlighted, past dimmed.
 // (Calendar events; distinct from the mini-cal dots = recorded meetings.)
 $('today').innerHTML=t.map(l=>{
   const m=l.match(/^(\d\d:\d\d)-(\d\d:\d\d) \| (.+?) \|/); if(!m) return '';
   const cls = nowHM>=m[1]&&nowHM<m[2] ? 'now' : nowHM>=m[2] ? 'past' : 'fut';
   return `<div class="crow ${cls}"><span class="ct">${m[1]}</span><span class="cbar"></span><span class="cname">${m[3]}</span></div>`;
 }).join('')||'<div class="crow" style="color:var(--muted)">no meetings today</div>';
 // Sidebar mini-calendar — date navigation (distinct from the main Library);
 // click a day → the same popover the big calendar uses.
 const allms=await (await fetch('/api/meetings?limit=500')).json();
 window._byDate={}; allms.forEach(m=>{(window._byDate[m.date]=window._byDate[m.date]||[]).push(m)});
 $('sidecal').innerHTML=miniCal(window._byDate);
 // Sidebar list = SEARCH RESULTS ONLY (no duplication of the main-page
 // Library). Empty search → hint; the main Library is the browser.
 if(mq){
   const ms=await (await fetch('/api/meetings?q='+encodeURIComponent(mq))).json();
   $('list').innerHTML=ms.map(m=>rowHtml(m,true)).join('')
     ||'<div class="mt" style="color:var(--muted)">no matches</div>';
 } else {
   $('list').innerHTML='<div style="color:var(--muted);font-size:12px;padding:4px 6px">Type to search — or <a href="#" onclick="showLib();return false" style="color:var(--accent)">browse all</a> in the main panel.</div>';
 }
 // To-do count badge (kept fresh even when the To-do view isn't open).
 try{
   const td=await (await fetch('/api/todos')).json();
   window._todos=td; setTodoBadge(td.count||0);
   if(!sel && mainView==='todo'){ renderTodos(td); return; }
 }catch(e){}
 if(!sel) renderLib();
}
function setTodoBadge(n){const b=$('todocount');if(!b)return;
 b.textContent=n||'';b.style.display=n?'inline-block':'none'}
async function showLib(){sel=null;detail=null;mainView='lib';syncTodoNav();try{localStorage.removeItem('stenoView')}catch(e){}await renderLib()}
async function showTodos(){/* TODO-DISABLED: feature paused — no-op so nothing can enter the todo view */}
function syncTodoNav(){const n=$('todonav');if(n)n.className=(mainView==='todo'&&!sel)?'on':''}
async function renderLib(){
 const ms=await (await fetch('/api/meetings?q='+encodeURIComponent(mq)+'&limit=500')).json();
 const cats=[...new Set(ms.map(m=>m.cat).filter(Boolean))].sort();
 const tabs=['recent','calendar','categories'].map(v=>
   `<button class="${v===libTab?'on':''}" onclick="libTab='${v}';renderLib()">${v}</button>`).join('');
 let body='';
 if(libTab==='recent'){
   const shown=libCat?ms.filter(m=>m.cat===libCat):ms;
   body=chipRow(cats)+`<div class="cardgrid">`+shown.map(cardHtml).join('')+`</div>`;
 } else if(libTab==='calendar'){
   body=bigCal(ms);
 } else {
   const groups=[...cats,''].map(c=>({c,grp:ms.filter(m=>(m.cat||'')===c)})).filter(g=>g.grp.length);
   body=groups.map(g=>`
     <div style="margin-bottom:26px">
       <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-bottom:10px">${g.c||'uncategorized'} · ${g.grp.length}</div>
       <div class="cardgrid">${g.grp.map(cardHtml).join('')}</div></div>`).join('');
 }
 $('view').innerHTML=`<h1 style="font-size:22px;margin-bottom:14px">Library</h1>
   <div class="tabs" style="margin-bottom:18px">${tabs}</div>${body}`;
}
function chipRow(cats){
 return cats.length?`<div style="display:flex;gap:5px;flex-wrap:wrap;margin-bottom:14px">${['all',...cats].map(c=>
  `<button onclick="libCat='${c==='all'?'':c}';renderLib()" style="border:1px solid ${((c==='all'&&!libCat)||c===libCat)?'var(--ink)':'var(--line)'};background:${((c==='all'&&!libCat)||c===libCat)?'var(--ink)':'var(--card)'};color:${((c==='all'&&!libCat)||c===libCat)?'var(--paper)':'var(--muted)'};border-radius:8px;padding:3px 12px;font-size:12px;cursor:pointer">${c}</button>`).join('')}</div>`:'';
}
function cardHtml(m){
 return `<div class="mcard" onclick='open_("${m.id}",${JSON.stringify(m.segs)})'>
   <div style="font-weight:650;font-size:14px;text-transform:capitalize;line-height:1.35">${m.starred?'<span style="color:var(--accent)">★</span> ':''}${m.title}<span class="dot ${m.has_note?'note':'raw'}" style="margin-left:6px"></span></div>
   <div style="font-size:11.5px;color:var(--muted);margin-top:6px">${[new Date(m.date+'T00:00').toLocaleDateString('en-GB',{day:'numeric',month:'short'}),m.time,m.n>1?m.n+' parts':''].filter(Boolean).join(' · ')}</div>
   ${m.transcribed===false?`<div style="margin-top:8px"><span style="font-size:10px;color:var(--accent);border:1px solid var(--accent);border-radius:6px;padding:1px 7px">● not transcribed</span> <button onclick='event.stopPropagation();transcribe("${m.id}",event)' style="font-size:10px;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:6px;padding:1px 8px;cursor:pointer;margin-left:4px">Transcribe</button></div>`:''}
   ${(m.cat||m.proj)?`<div style="margin-top:8px;display:flex;gap:4px;flex-wrap:wrap">${[m.cat,m.proj].filter(Boolean).map(x=>`<span style="font-size:10.5px;border:1px solid var(--line);border-radius:6px;padding:1px 7px;color:var(--muted)">${x}</span>`).join('')}</div>`:''}</div>`;
}
async function transcribe(id,ev){
 if(ev){ev.stopPropagation();ev.target.textContent='transcribing…';ev.target.disabled=true;}
 try{await fetch('/api/transcribe/'+encodeURIComponent(id),{method:'POST'});}catch(e){}
 // sweep runs in the background; the row flips to transcribed on its own once
 // the transcript lands (next library refresh).
}
async function retranscribe(lang){
 if(!sel)return;
 const L={auto:'Auto',en:'English',hi:'Hindi'}[lang]||lang;
 if(!confirm('Re-transcribe this recording as '+L+'? Runs whisper again in the background (~1-2 min); refresh to see the new transcript.'))return;
 try{await fetch('/api/transcribe/'+encodeURIComponent(sel)+'?lang='+lang,{method:'POST'});
   alert('Re-transcribing as '+L+'. Reopen the meeting in a minute to see it.');}catch(e){alert('Failed to start: '+e);}
}
function bigCal(ms){
 const byDate={};ms.forEach(m=>{(byDate[m.date]=byDate[m.date]||[]).push(m)});
 const [y,mo]=calMonth.split('-').map(Number);
 const first=new Date(y,mo-1,1), days=new Date(y,mo,0).getDate();
 const off=(first.getDay()+6)%7;
 const today=new Date().toISOString().slice(0,10);
 let h=`<div style="display:flex;align-items:center;max-width:860px;margin-bottom:12px">
   <button onclick="calNav(-1)" style="border:1px solid var(--line);background:var(--card);color:var(--muted);border-radius:8px;cursor:pointer;padding:4px 12px">◀</button>
   <span style="flex:1;text-align:center;font-size:15px;font-weight:700">${first.toLocaleDateString('en-GB',{month:'long',year:'numeric'})}</span>
   <button onclick="calNav(1)" style="border:1px solid var(--line);background:var(--card);color:var(--muted);border-radius:8px;cursor:pointer;padding:4px 12px">▶</button></div>
   <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:6px;max-width:860px">`;
 for(const d of ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'])h+=`<div style="text-align:center;font-size:11px;color:var(--muted);padding-bottom:2px">${d}</div>`;
 for(let i=0;i<off;i++)h+='<div></div>';
 for(let d=1;d<=days;d++){
   const ds=`${calMonth}-${String(d).padStart(2,'0')}`;
   const evs=byDate[ds]||[];
   h+=`<div onclick="dayPop(event,'${ds}')" style="min-height:92px;border:1px solid ${ds===today?'var(--accent)':'var(--line)'};border-radius:10px;background:var(--card);padding:6px 7px;overflow:hidden;cursor:${evs.length?'pointer':'default'}">
     <div style="font-size:11px;font-weight:${evs.length?'700':'400'};color:${ds===today?'var(--accent)':'var(--muted)'};margin-bottom:3px">${d}</div>
     ${evs.slice(0,3).map(m=>`<div onclick='event.stopPropagation();open_("${m.id}",${JSON.stringify(m.segs)})' style="font-size:10.5px;line-height:1.3;padding:2px 4px;border-radius:5px;background:var(--sel);margin-bottom:2px;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-transform:capitalize">${m.time?m.time+' ':''}${m.title}</div>`).join('')}
     ${evs.length>3?`<div style="font-size:9.5px;color:var(--accent);padding-left:4px;font-weight:600">+${evs.length-3} more</div>`:''}</div>`;
 }
 h+='</div>';
 // Day meetings render in a popover anchored to the clicked cell (dayPop),
 // not stacked below the grid — no scrolling to reach them.
 window._byDate=byDate;
 return h;
}
function rowHtml(m,withDate){
 return `<div class="mt${m.segs.includes(sel)?' sel':''}" onclick='open_("${m.id}",${JSON.stringify(m.segs)})'>
   <div class="title"><span class="mtime">${m.time||''}</span><span class="tt">${m.title}</span><span class="dot ${m.has_note?'note':'raw'}"></span></div>
   <div class="sub">${[withDate?m.date:'',m.cat,m.proj,m.transcribed===false?'not transcribed':'',m.n>1?m.n+' parts':''].filter(Boolean).join(' · ')||''}</div></div>`;
}
function calHtml(ms){
 const byDate={};ms.forEach(m=>{(byDate[m.date]=byDate[m.date]||[]).push(m)});
 const [y,mo]=calMonth.split('-').map(Number);
 const first=new Date(y,mo-1,1), days=new Date(y,mo,0).getDate();
 const off=(first.getDay()+6)%7; // Monday-first
 const today=new Date().toISOString().slice(0,10);
 let h=`<div style="display:flex;align-items:center;margin:4px 2px 8px">
   <button onclick="calNav(-1)" style="border:none;background:none;color:var(--muted);cursor:pointer;font-size:14px">◀</button>
   <span style="flex:1;text-align:center;font-size:12.5px;font-weight:600">${first.toLocaleDateString('en-GB',{month:'long',year:'numeric'})}</span>
   <button onclick="calNav(1)" style="border:none;background:none;color:var(--muted);cursor:pointer;font-size:14px">▶</button></div>
   <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:3px;margin-bottom:10px">`;
 for(const d of ['M','T','W','T','F','S','S'])h+=`<div style="text-align:center;font-size:9.5px;color:var(--muted)">${d}</div>`;
 for(let i=0;i<off;i++)h+='<div></div>';
 for(let d=1;d<=days;d++){
   const ds=`${calMonth}-${String(d).padStart(2,'0')}`;
   const n=(byDate[ds]||[]).length;
   const isSel=calDay===ds, isToday=ds===today;
   h+=`<div onclick="calDay=calDay==='${ds}'?null:'${ds}';load()" style="aspect-ratio:1;display:flex;flex-direction:column;align-items:center;justify-content:center;border-radius:8px;cursor:${n?'pointer':'default'};font-size:11px;
     background:${isSel?'var(--ink)':isToday?'var(--sel)':'transparent'};color:${isSel?'var(--paper)':n?'var(--ink)':'var(--muted)'};font-weight:${n?'600':'400'}">
     ${d}${n?`<span style="font-size:8px;line-height:3px;color:${isSel?'var(--paper)':'var(--accent)'}">${'•'.repeat(Math.min(n,4))}</span>`:'<span style="font-size:8px;line-height:3px">&nbsp;</span>'}</div>`;
 }
 h+='</div>';
 const dayMs=calDay?(byDate[calDay]||[]):[];
 if(calDay)h+=`<div style="font-size:10.5px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin:4px 4px 5px">${new Date(calDay+'T00:00').toLocaleDateString('en-GB',{weekday:'long',day:'numeric',month:'short'})}</div>`+(dayMs.map(m=>rowHtml(m,false)).join('')||'<div class="mt" style="color:var(--muted)">no meetings</div>');
 else h+='<div style="font-size:11.5px;color:var(--muted);text-align:center">click a day</div>';
 return h;
}
let sideMonth=new Date().toISOString().slice(0,7);
function miniCal(byDate){
 const [y,mo]=sideMonth.split('-').map(Number);
 const first=new Date(y,mo-1,1), days=new Date(y,mo,0).getDate();
 const off=(first.getDay()+6)%7, today=new Date().toISOString().slice(0,10);
 let h=`<div style="display:flex;align-items:center;margin-bottom:6px">
   <button onclick="sideNav(-1)" style="border:none;background:none;color:var(--muted);cursor:pointer;font-size:12px;padding:0 4px">◀</button>
   <span style="flex:1;text-align:center;font-size:11.5px;font-weight:600">${first.toLocaleDateString('en-GB',{month:'long',year:'numeric'})}</span>
   <button onclick="sideNav(1)" style="border:none;background:none;color:var(--muted);cursor:pointer;font-size:12px;padding:0 4px">▶</button></div>
   <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:2px">`;
 for(const d of ['M','T','W','T','F','S','S'])h+=`<div style="text-align:center;font-size:9px;color:var(--muted)">${d}</div>`;
 for(let i=0;i<off;i++)h+='<div></div>';
 for(let d=1;d<=days;d++){
   const ds=`${sideMonth}-${String(d).padStart(2,'0')}`, n=(byDate[ds]||[]).length;
   h+=`<div onclick="dayPop(event,'${ds}')" style="aspect-ratio:1;display:flex;flex-direction:column;align-items:center;justify-content:center;border-radius:6px;font-size:10.5px;cursor:${n?'pointer':'default'};
     background:${ds===today?'var(--sel)':'transparent'};color:${n?'var(--ink)':'var(--muted)'};font-weight:${n?'600':'400'};${ds===today?'outline:1px solid var(--accent)':''}">
     ${d}<span style="height:3px;line-height:3px;font-size:7px;color:var(--accent)">${n?'•':'&nbsp;'}</span></div>`;
 }
 return h+'</div>';
}
function sideNav(d){const [y,m]=sideMonth.split('-').map(Number);
 const nd=new Date(y,m-1+d,1);sideMonth=`${nd.getFullYear()}-${String(nd.getMonth()+1).padStart(2,'0')}`;load()}
function dayPop(ev,ds){
 const cell=ev.currentTarget, ms=(window._byDate||{})[ds]||[];
 closePop();
 if(!ms.length) return;
 const r=cell.getBoundingClientRect();
 const p=document.createElement('div'); p.id='daypop';
 const dl=new Date(ds+'T00:00').toLocaleDateString('en-GB',{weekday:'long',day:'numeric',month:'long'});
 p.innerHTML=`<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:8px">${dl} · ${ms.length} meeting${ms.length===1?'':'s'}</div>`
   +ms.map(m=>`<div class="mcard" style="margin-bottom:8px" onclick='closePop();open_("${m.id}",${JSON.stringify(m.segs)})'>
     <div style="font-weight:650;font-size:13.5px;text-transform:capitalize">${m.time?m.time+'  ':''}${m.title}<span class="dot ${m.has_note?'note':'raw'}" style="margin-left:6px"></span></div>
     ${(m.cat||m.proj)?`<div style="margin-top:5px;display:flex;gap:4px;flex-wrap:wrap">${[m.cat,m.proj].filter(Boolean).map(x=>`<span style="font-size:10px;border:1px solid var(--line);border-radius:6px;padding:1px 7px;color:var(--muted)">${x}</span>`).join('')}</div>`:''}</div>`).join('');
 Object.assign(p.style,{position:'fixed',zIndex:50,width:'320px',maxHeight:'70vh',overflowY:'auto',
   background:'var(--card)',border:'1px solid var(--line)',borderRadius:'14px',padding:'14px 16px',
   boxShadow:'0 12px 40px rgba(0,0,0,.28)'});
 document.body.appendChild(p);
 // Anchor beside the cell; flip left/up if it would overflow the viewport.
 const pw=320, ph=Math.min(p.scrollHeight,window.innerHeight*0.7);
 let x=r.right+8; if(x+pw>window.innerWidth-12) x=Math.max(12,r.left-pw-8);
 let y=r.top; if(y+ph>window.innerHeight-12) y=Math.max(12,window.innerHeight-ph-12);
 p.style.left=x+'px'; p.style.top=y+'px';
 setTimeout(()=>document.addEventListener('mousedown',popOutside),0);
}
function popOutside(e){if(!e.target.closest('#daypop'))closePop()}
function closePop(){const p=document.getElementById('daypop');if(p)p.remove();
 document.removeEventListener('mousedown',popOutside)}
function calNav(d){const [y,m]=calMonth.split('-').map(Number);
 const nd=new Date(y,m-1+d,1);calMonth=`${nd.getFullYear()}-${String(nd.getMonth()+1).padStart(2,'0')}`;calDay=null;load()}
let mq='', mqT=null, libTab='recent', libCat='', calDay=null,
    calMonth=new Date().toISOString().slice(0,7), mainView='lib', untOpen=false, doneOpen=false, todoTab='todo';
// ── To-do (My action items) view ──────────────────────────────────────────
async function renderTodos(td){
 if(!td) td=await (await fetch('/api/todos')).json();
 window._todos=td; setTodoBadge(td.count||0); syncTodoNav();
 const tabs=`<div class="tabs" style="margin:2px 0 16px">
   <button class="${todoTab==='todo'?'on':''}" onclick="todoTab='todo';renderTodos(window._todos)">To do${td.count?' · '+td.count:''}</button>
   <button class="${todoTab==='followup'?'on':''}" onclick="todoTab='followup';renderTodos(window._todos)">Follow up${td.follow_up_count?' · '+td.follow_up_count:''}</button></div>`;
 const sub=todoTab==='followup'
   ? 'Things others owe you from meetings — with a said-vs-done hint so you know who to chase.'
   : 'Your to-dos across meetings — asks to you, your commitments, actions you own or unassigned, plus 💡 AI suggestions.';
 const body=todoTab==='followup'?renderFollowUp(td):renderMine(td);
 $('view').innerHTML=`<h1 style="font-size:22px;margin-bottom:4px">My action items</h1>
   <div style="color:var(--muted);font-size:13px;margin-bottom:6px">${sub}</div>${tabs}${body}`;
}
function bucketByDue(arr){
 const today=new Date().toISOString().slice(0,10);
 const in7=new Date(Date.now()+7*864e5).toISOString().slice(0,10);
 const b={overdue:[],week:[],later:[],nodue:[]};
 arr.forEach(it=>{ if(!it.due) b.nodue.push(it);
   else if(it.due<today) b.overdue.push(it);
   else if(it.due<=in7) b.week.push(it); else b.later.push(it); });
 const srt=a=>a.sort((x,y)=>(x.due||'').localeCompare(y.due||'')||(y.ts||'').localeCompare(x.ts||''));
 Object.values(b).forEach(srt); return b;
}
function grpBlock(arr,rowFn){
 const b=bucketByDue(arr);
 const g=(cls,label,a)=>a.length?`<div class="tdgrp ${cls}">${label} · ${a.length}</div>`+a.map(rowFn).join(''):'';
 return g('over','Overdue',b.overdue)+g('','This week',b.week)+g('','Later',b.later)+g('','No due date',b.nodue);
}
function renderMine(td){
 const items=td.items||[], unt=td.untracked||[], sug=td.suggestions||[], done=td.done||[];
 let body='';
 if(sug.length) body+=`<div class="tdgrp" style="color:var(--accent)">💡 Suggested · ${sug.length} <span style="font-weight:400;text-transform:none;letter-spacing:0;color:var(--muted)">— inferred, not an explicit action</span></div>`+sug.map(suggestionRow).join('');
 body+=grpBlock(items,todoRow);
 if(!items.length && !sug.length) body=`<div class="empty">🎉 Nothing on your plate — no open action items from your meetings.${td.owner_resolved?'':'<div style="font-size:12px;margin-top:8px">(owner identity unresolved — showing unassigned items + asks only)</div>'}</div>`;
 if(unt.length) body+=`<div style="margin-top:30px;max-width:820px">
     <div onclick="untOpen=!untOpen;renderTodos(window._todos)" style="cursor:pointer;font-size:12.5px;font-weight:600;color:var(--muted);user-select:none">
       ${untOpen?'▾':'▸'} Mentioned, not tracked · ${unt.length} <span style="font-weight:400">— /ticketize candidates, not personal to-dos</span></div>
     ${untOpen?'<div style="margin-top:10px">'+unt.map(untRow).join('')+'</div>':''}</div>`;
 if(done.length) body+=`<div style="margin-top:22px;max-width:820px">
     <div onclick="doneOpen=!doneOpen;renderTodos(window._todos)" style="cursor:pointer;font-size:12.5px;font-weight:600;color:var(--good);user-select:none">
       ${doneOpen?'▾':'▸'} Completed · ${done.length} <span style="font-weight:400;color:var(--muted)">— click ↩ to un-check</span></div>
     ${doneOpen?'<div style="margin-top:10px">'+done.map(doneRow).join('')+'</div>':''}</div>`;
 return body;
}
function renderFollowUp(td){
 const fu=td.follow_up||[];
 if(!fu.length) return `<div class="empty">Nothing to follow up on — no open items others owe you.</div>`;
 return grpBlock(fu,followUpRow);
}
function kbadge(kind){
 const col=kind==='ask'?'var(--accent)':kind==='commitment'?'var(--good)':'var(--muted)';
 return `<span class="kbadge" style="color:${col}">${kind}</span>`;
}
function meetingSrc(m){
 if(!m||!m.title) return '';
 const t=esc(m.title);
 if(m.open) return `<a href="#" class="tdsrc" onclick='openFromTodo(${JSON.stringify(m.open)},${JSON.stringify(m.segs||[])});return false'>${t}</a>`;
 return `<span style="text-transform:capitalize">${t}</span>`;
}
function todoRow(it){
 const due=it.due?`<span style="color:${it.due<new Date().toISOString().slice(0,10)?'#b91c1c':'var(--muted)'}">due ${esc(it.due)}</span>`:'';
 const src=meetingSrc(it.meeting);
 return `<div class="todorow">
   <button class="tddone" title="Mark done" onclick='resolveTodo(${JSON.stringify(it.id)})'>✓</button>
   <div class="txt">${esc(it.text)}
     <div class="sub">${kbadge(it.kind)}${src?'<span>· '+src+'</span>':''}${due?'<span>· '+due+'</span>':''}</div>
   </div></div>`;
}
function untRow(it){
 const src=meetingSrc(it.meeting);
 return `<div class="todorow" style="opacity:.85">
   <button class="tddone" title="Dismiss (mark handled)" onclick='resolveTodo(${JSON.stringify(it.id)})'>✓</button>
   <div class="txt">${esc(it.text)}<div class="sub">${kbadge('untracked')}${src?'<span>· '+src+'</span>':''}</div></div></div>`;
}
function doneRow(it){
 const src=meetingSrc(it.meeting);
 return `<div class="todorow" style="opacity:.7">
   <button class="tddone" title="Un-check (reopen)" onclick='reopenTodo(${JSON.stringify(it.id)})'>↩</button>
   <div class="txt"><span style="text-decoration:line-through">${esc(it.text)}</span>
     <div class="sub">${kbadge(it.kind)}${src?'<span>· '+src+'</span>':''}</div>
   </div></div>`;
}
function suggestionRow(it){
 const src=meetingSrc(it.meeting);
 return `<div class="todorow" style="border-color:var(--recline);background:var(--recbg)">
   <button class="tddone" title="Accept / mark done" onclick='resolveTodo(${JSON.stringify(it.id)})'>✓</button>
   <div class="txt">${esc(it.text)}
     <div class="sub"><span class="kbadge" style="color:var(--accent)">suggested</span>${src?'<span>· '+src+'</span>':''}${it.rationale?'<span>· '+esc(it.rationale)+'</span>':''}</div>
   </div></div>`;
}
function followUpRow(it){
 const src=meetingSrc(it.meeting);
 const overdue=it.due&&it.due<new Date().toISOString().slice(0,10);
 const due=it.due?`<span style="color:${overdue?'#b91c1c':'var(--muted)'}">due ${esc(it.due)}</span>`:'';
 const stale=it.evidence&&it.evidence.indexOf('no activity')>=0;
 const ev=it.evidence?`<span style="color:${stale?'#b91c1c':'var(--good)'}">${esc(it.evidence)}</span>`:'';
 return `<div class="todorow">
   <button class="tddone" title="Mark followed up / handled" onclick='resolveTodo(${JSON.stringify(it.id)})'>✓</button>
   <div class="txt"><b>${esc(it.who_name||it.who||'?')}</b> — ${esc(it.text)}
     <div class="sub">${kbadge(it.kind)}${src?'<span>· '+src+'</span>':''}${due?'<span>· '+due+'</span>':''}${ev?'<span>· '+ev+'</span>':''}</div>
   </div></div>`;
}
async function resolveTodo(id){
 const x=await (await fetch('/api/todo/resolve/'+id,{method:'POST'})).json();
 if(!x.ok){alert('Could not mark it done.');return;}
 const td=await (await fetch('/api/todos')).json();
 renderTodos(td);
}
async function reopenTodo(id){
 const x=await (await fetch('/api/todo/reopen/'+id,{method:'POST'})).json();
 if(!x.ok){alert('Could not reopen it.');return;}
 const td=await (await fetch('/api/todos')).json();
 renderTodos(td);
}
// Open the source meeting from a to-do row (leaves the To-do view active so the
// back button / re-click returns here).
function openFromTodo(id,segs){open_(id,segs&&segs.length?segs:[id])}
document.addEventListener('DOMContentLoaded',()=>{});

function applyTheme(){const pref=localStorage.theme||'auto';
 const dark = pref==='dark' || (pref==='auto' && matchMedia('(prefers-color-scheme: dark)').matches);
 document.documentElement.dataset.theme = dark ? 'dark' : 'light';
 const b=$('theme'); if(b) b.textContent=pref;}
function cycleTheme(){const o=['auto','light','dark'];
 localStorage.theme=o[(o.indexOf(localStorage.theme||'auto')+1)%3]; applyTheme();}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',applyTheme);
applyTheme();
let segs=[];
async function open_(id,ss){
 sel=id; segs=ss||[id]; detail=await (await fetch('/api/meeting/'+id)).json(); tab=detail.note?'note':'transcript';
 // Remember the open meeting so Cmd-R (a full WebView reload) restores THIS view
 // instead of dropping back to the Library.
 try{localStorage.stenoView=JSON.stringify({sel,segs,tab})}catch(e){}
 syncTodoNav(); render(); load();
}
async function seg_(id){sel=id;detail=await (await fetch('/api/meeting/'+id)).json();render()}
function render(){
 if(!detail) return;
 const tabs=['note','transcript','my notes'].concat((detail.mom||detail.mom_queued)?['MoM']:[]).concat(detail.speakers?['Speakers']:[]).concat(detail.share?['Share']:[]);
 const parts=segs.length>1?`<div style="margin:-8px 0 12px;display:flex;gap:6px">${segs.map((s,i)=>`<button class="${s===sel?'on':''}" style="border:1px solid var(--line);background:${s===sel?'var(--ink)':'var(--card)'};color:${s===sel?'var(--paper)':'var(--muted)'};border-radius:8px;padding:3px 10px;font-size:12px;cursor:pointer" onclick="seg_('${s}')">part ${i+1}</button>`).join('')}</div>`:'';
 const linkchips=(detail.links||[]).map(u=>`<a href="${u.startsWith('http')?u:'#'}" target="_blank" style="font-size:11.5px;background:var(--card);border:1px solid var(--line);border-radius:7px;padding:2px 8px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-decoration:none">${u}</a>`).join('');
 const linkbar=`<div style="margin:0 0 14px;display:flex;flex-wrap:wrap;gap:6px;align-items:center">${linkchips}
   <input id="mlink" placeholder="+ attach context link" style="border:1px dashed var(--line);border-radius:8px;padding:3px 10px;font-size:12px;background:transparent;outline:none;width:180px"
     onkeydown="if(event.key==='Enter'&&this.value){fetch('/api/links/'+sel,{method:'POST',body:this.value}).then(()=>seg_(sel));}"></div>`;
 const TAXO=['standup','1-1','prd-handover','design-review','incident-review','planning','interview','vendor','townhall','other'];
 $('view').innerHTML=`<button class="back" onclick="showLib()">← library</button>
 <div style="display:flex;align-items:center;gap:8px"><h1 style="margin:0">${sel.slice(11).replace(/-\d+$/,'').replaceAll('-',' ')}</h1>
   <button onclick="toggleStar()" title="Star — protected, never auto-deleted" style="border:none;background:none;font-size:22px;line-height:1;cursor:pointer;color:${detail.starred?'var(--accent)':'var(--muted)'}">${detail.starred?'★':'☆'}</button></div>
 <div class="date" style="display:flex;align-items:center;gap:10px">${sel.slice(0,10)}
   <select onchange="setCat(this.value)" style="border:1px solid var(--line);background:var(--card);color:var(--muted);border-radius:7px;padding:2px 6px;font-size:11.5px">
     <option value="">${detail.cat?detail.cat+' ▾':'set category…'}</option>
     ${TAXO.filter(c=>c!==detail.cat).map(c=>`<option value="${c}">${c}</option>`).join('')}
   </select>
   <input id="pplin" placeholder="participants…" value="${(detail.participants||[]).join(', ')}"
     onchange="fetch('/api/participants/'+sel,{method:'POST',body:this.value}).then(()=>seg_(sel))"
     style="border:1px solid var(--line);background:var(--card);color:var(--muted);border-radius:7px;padding:2px 8px;font-size:11.5px;min-width:180px;outline:none"></div>
 ${parts}
 ${linkbar}
 <div class="tabs">${tabs.map(t=>`<button class="${t===tab?'on':''}" onclick="tab='${t}';render()">${t}</button>`).join('')}
   <span style="margin-left:auto;display:flex;gap:6px">
   ${(!detail.mom&&!detail.mom_queued)?`<button style="border:1px solid var(--line);background:var(--card);border-radius:9px;padding:5px 14px;font-size:13px;cursor:pointer;color:var(--muted)" onclick="mom()">MoM</button>`:''}
   ${(detail.note||detail.mom)?`<button style="border:1px solid var(--line);background:var(--card);border-radius:9px;padding:5px 14px;font-size:13px;cursor:pointer;color:var(--muted)" onclick="share()">Share (redact)</button>`:''}
   ${detail.note?`<button style="border:1px solid var(--line);background:var(--card);border-radius:9px;padding:5px 14px;font-size:13px;cursor:pointer;color:var(--accent)" onclick="regen()">↻ regenerate</button>`:''}
   <button style="border:1px solid var(--line);background:var(--card);border-radius:9px;padding:5px 14px;font-size:13px;cursor:pointer;color:#b91c1c" onclick="del()">delete</button></span></div>
 <div id="content">${
   tab==='note' ? (detail.note?`<div class="md">${md(detail.note)}</div>`
     : detail.queued?`<div style="background:var(--recbg);border:1px solid var(--recline);border-radius:12px;padding:10px 16px;margin-bottom:14px;font-size:13px">↻ Regeneration queued — rebuilds within ~30 min with your added notes & links. Below is the previous version. Want it now? Ask Claude to run /meeting-notes.</div><div class="md" style="opacity:.65">${md(detail.note_prev)}</div>`
     :'<div class="empty">Note not generated yet — the routine picks it up within ~30 min.</div>')
   : tab==='MoM' ? (detail.mom?`<div class="md">${md(detail.mom)}</div>`
     :'<div class="empty">MoM queued — generated by the routine within ~15 min.</div>')
   : tab==='Share' ? `<div style="background:var(--recbg);border:1px solid var(--recline);border-radius:12px;padding:10px 16px;margin-bottom:14px;font-size:13px;display:flex;flex-wrap:wrap;gap:10px;align-items:center">
       <span style="flex:1;min-width:220px">Redacted from the ${detail.share_src||'note'} for sharing — <b>review before sending</b>. Nothing is sent automatically. Masked: ${detail.share_masked||'PII'}.</span>
       <button onclick="share('names')" style="border:1px solid var(--line);background:var(--card);border-radius:8px;padding:4px 12px;font-size:12px;cursor:pointer;color:var(--muted)">also mask names</button>
       <button onclick="copyShare()" style="border:1px solid var(--line);background:var(--card);border-radius:8px;padding:4px 12px;font-size:12px;cursor:pointer;color:var(--accent)">copy</button></div>
     <div class="md">${md(detail.share)}</div>`
   : tab==='Speakers' ? (function(){
       const sp=detail.speakers;
       const opts=(selh)=>['<option value="">— unassigned —</option>'].concat(
         sp.roster.map(r=>`<option value="${r.handle}" ${r.handle===selh?'selected':''}>${r.name}</option>`)).join('');
       return `<div style="font-size:12.5px;color:var(--muted);margin-bottom:12px">Assign each detected voice to a person. Your choice is saved as ground truth for the note AND teaches Steno to recognise that voice in future meetings. 🔊 = auto-matched by voice.</div>`+
         sp.list.map(s=>`<div style="display:flex;align-items:center;gap:12px;padding:9px 12px;border:1px solid var(--line);border-radius:10px;margin-bottom:8px">
           <b style="min-width:76px">${s.display}</b>
           <span style="flex:1;color:var(--muted);font-size:12.5px">${s.name?('→ <b style="color:var(--accent)">'+s.name+'</b>'):(s.suggestion?('🔊 sounds like <b>'+s.suggestion+'</b> ('+s.score+') <button onclick="setSpeaker(\''+s.cluster+'\',\''+s.suggestion_handle+'\')" style="border:1px solid var(--recline);background:var(--card);color:var(--accent);border-radius:7px;padding:2px 9px;font-size:11.5px;cursor:pointer;margin-left:4px">confirm</button>'):'unassigned')}</span>
           <select onchange="setSpeaker('${s.cluster}',this.value)" style="border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:5px 10px;font-size:12.5px">${opts(s.handle||'')}</select>
         </div>`).join('');
     })()
   : tab==='transcript' ? `<div style="display:flex;gap:7px;align-items:center;margin-bottom:11px;font-size:12px;color:var(--muted);flex-wrap:wrap">
       <span>Re-transcribe:</span>
       <button onclick="retranscribe('auto')" style="border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:7px;padding:2px 10px;font-size:11.5px;cursor:pointer">Auto</button>
       <button onclick="retranscribe('en')" style="border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:7px;padding:2px 10px;font-size:11.5px;cursor:pointer">English</button>
       <button onclick="retranscribe('hi')" style="border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:7px;padding:2px 10px;font-size:11.5px;cursor:pointer">Hindi</button>
       <span style="opacity:.8">— Hinglish comes out as gibberish on Auto; pick Hindi.</span>
     </div>
     <pre>${mapTranscript(detail.transcript).replace(/</g,'&lt;')}</pre>`
   : `<textarea id="mpad" style="width:100%;min-height:320px;border:1px solid var(--line);border-radius:12px;background:var(--card);padding:14px 16px;font:14px/1.6 ui-monospace,Menlo,monospace;resize:vertical;outline:none" placeholder="Add your own context — what mattered, corrections, decisions the transcript garbled. Autosaves; used on the next (re)generation.">${detail.scratchpad.replace(/</g,'&lt;')}</textarea>
      <div style="font-size:12px;color:var(--muted);margin-top:6px">autosaves · attach links above · hit ↻ regenerate when ready</div>`}</div>`;
 const mp=document.getElementById('mpad');
 if(mp){let t=null;mp.addEventListener('input',()=>{clearTimeout(t);
   t=setTimeout(()=>{fetch('/api/scratch/'+sel,{method:'POST',body:mp.value});detail.scratchpad=mp.value},800)})}
}
function regen(){
 fetch('/api/regen/'+sel,{method:'POST'}).then(()=>seg_(sel));
}
// Share (redacted): deterministic PII mask, generated instantly server-side.
// Owner reviews the result and sends it by hand — nothing leaves the machine here.
function share(mode){
 fetch('/api/share/'+sel,{method:'POST',body:mode==='names'?'names':''})
  .then(r=>r.json()).then(j=>{ if(!j.ok){alert(j.error||'no note or MoM to share yet');return;}
    detail.share=j.share; detail.share_masked=j.masked; detail.share_src=j.source; tab='Share'; render();});
}
function copyShare(){ if(navigator.clipboard) navigator.clipboard.writeText(detail.share||''); }
// Assign/correct a diarized speaker → a person. Saves the mapping + enrolls the
// voiceprint so future meetings auto-recognise this voice.
function setSpeaker(cluster,handle){
 fetch('/api/speakers/'+sel,{method:'POST',body:JSON.stringify({cluster:cluster,handle:handle})}).then(()=>seg_(sel));
}
// Display confirmed speaker names in the raw transcript (Speaker N → name).
function mapTranscript(t){
 if(!detail.speakers) return t;
 const m={}; detail.speakers.list.forEach(s=>{ if(s.name) m[s.display]=s.name; });
 return t.replace(/\bSpeaker \d+\b/g, x=> m[x]||x);
}
function setCat(c){if(!c)return;
 fetch('/api/cat/'+sel,{method:'POST',body:c}).then(()=>{seg_(sel);load()})}
// Star = pin: protected from the audio-retention auto-delete.
function toggleStar(){fetch('/api/star/'+sel,{method:'POST'}).then(()=>{seg_(sel);load()})}
// Custom confirm — WKWebView (Steno.app) silently ignores native confirm().
function confirmModal(title, body){
 return new Promise(resolve=>{
   const ov=document.createElement('div');
   ov.style.cssText='position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center';
   ov.innerHTML=`<div style="background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px 24px;max-width:400px;box-shadow:0 16px 50px rgba(0,0,0,.35)">
     <div style="font-size:15px;font-weight:700;margin-bottom:8px">${title}</div>
     <div style="font-size:13px;color:var(--muted);line-height:1.5;margin-bottom:18px">${body}</div>
     <div style="display:flex;gap:8px;justify-content:flex-end">
       <button id="_cx" style="border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:9px;padding:7px 16px;font-size:13px;cursor:pointer">Cancel</button>
       <button id="_ok" style="border:none;background:#b91c1c;color:#fff;border-radius:9px;padding:7px 16px;font-size:13px;cursor:pointer;font-weight:600">Delete</button>
     </div></div>`;
   document.body.appendChild(ov);
   const done=v=>{ov.remove();resolve(v)};
   ov.querySelector('#_cx').onclick=()=>done(false);
   ov.querySelector('#_ok').onclick=()=>done(true);
   ov.onclick=e=>{if(e.target===ov)done(false)};
 });
}
async function del(){
 const title=sel.slice(11).replace(/-\d+$/,'').replaceAll('-',' ');
 const yes=await confirmModal('Delete "'+title+'"?',
   'Permanently removes the recording, transcript, notes and all its parts. This cannot be undone.');
 if(!yes) return;
 const x=await (await fetch('/api/delete',{method:'POST',body:(segs||[sel]).join(',')})).json();
 if(x.ok){sel=null;detail=null;showLib();load()}
 else confirmModal('Delete failed','Something went wrong — the meeting was not removed.');
}
function mom(){
 fetch('/api/mom/'+sel,{method:'POST'}).then(()=>{tab='MoM';seg_(sel)});
}
function toggleRec(){fetch('/api/toggle',{method:'POST'}).then(()=>setTimeout(()=>{poll();load()},1500))}
function startRec(){fetch('/api/start',{method:'POST',body:$('startlbl').value})
 .then(()=>{$('startlbl').value='';setTimeout(poll,2500)})}
// Record the live calendar meeting from the nudge banner — empty body so
// meet-record adopts the event's own name (not "in-person").
function nudgeRec(){fetch('/api/start',{method:'POST',body:''}).then(()=>setTimeout(poll,2500))}
// Arm the upcoming meeting from the pre-call banner — start recording now,
// labeled with the meeting title (so an early arm still names the note right).
function prenudgeRec(){fetch('/api/start',{method:'POST',body:$('prenudgetitle').textContent}).then(()=>setTimeout(poll,2500))}
$('pad').addEventListener('input',()=>{clearTimeout(padTimer);
 padTimer=setTimeout(()=>fetch('/api/scratchpad',{method:'POST',body:$('pad').value}),800)});
$('linkin').addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.value){
 fetch('/api/livelinks',{method:'POST',body:e.target.value}).then(()=>{e.target.value='';poll()})}});
$('msearch').addEventListener('input',e=>{clearTimeout(mqT);
 mqT=setTimeout(()=>{mq=e.target.value.trim();load()},350)});
// Cmd+[ (and Esc) → back to the Library from a meeting / close a popover.
document.addEventListener('keydown',e=>{
 if((e.metaKey&&e.key==='[')||e.key==='Escape'){
   if(document.getElementById('daypop')){closePop();return}
   if(sel){e.preventDefault();showLib()}
 }
});
(async()=>{await poll();
 // Restore the last-open meeting on reload (Cmd-R) so the current page refreshes
 // in place rather than resetting to the Library. Falls back to Library.
 let _sv=null; try{_sv=JSON.parse(localStorage.stenoView||'null')}catch(e){}
 if(_sv&&_sv.sel){ await open_(_sv.sel,_sv.segs); if(_sv.tab){tab=_sv.tab;render()} }
 else { await load(); }
 const s=await (await fetch('/api/scratchpad')).json(); $('pad').value=s.text||'';
 setInterval(poll,5000); setInterval(load,30000);
 setInterval(()=>{if(recOn){recEl++;$('recmeta').textContent='recording · '+fmt(recEl)+recSuffix}},1000)})();
</script></body></html>"""


# ---------------------------------------------------------------------------
# EXPERIMENTAL /copilot page (P6) — deliberately a SEPARATE page so the main
# UI stays untouched until this is tested and approved. Delete this block +
# its handlers to fully remove the feature.
# ---------------------------------------------------------------------------
COPILOT_PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>Steno · copilot</title>
<style>
:root{--paper:#f7f7f7;--ink:#1a1a1a;--muted:#8b8b8b;--line:#e6e6e6;--card:#fff;--accent:#c2410c;--sel:#eee}
html[data-theme=dark]{--paper:#161616;--ink:#ececec;--muted:#9a9a9a;--line:#2c2c2c;--card:#1e1e1e;--accent:#f97316;--sel:#2a2a2a}
*{box-sizing:border-box;margin:0}
body{font:14px/1.5 -apple-system,sans-serif;background:var(--paper);color:var(--ink);height:100vh;display:flex;flex-direction:column;padding:18px 22px}
h1{font-size:16px;font-weight:700;display:flex;align-items:center;gap:10px;margin-bottom:14px}
h1 .beta{font-size:10px;color:var(--accent);border:1px solid var(--accent);border-radius:5px;padding:1px 6px}
#arm{margin-left:auto;border:1px solid var(--line);border-radius:9px;padding:6px 16px;font-size:13px;cursor:pointer;font-weight:600}
#cols{flex:1;display:flex;gap:14px;min-height:0}
.pane{flex:1;display:flex;flex-direction:column;min-width:0}
.pane h2{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px}
.body{flex:1;overflow-y:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;white-space:pre-wrap;font:12.5px/1.65 ui-monospace,Menlo,monospace}
#sug{font:13px/1.55 -apple-system,sans-serif}
#sug h3{font-size:13px;margin:10px 0 4px;color:var(--accent)}
#status{font-size:12px;color:var(--muted);margin-bottom:10px}
a{color:var(--accent)}
</style></head><body>
<h1>🧠 Meeting Copilot <span class="beta">experimental</span>
  <button id="arm" onclick="toggleArm()">…</button></h1>
<div id="status">…</div>
<div id="cols">
  <div class="pane"><h2>live transcript (~20s behind)</h2><div class="body" id="live"></div></div>
  <div class="pane"><h2>suggestions</h2><div class="body" id="sug"></div></div>
</div>
<script>
const $=id=>document.getElementById(id);
function md(t){return t.replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^- (.*)$/gm,'<div>• $1</div>')
 .replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/\[(.+?)\]\((.+?)\)/g,'<a href="$2" target="_blank">$1</a>')}
async function poll(){
 const s=await (await fetch('/api/copilot')).json();
 $('arm').textContent = s.armed ? '⏻ armed — click to disarm' : 'arm copilot';
 $('arm').style.background = s.armed ? 'var(--accent)' : 'var(--card)';
 $('arm').style.color = s.armed ? '#fff' : 'var(--ink)';
 $('status').innerHTML = (s.recording?'● recording live':'no active recording')
   + (s.armed?' · armed':' · <b>arm before your meeting</b>')
   + ' · analyst: ' + (s.analyst?'<b style="color:var(--accent)">watching</b>'
       :'<b>not attached</b> — weekday sessions join at :25/:55; for ad-hoc/weekend runs ask Claude to latch');
 const lv=$('live'); const atBottom = lv.scrollTop+lv.clientHeight >= lv.scrollHeight-30;
 lv.textContent = s.live || '(live transcript appears here once recording + copilot are active)';
 if(atBottom) lv.scrollTop = lv.scrollHeight;
 $('sug').innerHTML = s.suggestions ? md(s.suggestions) : '(grounded talking points appear here as questions come up)';
}
function toggleArm(){fetch('/api/copilot/arm',{method:'POST'}).then(poll)}
const pref=localStorage.theme||'auto';
document.documentElement.dataset.theme=(pref==='dark'||(pref==='auto'&&matchMedia('(prefers-color-scheme: dark)').matches))?'dark':'light';
poll(); setInterval(poll,3000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/copilot":
            body = COPILOT_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/api/copilot":
            def tail(f, n=6000):
                try:
                    return (CAP / f).read_text(errors="replace")[-n:]
                except Exception:
                    return ""
            import time as _t
            hb = CAP / "copilot.session"
            analyst = hb.exists() and (_t.time() - hb.stat().st_mtime) < 90
            live_raw = tail("live_transcript.txt")
            # two async transcribers (Me ~3s, Them ~5s) append out of spoken
            # order — sort timestamped lines, keep headers in place.
            lines = live_raw.splitlines()
            import re as _re
            stamped = [l for l in lines if _re.match(r"^\[\d\d:\d\d\]", l)]
            other = [l for l in lines if not _re.match(r"^\[\d\d:\d\d\]", l)]
            stamped.sort(key=lambda l: l[1:6])
            self._json({
                "armed": (CAP / "copilot.on").exists(),
                "recording": rec_status()["recording"],
                "analyst": analyst,
                "live": "\n".join(other[:1] + stamped + other[1:]),
                "suggestions": tail("copilot_suggestions.md", 12000),
            })
        elif p == "/api/status":
            self._json(rec_status())
        elif p == "/api/meetings":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            q = (qs.get("q") or [""])[0]
            try:
                limit = min(int((qs.get("limit") or ["0"])[0]), 500)
            except ValueError:
                limit = 0
            self._json(meetings(q, limit or None))
        elif p.startswith("/api/meeting/"):
            self._json(meeting_detail(p.rsplit("/", 1)[1]))
        elif p == "/api/today":
            self._json(today())
        elif p == "/api/signals":
            self._json(signals())
        elif p == "/api/todos":
            self._json(todos())
        elif p == "/api/live":
            self._json(live_events())
        elif p == "/api/scratchpad":
            f = CAP / "live.notes.md"
            self._json({"text": f.read_text(errors="replace") if f.exists() else ""})
        elif p == "/api/livelinks":
            f = CAP / "live.links"
            self._json([l.strip() for l in f.read_text().splitlines() if l.strip()] if f.exists() else [])
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode(errors="replace") if n else ""
        if p == "/api/scratchpad":
            CAP.mkdir(parents=True, exist_ok=True)
            (CAP / "live.notes.md").write_text(body)
            self._json({"ok": True})
        elif p == "/api/start":
            # Manual start for IN-PERSON meetings (no call app → the daemon
            # can't detect them). Manual recordings are never auto-stopped —
            # the owner ends them with the banner's Stop button.
            # A BLANK name must NOT default to "in-person": passing any label
            # makes meet-record skip its calendar auto-labeling, so an on-calendar
            # in-person 1-1 ended up mislabeled. Blank → no label arg →
            # meet-record adopts the live calendar event's slug.
            label = re.sub(r"[^a-zA-Z0-9 _-]", "", body)[:60].strip()
            cmd = [str(REPO / "bin" / "meet-record"), "start"]
            if label:
                cmd.append(label)
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._json({"ok": True})
        elif p == "/api/toggle":
            subprocess.Popen([str(REPO / "bin" / "meet-record"), "toggle"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._json({"ok": True})
        elif p == "/api/livelinks":
            u = _valid_link(body)
            if u:
                CAP.mkdir(parents=True, exist_ok=True)
                with open(CAP / "live.links", "a") as f:
                    f.write(u + "\n")
            self._json({"ok": bool(u)})
        elif p.startswith("/api/todo/resolve/"):
            # ✓ Done — routes through signals.py resolve (idempotent; the
            # content-hash id means a resolved item never resurrects on re-add).
            self._json(todo_resolve(p.rsplit("/", 1)[1]))
        elif p.startswith("/api/todo/reopen/"):
            # Un-check from the Completed view — flip a done item back to open.
            self._json(todo_reopen(p.rsplit("/", 1)[1]))
        elif p.startswith("/api/links/"):
            mid = re.sub(r"[^a-zA-Z0-9_-]", "", p.rsplit("/", 1)[1])
            u = _valid_link(body)
            if u and mid:
                d = ARCHIVE / mid[:7]
                d.mkdir(parents=True, exist_ok=True)
                with open(d / f"{mid}.links", "a") as f:
                    f.write(u + "\n")
            self._json({"ok": bool(u and mid)})
        elif p.startswith("/api/scratch/"):
            # Post-hoc "my notes" edit on an archived meeting — feeds the
            # next note generation exactly like the live scratchpad.
            mid = re.sub(r"[^a-zA-Z0-9_-]", "", p.rsplit("/", 1)[1])
            if mid:
                d = ARCHIVE / mid[:7]
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{mid}.notes.md").write_text(body)
            self._json({"ok": bool(mid)})
        elif p.startswith("/api/regen/"):
            # Regenerate = archive the current note; the meeting-notes-auto
            # routine sees a note-less meeting and rebuilds it (≤30 min)
            # with whatever links/scratchpad exist by then. Old version kept
            # as .md.prev. (Instant path: ask Claude to run /meeting-notes.)
            mid = re.sub(r"[^a-zA-Z0-9_-]", "", p.rsplit("/", 1)[1])
            note = NOTES_DIR / f"{mid}.md"
            if mid and note.exists():
                import os
                os.replace(note, NOTES_DIR / f"{mid}.md.prev")
            self._json({"ok": True})
        elif p.startswith("/api/cat/"):
            # Manual category override — sidecar beats the AI classification
            # and survives note regeneration (the skill honors it).
            mid = re.sub(r"[^a-zA-Z0-9_-]", "", p.rsplit("/", 1)[1])
            cat = re.sub(r"[^a-z0-9-]", "", body.lower())[:30]
            if mid and cat:
                NOTES_DIR.mkdir(parents=True, exist_ok=True)
                (NOTES_DIR / f"{mid}.cat").write_text(cat + "\n")
                note = NOTES_DIR / f"{mid}.md"
                if note.exists():
                    t = note.read_text(errors="replace")
                    if re.search(r"<!--\s*category:", t):
                        t = re.sub(r"<!--\s*category:\s*[\w-]+\s*-->", f"<!-- category: {cat} -->", t, count=1)
                    else:
                        lines = t.splitlines()
                        lines.insert(1, f"<!-- category: {cat} -->")
                        t = "\n".join(lines)
                    note.write_text(t)
            self._json({"ok": bool(mid and cat)})
        elif p.startswith("/api/participants/"):
            # Manual participant tags — sidecar; skill treats as ground-truth
            # attendees (helps huddle naming + Them-attribution).
            mid = re.sub(r"[^a-zA-Z0-9_-]", "", p.rsplit("/", 1)[1])
            names = [re.sub(r"[^\w .-]", "", n).strip() for n in body.split(",")]
            names = [n for n in names if n][:15]
            if mid:
                d = ARCHIVE / mid[:7]
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{mid}.people").write_text("\n".join(names) + ("\n" if names else ""))
            self._json({"ok": bool(mid), "participants": names})
        elif p == "/api/copilot/arm":
            f = CAP / "copilot.on"
            if f.exists():
                f.unlink(missing_ok=True)
            else:
                CAP.mkdir(parents=True, exist_ok=True)
                f.touch()
                # fresh working files per arm
                (CAP / "copilot_suggestions.md").write_text("")
                # waiter keeps the LIVE TRANSCRIPT flowing for every recording
                # while armed — suggestions need a session, the transcript doesn't.
                subprocess.Popen(["/bin/bash", str(REPO / "bin" / "copilot_waiter.sh")],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._json({"armed": f.exists()})
        elif p == "/api/delete":
            # Delete a meeting (all its segment stems). Body = comma-sep stems.
            # Routes through delete_meeting.py (shared delete_events helper).
            stems = [re.sub(r"[^a-zA-Z0-9_-]", "", s) for s in body.split(",") if s.strip()]
            ok = False
            if stems:
                r = subprocess.run([_venv_py(), str(WC / "derive" / "meetings" / "delete_meeting.py"), *stems],
                                   capture_output=True, text=True, timeout=60)
                ok = r.returncode == 0
            self._json({"ok": ok})
        elif p.startswith("/api/mom/"):
            # One-click MoM: marker file → the notes routine generates a
            # formal shareable Minutes of Meeting on its next pass (≤15 min).
            mid = re.sub(r"[^a-zA-Z0-9_-]", "", p.rsplit("/", 1)[1])
            if mid:
                NOTES_DIR.mkdir(parents=True, exist_ok=True)
                (NOTES_DIR / f"{mid}.mom.request").touch()
            self._json({"ok": bool(mid)})
        elif p.startswith("/api/speakers/"):
            # Assign (or correct) a diarized speaker → a person. Persists the
            # mapping in <mid>.speakers.json AND enrolls that speaker's voiceprint
            # into the local gallery so future meetings auto-recognise the voice.
            # body = {"cluster":"SPEAKER_00","handle":"alex"}  (handle "" clears).
            mid = re.sub(r"[^a-zA-Z0-9_-]", "", p.rsplit("/", 1)[1])
            month = mid[:7]
            try:
                req = json.loads(body or "{}")
            except Exception:
                req = {}
            cluster = str(req.get("cluster", ""))
            handle = str(req.get("handle", "")).strip()
            spk_f = ARCHIVE / month / f"{mid}.speakers.json"
            if not (cluster and spk_f.exists()):
                self._json({"ok": False, "error": "no such speaker map"})
                return
            data = json.loads(spk_f.read_text(errors="replace"))
            if cluster not in data:
                self._json({"ok": False, "error": "unknown cluster"})
                return
            if handle:
                data[cluster]["handle"] = handle
                data[cluster]["name"] = _name_for(handle, _roster())
            else:  # clear assignment
                data[cluster]["handle"] = None
                data[cluster]["name"] = None
            spk_f.write_text(json.dumps(data, indent=2), encoding="utf-8")
            # Enroll the voiceprint (best-effort; needs the diarize venv + diar.json).
            diar_f = ARCHIVE / month / f"{mid}.diar.json"
            if handle and DIAR_PY.exists() and diar_f.exists():
                subprocess.Popen(
                    [str(DIAR_PY), str(WC / "derive" / "meetings" / "voice_gallery.py"),
                     "enroll", str(diar_f), cluster, handle],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            self._json({"ok": True})
        elif p.startswith("/api/share/"):
            # Instant redacted export for sharing. Deterministic (no model, no
            # cloud), so it runs synchronously here — masks PII in the MoM
            # (preferred) or the note, writes <mid>.share.md, and returns it for
            # the OWNER to review. Nothing is sent anywhere; sharing is a manual,
            # permission-required step the owner does by hand. body "names" also
            # masks team names (default: names kept).
            mid = re.sub(r"[^a-zA-Z0-9_-]", "", p.rsplit("/", 1)[1])
            src = None
            if mid:
                mom = NOTES_DIR / f"{mid}.mom.md"
                note = NOTES_DIR / f"{mid}.md"
                src = mom if mom.exists() else (note if note.exists() else None)
            if not src:
                self._json({"ok": False, "error": "no note or MoM to share yet"})
                return
            sys.path.insert(0, str(WC))
            from derive.meetings.redact import redact_text, summarize
            redacted, report = redact_text(
                src.read_text(errors="replace"), mask_names=(body.strip() == "names")
            )
            (NOTES_DIR / f"{mid}.share.md").write_text(redacted, encoding="utf-8")
            self._json({
                "ok": True,
                "share": redacted,
                "masked": summarize(report),
                "source": "MoM" if src.name.endswith(".mom.md") else "note",
            })
        elif p == "/api/relabel":
            slug = re.sub(r"[^a-z0-9-]", "", body)[:60]
            if slug:
                subprocess.run([str(REPO / "bin" / "meet-record"), "relabel", slug],
                               capture_output=True, timeout=10)
            self._json({"ok": bool(slug)})
        elif p.startswith("/api/transcribe/"):
            # On-demand transcription for a raw recording (id = <date>-<stem>).
            # Locate its audio (+ any dual-stream wavs) across inbox/hold/archive,
            # move it into the inbox, and kick the sweep detached. Whisper runs
            # ONLY on this click — no auto-drain. Processes whatever is queued
            # in the inbox at that moment.
            mid = re.sub(r"[^a-zA-Z0-9_-]", "", p.rsplit("/", 1)[1])
            stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", mid)
            ok = False
            if stem:
                import shutil
                src = None
                for d in (INBOX, HOLD, *sorted(ARCHIVE.glob("*"))):
                    if (d / f"{stem}.m4a").exists():
                        src = d
                        break
                if src and src != INBOX:
                    INBOX.mkdir(parents=True, exist_ok=True)
                    for f in src.glob(f"{stem}.*"):
                        shutil.move(str(f), str(INBOX / f.name))
                if src:
                    import os as _os
                    from urllib.parse import parse_qs, urlparse
                    # Optional ?lang= to force whisper's language. Hinglish can't be
                    # auto-detected (whisper decodes Hindi as confident-but-garbled
                    # English), so the transcript tab offers Auto/English/Hindi and
                    # passes the pick through transcripts_process.sh → transcribe.sh.
                    lang = (parse_qs(urlparse(self.path).query).get("lang", ["auto"])[0] or "auto").lower()
                    if lang not in ("auto", "hi", "en"):
                        lang = "auto"
                    env = {**_os.environ, "FORCE_TRANSCRIBE": "1",  # bypass the pause toggle
                           "TRANSCRIBE_LANG": lang}
                    subprocess.Popen(["/bin/bash", str(REPO / "bin" / "transcripts_process.sh")],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     cwd=str(WC), env=env)
                    _CACHE.pop("meetings", None)
                    ok = True
            self._json({"ok": ok})
        elif p.startswith("/api/star/"):
            # Star = pin: a <mid>.star sidecar marks the meeting protected. The
            # audio-retention prune skips any meeting with this marker (never
            # auto-deleted). Toggle on/off.
            mid = re.sub(r"[^a-zA-Z0-9_-]", "", p.rsplit("/", 1)[1])
            starred = False
            if mid:
                NOTES_DIR.mkdir(parents=True, exist_ok=True)
                star = NOTES_DIR / f"{mid}.star"
                if star.exists():
                    star.unlink(missing_ok=True)
                else:
                    star.touch()
                    starred = True
                _CACHE.pop("meetings", None)
            self._json({"ok": bool(mid), "starred": starred})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"meet UI → http://127.0.0.1:{PORT}")
    srv.serve_forever()

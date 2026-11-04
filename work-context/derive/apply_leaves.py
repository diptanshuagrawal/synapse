#!/usr/bin/env python3
"""apply_leaves.py — validate chat-emitted leaves verdicts and write to DB.

Mirrors derive/apply_verdicts.py shape:
  1. Read state/verdicts.leaves.json (written by /leaves chat skill).
  2. Cross-check event_id against state/pending_leaves.json.
  3. Validate each verdict (confidence, date format, actor on team).
  4. UPSERT into team_leaves (one row per leaves[] entry).
  5. INSERT into team_leaves_processed regardless of is_leave value
     (so rejected false positives don't re-emerge from the dump).
  6. Archive verdicts file → verdicts.leaves.<ts>.json.

Verdicts with confidence < 0.7 are dropped from team_leaves but their
event_id is NOT marked processed (so the next dump re-presents them).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import get_db  # noqa: E402

STATE_DIR = _REPO_ROOT / "state"
PENDING_JSON = STATE_DIR / "pending_leaves.json"
VERDICTS_JSON = STATE_DIR / "verdicts.leaves.json"

REASON_ENUM = {"wfh", "vacation", "sick", "holiday", "ooo", "travel", "other"}
CONFIDENCE_MIN = 0.7
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _months_spanned(date_start: str | None, date_end: str | None) -> set[tuple[int, int]]:
    """(year, month) pairs a leave row touches. Open-ended rows count one month."""
    if not date_start:
        return set()
    try:
        ds = date.fromisoformat(date_start)
        de = date.fromisoformat(date_end) if date_end else ds
    except ValueError:
        return set()
    if de < ds:
        ds, de = de, ds
    out, y, m = set(), ds.year, ds.month
    while (y, m) <= (de.year, de.month):
        out.add((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _invalidate_capacity_caches(months: set[tuple[int, int]]) -> None:
    """Drop the derived capacity caches that a leave change invalidates.

    sprint_server serves derived/{capacity,monthly,month-YYYY-MM}.json straight
    from disk and only rebuilds on ?fresh=1 — there is no TTL and nothing watches
    team_leaves. So a leave written here stayed invisible in Synapse until someone
    happened to force a refresh (observed 2026-09-24: November's cache was built
    23 Sep, and six November leave rows landing on 24 Sep left it reporting 165
    net days / 88% instead of 147 / 78%).

    Deleting beats rewriting: the server regenerates on next request, so we do not
    duplicate capacity_engine's OpsGenie probes here or race a running server.

    capacity.json (current sprint) and monthly.json (rolling 3-month window) are
    dropped unconditionally — both are relative to today, so we cannot tell from a
    row's dates alone whether it falls inside them.
    """
    derived = _REPO_ROOT / "derived"
    targets = [derived / "capacity.json", derived / "monthly.json"]
    targets += [derived / f"month-{y:04d}-{m:02d}.json" for y, m in sorted(months)]
    dropped = []
    for path in targets:
        try:
            path.unlink()
            dropped.append(path.name)
        except FileNotFoundError:
            continue
        except OSError as e:
            print(f"[cache][WARN] could not drop {path.name}: {e}")
    if dropped:
        print(f"[cache] invalidated {len(dropped)}: {', '.join(dropped)}")
    else:
        print("[cache] nothing to invalidate (no built caches on disk)")


def _thread_root(event_id: str) -> str | None:
    """Thread root of a reply event (slack:<chan>:<root>:<ts>), else None."""
    parts = event_id.split(":")
    if len(parts) == 4 and parts[0] == "slack":
        return f"slack:{parts[1]}:{parts[2]}"
    return None


def _supersede_older_plans(conn, ev_id: str, mentioned_at: str,
                           leaves: list[dict]) -> list[tuple]:
    """Drop an actor's OLDER rows from the same leave-plan thread that overlap
    the span of their newer reply. Returns the deleted (date_start, date_end) pairs.

    A leave-plan thread collects "updated plan:" replies that restate the whole
    plan. Each reply is its own event, so the old reply's rows survived the new
    one (observed 2026-10-22: one teammate's 19-23 Oct leave stayed after a later
    reply cut it to 19-21 Oct, marking 22-23 Oct as leave). Scoped to the same
    thread and actor, and to rows overlapping the new reply's date span, so
    unrelated ad-hoc leave messages and older non-overlapping plan entries stay.
    """
    root = _thread_root(ev_id)
    if root is None:
        return []
    dropped: list[tuple] = []
    by_actor: dict[str, list[str]] = {}
    for lv in leaves:
        if lv["date_start"]:
            by_actor.setdefault(lv["actor"], []).extend(
                [lv["date_start"], lv["date_end"] or lv["date_start"]])
    for actor, dates in by_actor.items():
        lo, hi = min(dates), max(dates)
        rows = conn.execute(
            "SELECT id, date_start, date_end FROM team_leaves "
            "WHERE actor = ? AND event_id LIKE ? AND event_id != ? "
            "AND mentioned_at < ? AND date_start IS NOT NULL "
            "AND date_start <= ? AND COALESCE(date_end, date_start) >= ?",
            (actor, root + ":%", ev_id, mentioned_at, hi, lo)).fetchall()
        for rid, ds, de in rows:
            conn.execute("DELETE FROM team_leaves WHERE id = ?", (rid,))
            dropped.append((ds, de))
            print(f"[supersede] {actor} {ds}..{de or ds} replaced by {ev_id}")
    return dropped


def _superseded_by_newer_plan(conn, ev_id: str, mentioned_at: str,
                              lv: dict) -> bool:
    """True when a NEWER reply by the same actor in the same leave-plan thread
    already covers lv's dates — the mirror of _supersede_older_plans.

    Needed once an edited reply is re-classified (leaves_dump re-emits edits):
    the old reply comes back AFTER the newer "updated plan:" reply was applied,
    and without this its stale ranges would be re-inserted over the update.
    The newer reply's span is min..max of its rows, as _supersede_older_plans
    uses for the reply being applied.
    """
    root = _thread_root(ev_id)
    if root is None or not lv["date_start"]:
        return False
    lo, hi = lv["date_start"], lv["date_end"] or lv["date_start"]
    row = conn.execute(
        "SELECT 1 FROM team_leaves WHERE actor = ? AND event_id LIKE ? "
        "AND event_id != ? AND mentioned_at > ? AND date_start IS NOT NULL "
        "GROUP BY event_id "
        "HAVING MIN(date_start) <= ? AND MAX(COALESCE(date_end, date_start)) >= ? "
        "LIMIT 1",
        (lv["actor"], root + ":%", ev_id, mentioned_at, hi, lo)).fetchone()
    return row is not None


def _valid_date(s: str | None) -> bool:
    if s is None:
        return True
    if not DATE_RE.match(s):
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _validate(v: dict, pending_map: dict[str, dict],
              team_canonical: set[str]) -> tuple[dict | None, list[str]]:
    errs: list[str] = []
    ev_id = v.get("event_id")
    if not ev_id:
        return None, ["missing event_id"]
    p = pending_map.get(ev_id)
    if p is None:
        return None, [f"event_id {ev_id} not in pending — stale verdict"]

    conf = v.get("confidence")
    if conf is None or not isinstance(conf, (int, float)):
        errs.append(f"{ev_id}: missing/invalid confidence")
        return None, errs
    if conf < CONFIDENCE_MIN:
        errs.append(f"{ev_id}: confidence {conf:.2f} < {CONFIDENCE_MIN} — stays pending")
        return None, errs

    is_leave = v.get("is_leave")
    if not isinstance(is_leave, bool):
        errs.append(f"{ev_id}: is_leave must be boolean")
        return None, errs

    if not is_leave:
        # Mark processed, no leave rows. Still valid.
        return {
            "event_id": ev_id,
            "is_leave": False,
            "confidence": conf,
            "leaves": [],
            "pending": p,
        }, []

    leaves_raw = v.get("leaves") or []
    if not isinstance(leaves_raw, list) or not leaves_raw:
        errs.append(f"{ev_id}: is_leave=true but leaves[] empty")
        return None, errs

    cleaned_leaves: list[dict] = []
    for i, lv in enumerate(leaves_raw):
        actor = lv.get("actor")
        if actor not in team_canonical:
            errs.append(f"{ev_id}.leaves[{i}]: actor '{actor}' not in team")
            return None, errs
        ds = lv.get("date_start")
        de = lv.get("date_end")
        if not _valid_date(ds):
            errs.append(f"{ev_id}.leaves[{i}]: bad date_start {ds!r}")
            return None, errs
        if not _valid_date(de):
            errs.append(f"{ev_id}.leaves[{i}]: bad date_end {de!r}")
            return None, errs
        if ds and de and de < ds:
            errs.append(f"{ev_id}.leaves[{i}]: date_end < date_start")
            return None, errs
        reason = (lv.get("reason") or "other").lower()
        if reason not in REASON_ENUM:
            reason = "other"
        cleaned_leaves.append({
            "actor": actor,
            "date_start": ds,
            "date_end": de,
            "reason": reason,
        })

    return {
        "event_id": ev_id,
        "is_leave": True,
        "confidence": conf,
        "leaves": cleaned_leaves,
        "pending": p,
    }, []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(VERDICTS_JSON),
                    help=f"verdicts JSON (default {VERDICTS_JSON.name})")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate only, no DB writes")
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"[err] no verdicts file at {in_path}", file=sys.stderr)
        return 2
    if not PENDING_JSON.exists():
        print(f"[err] no pending file at {PENDING_JSON}", file=sys.stderr)
        return 2

    pending_payload = json.loads(PENDING_JSON.read_text())
    pending_map = {p["event_id"]: p for p in pending_payload.get("pending", [])}
    team_canonical = set(pending_payload.get("team_canonical") or [])

    verdicts_in = json.loads(in_path.read_text())
    # Accept either {"verdicts": [...]} or a bare list.
    if isinstance(verdicts_in, dict):
        verdicts_list = verdicts_in.get("verdicts") or []
    else:
        verdicts_list = verdicts_in

    print(f"[in] {len(verdicts_list)} verdicts, {len(pending_map)} pending events")

    accepted: list[dict] = []
    rejected = 0
    for v in verdicts_list:
        cleaned, errs = _validate(v, pending_map, team_canonical)
        if cleaned is None:
            rejected += 1
            for e in errs:
                print(f"  REJECT {e}", file=sys.stderr)
            continue
        accepted.append(cleaned)

    print(f"[validate] {len(accepted)} accepted, {rejected} rejected")

    if args.dry_run:
        print("[dry] no DB writes")
        return 0

    if not accepted:
        return 0

    conn = get_db()
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    n_rows = 0
    n_proc = 0
    touched_months: set[tuple[int, int]] = set()
    for v in accepted:
        ev_id = v["event_id"]
        p = v["pending"]
        # Mark processed (whether real leave or false positive).
        conn.execute(
            "INSERT OR REPLACE INTO team_leaves_processed "
            "(event_id, processed_at, is_leave, confidence) VALUES (?,?,?,?)",
            (ev_id, now_iso, 1 if v["is_leave"] else 0, v["confidence"]),
        )
        n_proc += 1
        if not v["is_leave"]:
            continue
        # Wipe any prior rows for this event_id then insert fresh (allows re-classify).
        # Capture the outgoing rows' months too — a re-classify that moves or drops
        # a date range invalidates the month it left, not just the one it lands in.
        for old_ds, old_de in conn.execute(
            "SELECT date_start, date_end FROM team_leaves WHERE event_id = ?", (ev_id,)
        ).fetchall():
            touched_months |= _months_spanned(old_ds, old_de)
        conn.execute("DELETE FROM team_leaves WHERE event_id = ?", (ev_id,))
        for lv in v["leaves"]:
            if _superseded_by_newer_plan(conn, ev_id, p["mentioned_at"], lv):
                print(f"[supersede] {lv['actor']} {lv['date_start']}.."
                      f"{lv['date_end'] or lv['date_start']} from {ev_id} "
                      f"skipped — a newer plan reply covers it")
                continue
            conn.execute(
                """INSERT INTO team_leaves
                   (event_id, actor, mentioned_at, date_start, date_end,
                    reason, channel_id, channel_name, body_excerpt, url,
                    confidence, extracted_by, classified_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ev_id, lv["actor"], p["mentioned_at"],
                    lv["date_start"], lv["date_end"], lv["reason"],
                    p["channel_id"], p["channel_name"],
                    p["body_excerpt"], p["url"],
                    v["confidence"], "chat", now_iso,
                ),
            )
            touched_months |= _months_spanned(lv["date_start"], lv["date_end"])
            n_rows += 1
        for old_ds, old_de in _supersede_older_plans(
                conn, ev_id, p["mentioned_at"], v["leaves"]):
            touched_months |= _months_spanned(old_ds, old_de)
    conn.commit()
    print(f"[apply] +{n_rows} leaves rows · +{n_proc} processed gates")

    # Only after the commit — a cache dropped before a failed write would be
    # rebuilt from the old DB state and look authoritative again.
    if touched_months:
        _invalidate_capacity_caches(touched_months)

    # Archive verdicts file.
    archive = in_path.with_suffix(f".{now_iso[:19].replace(':','')}.json")
    in_path.rename(archive)
    print(f"[archive] {in_path.name} → {archive.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

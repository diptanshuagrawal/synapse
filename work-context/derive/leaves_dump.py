#!/usr/bin/env python3
"""leaves_dump.py — regex prefilter for team leave mentions in Slack.

Phase 1 of the chat-classify leave pipeline (mirrors derive/dump_pending.py
for subjects):

  0. Re-drain leave-plan thread roots from the Slack API first, bypassing the
     ingest-side ~24h per-thread cooldown. Thread replies never arrive via
     conversations.history, so without this a leave plan posted this morning is
     absent from events.db and the scan below misses it silently.
  1. Pull slack events from last 60 days authored by a team member
     (team = people.yaml scope:team direct reports, owner included).
  2. Filter messages whose body matches one of the leave-coordination
     regex patterns (OOO, WFH, on leave, vacation, holiday, etc.).
  3. Skip events already in team_leaves_processed (dedup — covers both
     accepted leaves and rejected false positives).
  4. Write state/pending_leaves.json + state/pending_leaves.rules.md
     for the /leaves chat skill to consume.

No LLM. Anthropic auth stripped defensively by run-leaves.sh.

Usage:
    python derive/leaves_dump.py                # default 60-day window
    python derive/leaves_dump.py --days 30
    python derive/leaves_dump.py --reset        # reprocess everything,
                                                  clears team_leaves_processed
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH, get_db  # noqa: E402
from derive.sources_config import owner_email  # noqa: E402

DEFAULT_DAYS = 60
STATE_DIR = _REPO_ROOT / "state"
PENDING_JSON = STATE_DIR / "pending_leaves.json"
RULES_MD = STATE_DIR / "pending_leaves.rules.md"
CHANNELS_YAML = _REPO_ROOT / "config" / "slack_channels.yaml"
PEOPLE_YAML = _REPO_ROOT / "config" / "people.yaml"
OWNER_EMAIL = owner_email()

# ── Regex prefilter ──────────────────────────────────────────────────────────
# Casts a wide net — false positives drop in the chat-classify phase.
# Word boundary anchored; case-insensitive.
LEAVE_PATTERN = re.compile(
    r"\b("
    r"OOO|OOTO|out\s+of\s+office|"
    r"on\s+(?:leave|holiday|vacation)|"
    r"WFH|working\s+from\s+home|"
    r"PTO|"
    r"vacation|holiday|holidays|"
    r"sick|unwell|"
    r"won.?t\s+be\s+(?:available|online|around|in)|"
    r"back\s+(?:on|by)\s+\w+|"
    r"taking\s+(?:a\s+)?(?:day|days|week|leave|off)|"
    r"off\s+(?:today|tomorrow|this|next|on)|"
    r"leaving\s+early|"
    r"half[\s-]?day|"
    r"away\s+(?:from|today|tomorrow|next|this)|"
    r"travelling|traveling|"
    r"(?:on|going\s+on)\s+(?:a\s+)?break"
    r")\b",
    re.IGNORECASE,
)

# Leave-plan prompt detector. When a thread ROOT asks the team to share
# their leave plan, the replies are bare date lists ("2-3 July, 7-8 July")
# that carry no leave keyword and so escape LEAVE_PATTERN. We surface every
# team-authored reply in such a thread regardless of keyword match.
LEAVE_PLAN_PROMPT = re.compile(r"leave\s*plan|leave\s*calendar", re.IGNORECASE)

# Body length cap for excerpt sent to chat.
EXCERPT_MAX = 300


def _refresh_leave_plan_threads(roots: set[tuple[str, str]]) -> None:
    """Re-drain leave-plan thread roots from the Slack API before scanning.

    Slack's conversations.history never returns thread replies — they land only
    via a separate per-thread conversations.replies drain, which the ingest path
    throttles to ~once/day per thread (see slack_backfill_app.active_thread_parents).
    A leave plan posted this morning therefore sits in Slack but NOT in events.db
    until that cooldown lapses, so the scan below silently misses it and the run
    stamps success on an incomplete day (observed 2026-09-24: thread drained
    00:46Z, three November plans posted 06:05Z, dump ran 07:39Z and saw none).

    The dump owns this dependency, so the dump guarantees it — that way cron,
    a manual /leaves and an ad-hoc backfill all get fresh threads, rather than
    the guarantee living in one caller's task file.

    Best-effort: a drain failure degrades freshness, it does not invalidate the
    scan, so we warn loudly and continue on any error. Slack creds are absent by
    design in the chat-classify path (run-leaves.sh strips LLM creds and
    _assert_auth_clean refuses to run beside ANTHROPIC_API_KEY) — that is a
    skip, not a crash.
    """
    if not roots:
        return
    try:
        from ingest.slack_api_client import (  # noqa: PLC0415
            SlackClient, _assert_auth_clean, _load_env, make_name_resolver,
        )
        from ingest.slack_backfill_app import fetch_threads  # noqa: PLC0415
        from derive.slack_backfill_helper import _clamp_parent_after_drain  # noqa: PLC0415

        token = _assert_auth_clean(_load_env())
        client = SlackClient(token=token)
        users_cache = client.build_users_cache()
        name_resolver = make_name_resolver(client, users_cache)
        subteams_cache = client.build_subteams_cache()
    except Exception as e:
        print(f"[leave-plan][WARN] drain skipped ({type(e).__name__}: {e}) — "
              f"pending may miss replies posted since the last ingest drain")
        return

    inserted = 0
    dconn = sqlite3.connect(DB_PATH)
    try:
        for cid, root_ts in sorted(roots):
            try:
                n, _, errs = fetch_threads(
                    client, cid, [root_ts], False, users_cache,
                    keep_bot_messages=False, name_resolver=name_resolver,
                    subteams_cache=subteams_cache,
                )
                inserted += n
                # Mirror fetch_threads_capped: keep reply_count honest and let
                # the ingest-side cooldown see that this thread was just walked.
                _clamp_parent_after_drain(dconn, cid, root_ts)
                for err in errs:
                    print(f"[leave-plan][WARN] drain {cid}:{root_ts}: {err}")
            except Exception as e:
                print(f"[leave-plan][WARN] drain {cid}:{root_ts} failed "
                      f"({type(e).__name__}: {e})")
    finally:
        dconn.close()
    print(f"[leave-plan] drained {len(roots)} root(s) · +{inserted} reply(ies)")


def _thread_root_ts(event_id: str, thread_ts: str | None) -> str:
    """Return the thread-root ts for a slack event.

    Reply ids are `slack:<cid>:<root_ts>:<reply_ts>` (4 colon-parts); root
    messages are `slack:<cid>:<ts>`. Prefer the id-encoded root, fall back to
    the thread_ts column, finally the event's own ts (it is itself a root).
    """
    parts = event_id.split(":")
    if len(parts) >= 4:
        return parts[2]
    if thread_ts:
        return thread_ts
    return parts[-1]


def _load_team_emails() -> set[str]:
    """Direct-reports emails ONLY — owner's own leaves intentionally excluded.

    Owner usually knows their own plans; the leave dashboard tracks the
    team they manage. Differs from slack_team.load_team_emails() which
    includes owner for the is_team_involved check (where owner-authored
    messages legitimately count as team activity).

    Source of truth: people.yaml `scope: team` (consolidated 2026-07-16 —
    team.md is the manager's notes doc and no longer drives membership).
    """
    emails: set[str] = set()
    if PEOPLE_YAML.exists():
        with PEOPLE_YAML.open() as f:
            cfg = yaml.safe_load(f) or {}
        emails = {p.get("email") for p in cfg.get("people", []) or []
                  if p.get("scope") == "team" and p.get("email")}
    emails.discard(OWNER_EMAIL)  # belt + suspenders
    return emails


def _load_team_canonical() -> set[str]:
    """Return canonical github handles for owner + direct reports."""
    team_emails = _load_team_emails()
    out: set[str] = set()
    if not PEOPLE_YAML.exists():
        return out
    with PEOPLE_YAML.open() as f:
        cfg = yaml.safe_load(f) or {}
    for p in cfg.get("people", []):
        if p.get("email") in team_emails and p.get("canonical"):
            out.add(p["canonical"])
    return out


def _load_team_slack_map() -> dict[str, str]:
    """Return {slack_id: canonical} for owner + direct reports.

    Slack `events.actor` stores the raw `U…` slack_id (unlike github/jira
    where actor is canonical). Filter at SQL time on slack_id; resolve
    to canonical at extract time so team_leaves.actor stays canonical.
    """
    team_emails = _load_team_emails()
    out: dict[str, str] = {}
    if not PEOPLE_YAML.exists():
        return out
    with PEOPLE_YAML.open() as f:
        cfg = yaml.safe_load(f) or {}
    for p in cfg.get("people", []):
        email = p.get("email")
        sid = p.get("slack_id")
        canon = p.get("canonical")
        if email in team_emails and sid and canon:
            out[sid] = canon
    return out


def _check_identity_sane() -> list[str]:
    """Fail loudly when one human carries two canonicals in people.yaml.

    Both _load_team_canonical (keyed on email) and _load_team_slack_map
    (keyed on slack_id) silently tolerate duplicate entries for the same
    person: the canonical set just grows, and the slack map lets whichever
    entry parses last win. Nothing downstream notices — capacity_engine
    does `leaves.get(p["canonical"], {})`, so a handle mismatch reads as
    "no leaves" rather than raising.

    That cost us the 2026-10-06 run: a stale `scope: org` duplicate of
    Sai Vignesh (canonical `saivignesh`, same email/slack_id/jira_id as his
    `scope: team` entry with canonical `sai-vignesh`) won the slack_id map,
    so every leave he announced was filed under a handle the Synapse
    monthly tab never reads. His 19-23 Oct vacation showed as leave=0.

    Returns a list of fatal errors; empty means the roster is coherent.
    """
    errs: list[str] = []
    if not PEOPLE_YAML.exists():
        return errs
    with PEOPLE_YAML.open() as f:
        cfg = yaml.safe_load(f) or {}
    people = cfg.get("people", []) or []
    team_emails = _load_team_emails()

    by_email: dict[str, set[str]] = {}
    by_slack: dict[str, set[str]] = {}
    for p in people:
        email, canon = p.get("email"), p.get("canonical")
        if email not in team_emails or not canon:
            continue
        by_email.setdefault(email, set()).add(canon)
        if p.get("slack_id"):
            by_slack.setdefault(p["slack_id"], set()).add(canon)

    for email, canons in sorted(by_email.items()):
        if len(canons) > 1:
            errs.append(f"email {email} maps to {len(canons)} canonicals: "
                        f"{', '.join(sorted(canons))} — dedupe people.yaml")
    for sid, canons in sorted(by_slack.items()):
        if len(canons) > 1:
            errs.append(f"slack_id {sid} maps to {len(canons)} canonicals: "
                        f"{', '.join(sorted(canons))} — dedupe people.yaml")

    # A team member with no slack_id is invisible to the scan below: their
    # leave announcements can never be picked up. Not fatal (they may simply
    # not be on Slack yet) but it must not pass unremarked.
    missing = sorted(e for e in team_emails
                     if not any(p.get("email") == e and p.get("slack_id")
                                for p in people))
    for email in missing:
        print(f"[warn] team member {email} has no slack_id — their leave "
              "announcements cannot be detected", file=sys.stderr)

    return errs


def _warn_orphan_leave_actors(conn, team_canonical: set[str]) -> None:
    """Flag team_leaves rows whose actor is not a current team canonical.

    Expected for people who have left the team (their old rows stay for
    history), so this warns rather than fails — but a handle that drifts
    from the roster shows up here first.
    """
    try:
        rows = conn.execute(
            "SELECT actor, COUNT(*) FROM team_leaves GROUP BY actor").fetchall()
    except Exception:
        return
    orphans = [(a, n) for a, n in rows if a not in team_canonical]
    if orphans:
        detail = ", ".join(f"{a} ({n})" for a, n in sorted(orphans))
        print(f"[warn] {len(orphans)} leave actor(s) not on the current team: "
              f"{detail} — ex-members are fine; a near-miss of a current "
              "handle means an identity split", file=sys.stderr)


def _load_channel_names() -> dict[str, str]:
    if not CHANNELS_YAML.exists():
        return {}
    with CHANNELS_YAML.open() as f:
        cfg = yaml.safe_load(f) or {}
    return {c["id"]: c.get("name", c["id"]) for c in cfg.get("channels", [])}


def _rules_md(window_days: int) -> str:
    return f"""# /leaves chat-classify rules

Window: last {window_days} days of Slack events authored by team members.

## What to emit per event

For each entry in `pending_leaves.json`, emit one verdict in
`state/verdicts.leaves.json`. Schema:

```json
{{
  "event_id": "<copy from pending>",
  "is_leave": true,                  // false → mark processed, no leave rows
  "confidence": 0.0..1.0,            // your certainty
  "leaves": [                        // list — multiple OK per event
    {{
      "actor": "<canonical github handle from team>",
      "date_start": "YYYY-MM-DD",    // null if not parseable
      "date_end":   "YYYY-MM-DD",    // null if single-day or open-ended
      "reason":     "wfh|vacation|sick|holiday|ooo|travel|other"
    }}
  ]
}}
```

## Rules

1. **`is_leave: false`** when the regex matched but the message is NOT
   a leave announcement (e.g. "I was OOO yesterday so I missed this"
   referring to a past mention, or "fixed the OOO module bug" — wrong
   sense). Mark processed so it doesn't re-emerge.

2. **Resolve relative dates** against `mentioned_at` (ISO timestamp on
   the pending row). "Tomorrow" → mentioned_at + 1d. "Next Monday" →
   next calendar Monday after mentioned_at. "Till 5th" → infer month
   from mentioned_at; if 5th already past in that month, assume next
   month.

3. **Multi-person mentions** ("@bob and @eve out tomorrow") →
   one verdict, multiple entries in `leaves[]`. Use canonical handles
   from the team set listed below.

4. **Ambiguous date** (e.g. "may take leave next week, will confirm")
   → emit with `date_start: null, date_end: null` AND
   `reason: "future leave (date TBD)"`. Confidence ≤ 0.7 → row stays
   pending until next dump catches a clearer mention.

5. **Confidence < 0.7** → row is rejected by apply_leaves and stays
   pending. Don't fabricate certainty.

6. **Leave-plan thread replies** — some events are surfaced because they
   are replies in a "share your leave plan" thread, NOT because they
   matched a keyword. These are often bare date lists
   ("2-3 July, 7-8 July, 13-17July") → parse EACH range into its own
   `leaves[]` entry (one verdict, many entries), reason `vacation`
   unless stated otherwise. Resolve the year/month from `mentioned_at`.
   A pure ack ("noted", "done") in such a thread → `is_leave: false`.

## Team set (canonical handles)

See `pending_leaves.json::team_canonical` — only these names belong in
`leaves[].actor`. Mentions of non-team members (e.g. cross-team folks)
should be discarded.

## After classifying

Write the verdict array to `state/verdicts.leaves.json`, then run:

```bash
.venv/bin/python derive/apply_leaves.py
.venv/bin/python derive/render_leaves.py
```

Then archive `verdicts.leaves.json` → `verdicts.leaves.<ts>.json`.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"lookback window in days (default {DEFAULT_DAYS})")
    ap.add_argument("--reset", action="store_true",
                    help="clear team_leaves_processed first (reprocess everything)")
    ap.add_argument("--no-drain", action="store_true",
                    help="skip the pre-scan leave-plan thread drain (offline runs; "
                         "pending may miss replies newer than the last ingest drain)")
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    identity_errs = _check_identity_sane()
    if identity_errs:
        for e in identity_errs:
            print(f"[err] identity: {e}", file=sys.stderr)
        print("[err] refusing to classify against a split roster — leaves "
              "would be filed under a handle the capacity engine never reads",
              file=sys.stderr)
        return 2

    team_canonical = _load_team_canonical()
    team_slack_map = _load_team_slack_map()
    if not team_canonical or not team_slack_map:
        print("[err] team set empty — check people.yaml scope:team "
              f"(canonical={len(team_canonical)}, slack_ids={len(team_slack_map)})",
              file=sys.stderr)
        return 2
    print(f"[team] {len(team_canonical)} canonical · {len(team_slack_map)} slack_ids")

    channel_names = _load_channel_names()

    since_dt = datetime.now(tz=timezone.utc) - timedelta(days=args.days)
    since_iso = since_dt.isoformat().replace("+00:00", "Z")

    conn = get_db()
    _warn_orphan_leave_actors(conn, team_canonical)
    if args.reset:
        n = conn.execute("DELETE FROM team_leaves_processed").rowcount
        conn.commit()
        print(f"[reset] cleared {n} rows from team_leaves_processed")

    # Thread roots that ask the team for their leave plan. Replies in these
    # threads are bare date lists with no leave keyword — surface them anyway.
    # The prompt itself is usually owner-authored (excluded from the team scan),
    # so scan ALL slack events in window, narrowed by a cheap LIKE prefilter.
    plan_root_ts: set[str] = set()
    plan_roots: set[tuple[str, str]] = set()
    for rid, rthread, rbody, rcid in conn.execute(
        "SELECT id, thread_ts, body, channel_id FROM events "
        "WHERE source = 'slack' AND ts >= ? AND body LIKE '%leave%'",
        [since_iso],
    ):
        if rbody and LEAVE_PLAN_PROMPT.search(rbody):
            root = _thread_root_ts(rid, rthread)
            plan_root_ts.add(root)
            if rcid:
                plan_roots.add((rcid, root))
    print(f"[leave-plan] {len(plan_root_ts)} leave-plan thread root(s) in window")

    # Freshen those threads BEFORE the scan below reads them — replies posted
    # since the last ingest drain are not in events.db yet. See the function
    # docstring for why this belongs to the dump and not to its callers.
    if not args.no_drain:
        _refresh_leave_plan_threads(plan_roots)

    # Slack events.actor stores raw U-ids — filter on slack_id, resolve to
    # canonical at emit time.
    slack_ids = sorted(team_slack_map.keys())
    placeholders = ",".join(["?"] * len(slack_ids))
    q = f"""
        SELECT e.id, e.actor, e.ts, e.body, e.channel_id, e.url, e.thread_ts
        FROM events e
        WHERE e.source = 'slack'
          AND e.ts >= ?
          AND e.actor IN ({placeholders})
          AND (e.deleted_ts IS NULL)
          AND e.id NOT IN (SELECT event_id FROM team_leaves_processed)
        ORDER BY e.ts ASC
    """
    params = [since_iso, *slack_ids]
    rows = conn.execute(q, params).fetchall()
    print(f"[scan] {len(rows)} candidate events from team in window")

    pending: list[dict] = []
    n_plan_reply = 0
    for r in rows:
        ev_id, actor_slack_id, ts, body, cid, url, thread_ts = r
        if not body:
            continue
        in_leave_plan_thread = (
            plan_root_ts
            and _thread_root_ts(ev_id, thread_ts) in plan_root_ts
        )
        if not LEAVE_PATTERN.search(body) and not in_leave_plan_thread:
            continue
        if in_leave_plan_thread and not LEAVE_PATTERN.search(body):
            n_plan_reply += 1
        canonical = team_slack_map.get(actor_slack_id, actor_slack_id)
        excerpt = body[:EXCERPT_MAX]
        if len(body) > EXCERPT_MAX:
            excerpt += "…"
        pending.append({
            "event_id": ev_id,
            "actor": canonical,                # canonical github handle
            "actor_slack_id": actor_slack_id,  # raw, for chat verification
            "mentioned_at": ts,
            "channel_id": cid,
            "channel_name": channel_names.get(cid, cid or ""),
            "body_excerpt": excerpt,
            "url": url,
        })

    print(f"[regex] {len(pending)} events matched "
          f"({n_plan_reply} via leave-plan thread, rest keyword)")

    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "window_days": args.days,
        "team_canonical": sorted(team_canonical),
        "pending": pending,
    }
    PENDING_JSON.write_text(json.dumps(payload, indent=2, sort_keys=False))
    RULES_MD.write_text(_rules_md(args.days))
    print(f"[out] wrote {PENDING_JSON.name} + {RULES_MD.name}")

    if not pending:
        print("[summary] nothing to classify")
    else:
        print(f"[summary] {len(pending)} events awaiting /leaves chat classify")
    return 0


if __name__ == "__main__":
    sys.exit(main())

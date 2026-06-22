#!/usr/bin/env python3
"""leaves_dump.py — regex prefilter for team leave mentions in Slack.

Phase 1 of the chat-classify leave pipeline (mirrors derive/dump_pending.py
for subjects):

  1. Pull slack events from last 60 days authored by a team member
     (team = team.md direct reports, owner included).
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
TEAM_MD = _REPO_ROOT.parent / "management" / "context" / "team.md"
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
    """
    emails: set[str] = set()
    if TEAM_MD.exists():
        for line in TEAM_MD.read_text().splitlines():
            m = re.match(r"^##\s+.+?\s+[—-]+\s+(\S+@\S+)\s*$", line.strip())
            if m:
                emails.add(m.group(1).strip())
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
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    team_canonical = _load_team_canonical()
    team_slack_map = _load_team_slack_map()
    if not team_canonical or not team_slack_map:
        print("[err] team set empty — check team.md + people.yaml "
              f"(canonical={len(team_canonical)}, slack_ids={len(team_slack_map)})",
              file=sys.stderr)
        return 2
    print(f"[team] {len(team_canonical)} canonical · {len(team_slack_map)} slack_ids")

    channel_names = _load_channel_names()

    since_dt = datetime.now(tz=timezone.utc) - timedelta(days=args.days)
    since_iso = since_dt.isoformat().replace("+00:00", "Z")

    conn = get_db()
    if args.reset:
        n = conn.execute("DELETE FROM team_leaves_processed").rowcount
        conn.commit()
        print(f"[reset] cleared {n} rows from team_leaves_processed")

    # Thread roots that ask the team for their leave plan. Replies in these
    # threads are bare date lists with no leave keyword — surface them anyway.
    # The prompt itself is usually owner-authored (excluded from the team scan),
    # so scan ALL slack events in window, narrowed by a cheap LIKE prefilter.
    plan_root_ts: set[str] = set()
    for rid, rthread, rbody in conn.execute(
        "SELECT id, thread_ts, body FROM events "
        "WHERE source = 'slack' AND ts >= ? AND body LIKE '%leave%'",
        [since_iso],
    ):
        if rbody and LEAVE_PLAN_PROMPT.search(rbody):
            plan_root_ts.add(_thread_root_ts(rid, rthread))
    print(f"[leave-plan] {len(plan_root_ts)} leave-plan thread root(s) in window")

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

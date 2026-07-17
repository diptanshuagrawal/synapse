"""
slack_team.py — single source of truth for "who's on my team" lookup.

Resolves team identifiers from two configs:

  1. `config/people.yaml` — every entry with `scope: team` IS the roster
     (consolidated 2026-07-16; `management/context/team.md` is the manager's
     1:1-notes doc only and no longer drives membership). Owner auto-included.
     → set of individual Slack user-ids (UID).

  2. `config/team_subteams.yaml` (subteams list)
     → set of Slack user-group ids (SID) the team is addressed by, e.g.
     `S0EXAMPLE` for `team-devs` / `EX-team`.

Exposes:
  - `load_team_slack_ids() -> dict[str, str]` — {UID: canonical name}
  - `load_team_subteam_ids() -> set[str]` — SIDs from team_subteams.yaml
  - `is_team_involved(actor_id, body, team_slack_ids, team_subteam_ids=None)`
    → True if author is a team UID, OR body @-mentions a team UID
    (`<@U…>` form), OR body pings a team subteam handle (`<!subteam^S…>` form).

`is_team_involved` is the shared filter consumed by:
  - `ingest/slack_ingest_app.py::fetch_history_team_filtered` (steady-state)
  - `ingest/slack_backfill_app.py::fetch_history` (one-shot backfill)
  - `derive/slack_team_filter_cleanup.py` (retro purge on team_involved flips)

All three call sites also implement the bot-deferred reply walk: for
`ingest_mode: team_involved` channels, a bot-authored root (PagerDuty,
OpsGenie, "Alert Incident Commander" templates) is NOT dropped
early. Replies are inspected first; if any reply is team-involved the
bot-rooted incident header is retained alongside the team replies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from derive.sources_config import owner_email

_REPO_ROOT = Path(__file__).resolve().parent.parent
PEOPLE_YAML = _REPO_ROOT / "config" / "people.yaml"
TEAM_SUBTEAMS_YAML = _REPO_ROOT / "config" / "team_subteams.yaml"
OWNER_EMAIL = owner_email()


def _team_people() -> list[dict]:
    """people.yaml entries with `scope: team`, plus the owner's entry (any scope)."""
    if not PEOPLE_YAML.exists():
        return []
    with PEOPLE_YAML.open() as f:
        cfg = yaml.safe_load(f) or {}
    return [p for p in cfg.get("people", []) or []
            if p.get("scope") == "team" or p.get("email") == OWNER_EMAIL]


def load_team_emails() -> set[str]:
    """Emails of the roster = people.yaml `scope: team` entries. Owner auto-included.

    SINGLE SOURCE OF TRUTH (consolidated 2026-07-16): membership previously
    lived in management/context/team.md AND people.yaml scope, kept in sync by
    hand — a new dev added to one but not the other got silent partial
    coverage. team.md is now the manager's notes doc only.
    """
    emails = {p.get("email") for p in _team_people() if p.get("email")}
    emails.add(OWNER_EMAIL)
    return emails


def load_team_slack_ids() -> dict[str, str]:
    """Returns {slack_id: canonical_name} for the owner + every direct report.

    Source of truth: people.yaml `scope: team` (owner included regardless of
    scope). Cross-team collaborators (scope: org / external) are excluded.
    """
    return {p["slack_id"]: p.get("canonical", p["slack_id"])
            for p in _team_people() if p.get("slack_id")}


def load_owner_slack_id() -> Optional[str]:
    """Returns the owner's Slack user-id (resolved via OWNER_EMAIL → people.yaml),
    or None if people.yaml is missing or the owner has no slack_id mapping."""
    if not PEOPLE_YAML.exists():
        return None
    with PEOPLE_YAML.open() as f:
        cfg = yaml.safe_load(f)
    for p in cfg.get("people", []):
        if p.get("email") == OWNER_EMAIL and p.get("slack_id"):
            return p["slack_id"]
    return None


def load_team_subteam_ids() -> set[str]:
    """Returns the set of Slack subteam (user-group) IDs that represent THIS team.

    Source of truth: config/team_subteams.yaml. Missing file → empty set
    (warning-worthy but not fatal — old behaviour preserved).

    Used by is_team_involved() to catch threads that ping the team via
    <!subteam^Sxxx> handles instead of individual @UID mentions.
    """
    if not TEAM_SUBTEAMS_YAML.exists():
        return set()
    with TEAM_SUBTEAMS_YAML.open() as f:
        cfg = yaml.safe_load(f) or {}
    out: set[str] = set()
    for s in cfg.get("subteams", []) or []:
        sid = (s or {}).get("id")
        if sid:
            out.add(sid)
    return out


def is_team_involved(actor_id: Optional[str], body: Optional[str],
                     team_slack_ids: set[str],
                     team_subteam_ids: Optional[set[str]] = None) -> bool:
    """True if msg is involves the team via any of:
      - author is a team member (actor_id in team_slack_ids)
      - body @-mentions a team member (<@UID> or <@UID|name>)
      - body pings a team subteam handle (<!subteam^SID> or <!subteam^SID|handle>)

    `team_subteam_ids` is optional for backward-compat. When None or empty,
    the subteam check is skipped (legacy behaviour).

    Used by both ingest_app (steady-state) and backfill_app (one-shot)
    when ingest_mode=team_involved.
    """
    if actor_id and actor_id in team_slack_ids:
        return True
    if body:
        for uid in team_slack_ids:
            if f"<@{uid}" in body:  # matches <@U…> and <@U…|name>
                return True
        if team_subteam_ids:
            for sid in team_subteam_ids:
                if f"<!subteam^{sid}" in body:  # matches <!subteam^S…> and <!subteam^S…|handle>
                    return True
    return False

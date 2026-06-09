#!/usr/bin/env python3
"""
ownership_corrections.py — content-first ownership post-pass.

Runs AFTER `apply_verdicts.py` in the rollup pipeline. Resolves ownership from
the WORK (a subject's classified `domains`), not from who posted or where.

Signal priority (highest first):
  1. STRUCTURAL NOISE — slack channel-join/leave → `external` (not work).
  2. CONTENT — `domains` → owning team(s) via `config/domain_team_map.yaml`
     (`derive/ownership_resolve.resolve`). Dominant domain = primary; the rest
     = co_owners. This is the primary mechanism and fixes the cross-team recall
     hole (e.g. year-end close mis-attributed via a broadcast-channel author).
  3. CHAT verdict — the LLM's `owned_by_primary`, kept when a subject has no
     mappable domains.
  4. IDENTITY tiebreaker — author/root-actor → team, used ONLY when content +
     chat are both empty (thin-content subjects).

Idempotent: re-running converges to a no-op.

Emits a `basis` breakdown (content / chat / identity / noise / unresolved) so
the census reconciliation can surface how many subjects fell to the identity
tiebreaker (the residual blind spot) and which domains need review.

Usage:
    .venv/bin/python derive/ownership_corrections.py            # apply
    .venv/bin/python derive/ownership_corrections.py --dry-run  # report only
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.common import DB_PATH  # noqa: E402
from derive.ownership_resolve import load_map, resolve  # noqa: E402
from derive.sources_config import home_team, coowner_team, github_org, org_match_tokens  # noqa: E402

HOME_TEAM = home_team()
POTS_TEAM = coowner_team()
_MATCH_TOKENS = org_match_tokens()

HR_OOO_PHRASES = [
    " ooo", "ooo on", "on leave", "wfh", "feeling unwell", "fever", "sick",
    "won't be available", "won t be available", "out of office",
    "annual leave", "half day", "working from home", "work from home",
    "day off", "logout today", "log out today", "personal plans",
    "planned leave", "planned leaves", "on vacation",
]
# Admin / IT-helpdesk / meeting-nudge noise — not team WORK even when a team
# member posts it. Routed to `external` (noise) before identity fallback so it
# stops inflating team ownership.
ADMIN_NOISE_PHRASES = [
    "msf nomination", "msf nominations", "self reviews by", "self-reviews by",
    "performance review cycle",
    "unlock the laptop", "laptop is locked", "laptop unlock",
    "not able to login", "not able to log in", "vpn is enabled", "reset password",
    "fresh new seats", "seats for us", "new seats",
    "join standup", "join the standup", "please join standup", "pls join standup",
    "join the weekly sync", "join the daily",
]
CHANNEL_EVENT_PHRASES = ["has joined the channel", "has left the channel"]

# Slack subteam (user-group) S-id → owning team. Built by inverting teams.yaml
# (each team lists its `subteam_ids`) — teams.yaml is the single source of truth,
# so there are no Slack IDs hardcoded here. A subteam ping `<!subteam^S...>` is an
# explicit page of that team — strong ownership signal for cross-team threads
# the home roster doesn't cover.
def _build_subteam_team() -> dict[str, str]:
    import yaml
    path = _REPO_ROOT / "config" / "teams.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    out: dict[str, str] = {}
    for t in data.get("teams", []) or []:
        tid = t.get("id")
        for sid in t.get("subteam_ids", []) or []:
            if sid and tid:
                out[sid] = tid
    return out


SUBTEAM_TEAM = _build_subteam_team()
_SUBTEAM_RE = re.compile(r"\^(S[0-9A-Z]+)")


# ── Identity loading (tiebreaker only) ───────────────────────────────────────


def _load_identities() -> dict:
    import yaml
    people = yaml.safe_load((_REPO_ROOT / "config" / "people.yaml").read_text())
    teams = yaml.safe_load((_REPO_ROOT / "config" / "teams.yaml").read_text())

    # Channel-id → (name, class). Class drives slack ownership tiebreaker:
    # team/working-group service-c rooms → home; service-c alerts/oncall → home; announcements → external.
    chan: dict = {}
    chan_path = _REPO_ROOT / "config" / "slack_channels.yaml"
    if chan_path.exists():
        cy = yaml.safe_load(chan_path.read_text()) or {}
        for c in cy.get("channels", []) or []:
            if c.get("id"):
                chan[c["id"]] = (c.get("name", ""), c.get("class", ""))

    team_people = [p for p in people.get("people", []) if p.get("scope") == "team"]
    emails = {p["email"] for p in team_people if p.get("email")}
    jira_ids = {p["jira_id"] for p in team_people if p.get("jira_id")}
    slack_ids = {p["slack_id"] for p in team_people if p.get("slack_id")}

    dipt = next((t for t in teams["teams"] if t["id"] == HOME_TEAM), {})
    pots = next((t for t in teams["teams"] if t["id"] == POTS_TEAM), {})
    emails |= set(dipt.get("contributors_email", []) or [])
    jira_ids |= set(dipt.get("contributors_jira", []) or [])
    collab_slack = set(dipt.get("contributors_slack", []) or [])
    team_github = set(dipt.get("contributors_github", []) or [])
    pots_github = set(pots.get("contributors_github", []) or [])
    return {
        "emails": emails, "jira_ids": jira_ids, "slack_ids": slack_ids,
        "collab_slack": collab_slack, "team_github": team_github, "pots_github": pots_github,
        "author_roster": emails | jira_ids,
        "slack_team_or_collab": slack_ids | collab_slack,
        "chan": chan,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _root_event(conn, sub):
    """(title, body, actor, event_types) from the root/primary event."""
    rows = conn.execute(
        "SELECT title, body, actor, event_type FROM events WHERE subject=? ORDER BY "
        "CASE event_type WHEN 'thread_started' THEN 0 WHEN 'issue_created' THEN 0 "
        "WHEN 'page_created' THEN 0 WHEN 'pr_opened' THEN 0 ELSE 1 END, ts",
        (sub,),
    ).fetchall()
    if not rows:
        return "", "", "", set()
    etypes = {r[3] for r in rows}
    return rows[0][0] or "", rows[0][1] or "", rows[0][2] or "", etypes


def _pr_author(conn, sub):
    r = conn.execute(
        "SELECT actor FROM events WHERE subject=? AND event_type IN ('pr_opened','pr_merged') LIMIT 1",
        (sub,),
    ).fetchone()
    return r[0] if r else None


def _doc_author(conn, sub):
    r = conn.execute(
        "SELECT actor FROM events WHERE subject=? AND actor IS NOT NULL "
        "AND event_type IN ('issue_created','page_created','page_updated') ORDER BY ts DESC LIMIT 1",
        (sub,),
    ).fetchone()
    return r[0] if r else None


def _identity_fallback(conn, sub, source, idn):
    """IDENTITY tiebreaker — only for subjects with no content/chat owner.
    Returns (primary, co_owners, reason) or (None, [], None)."""
    if source == "github":
        a = _pr_author(conn, sub)
        if a in idn["pots_github"]:
            return POTS_TEAM, [HOME_TEAM], f"identity: PR by {a} (co-owner team); home co-owns repo."
        if a in idn["team_github"]:
            return HOME_TEAM, [], f"identity: PR by {a} (home roster)."
    if source in ("jira", "confluence"):
        a = _doc_author(conn, sub)
        if a and a in idn["author_roster"]:
            return HOME_TEAM, [], f"identity: author {a} (home roster)."
    if source == "slack":
        title, body, actor, _ = _root_event(conn, sub)
        if actor in idn["slack_team_or_collab"]:
            return HOME_TEAM, [], f"identity: root actor {actor} (home/collab)."
        bl = body.lower()
        if (any(f"<@{s}" in body or f"^{s}" in body for s in idn["slack_ids"])
                and not any(p in bl for p in HR_OOO_PHRASES)
                and not any(p in bl for p in CHANNEL_EVENT_PHRASES)):
            return HOME_TEAM, [], "identity: thread mentions home member, not HR/OOO."
        # Subteam ping → owning team (explicit cross-team page the home roster
        # doesn't cover). Single distinct non-home team wins; home co-owns if
        # also paged. Ambiguous (2+ non-home) falls through to channel class.
        pinged = {SUBTEAM_TEAM[s] for s in _SUBTEAM_RE.findall(body) if s in SUBTEAM_TEAM}
        nonhome = sorted(t for t in pinged if t != HOME_TEAM)
        if len(nonhome) == 1:
            co = [HOME_TEAM] if HOME_TEAM in pinged else []
            return nonhome[0], co, f"identity: subteam ping -> {nonhome[0]}."
        if pinged == {HOME_TEAM}:
            return HOME_TEAM, [], "identity: home subteam ping."
        # Channel class → team (service-c rooms) / external (org announcements).
        chid = sub.split(":")[1] if sub.count(":") >= 2 else ""
        name, cls = idn["chan"].get(chid, ("", ""))
        if cls == "team" or (cls == "working-group" and (any(t in name for t in _MATCH_TOKENS) or "accounting" in name)):
            return HOME_TEAM, [], f"identity: channel {name} (team)."
        if cls in ("alerts", "oncall") and any(t in name for t in _MATCH_TOKENS):
            return HOME_TEAM, [], f"identity: channel {name} ({cls})."
        if cls == "announcements":
            return "external", [], f"identity: channel {name} (announcements)."
    return None, [], None


def _source_of(subject: str) -> str:
    if subject.startswith("slack:"):
        return "slack"
    if subject.startswith("page:"):
        return "confluence"
    if subject.startswith(github_org() + "/"):
        return "github"
    return "jira"


# ── Main pass ────────────────────────────────────────────────────────────────


def correct(conn: sqlite3.Connection, dry: bool = False) -> dict:
    idn = _load_identities()
    m = load_map()
    rows = conn.execute(
        "SELECT subject, domains, owned_by_primary, co_owners_json FROM subject_summary"
    ).fetchall()

    stats = {"content": 0, "chat": 0, "identity": 0, "noise": 0, "unresolved": 0, "changed": 0}
    identity_fallback_subjects: list[str] = []

    for sub, domains_json, chat_primary, chat_co_json in rows:
        source = _source_of(sub)
        try:
            domains = json.loads(domains_json) if domains_json else []
        except (json.JSONDecodeError, TypeError):
            domains = []
        chat_co = []
        try:
            chat_co = json.loads(chat_co_json) if chat_co_json else []
        except (json.JSONDecodeError, TypeError):
            pass

        # 1. Structural noise — slack channel-join/leave + admin/OOO/helpdesk
        #    nudges → external (not team WORK even if a team member posts).
        if source == "slack":
            _, body, _, _ = _root_event(conn, sub)
            bl = body.lower()
            if (any(p in body for p in CHANNEL_EVENT_PHRASES)
                    or any(p in bl for p in HR_OOO_PHRASES)
                    or any(p in bl for p in ADMIN_NOISE_PHRASES)):
                stats["noise"] += 1
                if _set_owner(conn, sub, "external", [], 0.90,
                              "Admin/OOO/helpdesk/channel noise — external (not team work).", dry):
                    stats["changed"] += 1
                continue

        # 2/3. Content-first (with chat fallback inside resolve()).
        primary, co, basis = resolve(domains, chat_primary, chat_co, m)

        if basis == "content":
            stats["content"] += 1
            if _set_owner(conn, sub, primary, co, 0.90,
                          f"content: domains {domains[:3]} → {primary}", dry):
                stats["changed"] += 1
            continue

        # 4. Identity tiebreaker (no mappable domains).
        idp, idco, idreason = _identity_fallback(conn, sub, source, idn)
        if idp:
            stats["identity"] += 1
            identity_fallback_subjects.append(sub)
            if _set_owner(conn, sub, idp, idco, 0.75, idreason, dry):
                stats["changed"] += 1
            continue

        if basis == "chat" and chat_primary:
            stats["chat"] += 1  # keep chat verdict as-is
            continue

        stats["unresolved"] += 1  # no signal at all — leave/unknown

    if not dry:
        conn.commit()
    stats["identity_fallback_subjects"] = identity_fallback_subjects
    return stats


def _set_owner(conn, sub, primary, co_list, conf, reason, dry):
    cur = conn.execute(
        "SELECT owned_by_primary, co_owners_json FROM subject_summary WHERE subject=?", (sub,)
    ).fetchone()
    if cur is None:
        return False
    co_json = json.dumps(co_list)
    if cur[0] == primary and (cur[1] or "[]") == co_json:
        return False
    if not dry:
        conn.execute(
            "UPDATE subject_summary SET owned_by_primary=?, co_owners_json=?, "
            "owned_by_confidence=?, ownership_reasoning=? WHERE subject=?",
            (primary, co_json, conf, reason, sub),
        )
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    stats = correct(conn, args.dry_run)

    tag = "DRY-RUN" if args.dry_run else "applied"
    print(f"ownership_corrections ({tag}):")
    print(f"  content-resolved : {stats['content']}")
    print(f"  chat-kept        : {stats['chat']}")
    print(f"  identity-fallback: {stats['identity']}  ← residual (thin content; review)")
    print(f"  noise→external   : {stats['noise']}")
    print(f"  unresolved       : {stats['unresolved']}")
    print(f"  rows changed     : {stats['changed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

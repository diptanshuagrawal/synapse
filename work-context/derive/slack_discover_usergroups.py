#!/usr/bin/env python3
"""
slack_discover_usergroups.py — find Slack user-groups (subteams) the owner /
team belong to that aren't yet in config/team_subteams.yaml.

Two layers (mirrors how team_subteams.yaml is consumed):

  Layer 1 — MANAGER  : owner is a member, few/no direct reports present.
                       Written with `owner_member: true` → a ping here counts
                       as an ask to the owner (standup_gather.owner_subteam_ids).
  Layer 2 — TEAM     : >=N direct reports are members (represents the team).
                       Written WITHOUT owner_member → feeds the team-involved
                       ingest filter only (is_team_involved subteam ping).

Signal: one `usergroups.list(include_users=true)` call returns every group plus
its member UIDs. We intersect each group's members with the owner UID and the
team roster (owner + direct reports, from slack_team.load_team_slack_ids).

Why propose-only on cron: manager-vs-team-vs-noise can't be auto-decided from
membership alone (a project working-group the owner sits in looks like a manager
group; a 80-person @engineering looks like a team group). So discovery only
PROPOSES; the owner applies the layers explicitly.

Usage:
    # propose (default) — print buckets + write state/last_slack_discover_usergroups.json
    python -m derive.slack_discover_usergroups
    python -m derive.slack_discover_usergroups --json-out state/last_slack_discover_usergroups.json

    # apply (owner-driven, explicit IDs) — append to config/team_subteams.yaml
    # (pass the real subteam ids; placeholders shown here)
    python -m derive.slack_discover_usergroups --apply-manager <subteam-id> [<subteam-id> ...]
    python -m derive.slack_discover_usergroups --apply-team    <subteam-id> [<subteam-id> ...]

    # silence noise so it stops re-surfacing (e.g. org-wide @engineering)
    python -m derive.slack_discover_usergroups --skip <subteam-id>

Append-only: existing rows + their inline comments are never rewritten. Already
-configured and skiplisted groups are excluded from proposals.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.slack_api_client import SlackClient  # noqa: E402
from derive.slack_team import (  # noqa: E402
    load_team_slack_ids as _load_team_slack_ids,
    load_owner_slack_id as _load_owner_slack_id,
)

SUBTEAMS_YAML = _REPO_ROOT / "config" / "team_subteams.yaml"
SKIP_FILE = _REPO_ROOT / "state" / "slack_usergroups_skip.txt"
DEFAULT_JSON_OUT = _REPO_ROOT / "state" / "last_slack_discover_usergroups.json"

# >= this many direct reports in a group => team-layer candidate.
REPORTS_MIN_FOR_TEAM = 2
# Groups bigger than this with the owner but ~no reports are almost always
# org-wide firehoses (e.g. @engineering); tag them low-confidence.
BROAD_GROUP_SIZE = 40


# ── config + skiplist I/O ────────────────────────────────────────────────────


def _load_existing() -> dict[str, bool]:
    """Returns {subteam_id: owner_member_bool} already in team_subteams.yaml."""
    if not SUBTEAMS_YAML.exists():
        return {}
    data = yaml.safe_load(SUBTEAMS_YAML.read_text()) or {}
    out: dict[str, bool] = {}
    for s in data.get("subteams", []) or []:
        sid = s.get("id")
        if sid:
            out[sid] = bool(s.get("owner_member"))
    return out


def _load_skiplist() -> set[str]:
    if not SKIP_FILE.exists():
        return set()
    out = set()
    for line in SKIP_FILE.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def _add_skips(ids: list[str]) -> None:
    SKIP_FILE.parent.mkdir(parents=True, exist_ok=True)
    have = _load_skiplist()
    new = [i for i in ids if i not in have]
    if not new:
        print(f"[skip] all {len(ids)} id(s) already skiplisted — nothing to do.")
        return
    with SKIP_FILE.open("a") as f:
        if not SKIP_FILE.exists() or SKIP_FILE.stat().st_size == 0:
            f.write("# Slack user-group IDs suppressed from usergroup discovery.\n")
        for i in new:
            f.write(f"{i}\n")
    print(f"[skip] added {len(new)} id(s) to {SKIP_FILE.name}: {', '.join(new)}")


# ── scan + classify ──────────────────────────────────────────────────────────


def _fetch_groups(client: SlackClient) -> list[dict]:
    r = client._call(
        "usergroups.list", {"include_users": "true", "include_disabled": "true"}
    )
    return r.get("usergroups", []) or []


def classify(groups: list[dict], owner: str, team: dict[str, str],
             existing: dict[str, bool], skiplist: set[str]) -> dict:
    """Bucket every relevant group into manager / team / ambiguous / configured."""
    reports = {uid for uid in team if uid != owner}
    out = {"manager": [], "team": [], "ambiguous": [], "configured": []}
    for g in groups:
        sid = g.get("id", "")
        if not sid.startswith("S"):
            continue
        members = set(g.get("users", []) or [])
        if not members:
            continue
        owner_in = owner in members
        reps_in = sorted(reports & members)
        n_reps = len(reps_in)
        if not owner_in and n_reps == 0:
            continue  # no connection to owner or team
        rec = {
            "id": sid,
            "handle": g.get("handle") or g.get("name") or sid,
            "name": g.get("name", ""),
            "size": len(members),
            "owner_in": owner_in,
            "reports": n_reps,
            "report_names": [team[u] for u in reps_in],
            "broad": len(members) > BROAD_GROUP_SIZE,
        }
        if sid in existing:
            rec["layer"] = "manager" if existing[sid] else "team"
            out["configured"].append(rec)
            continue
        if sid in skiplist:
            continue
        team_like = n_reps >= REPORTS_MIN_FOR_TEAM
        if owner_in and team_like:
            out["ambiguous"].append(rec)
        elif owner_in:
            out["manager"].append(rec)
        elif team_like:
            out["team"].append(rec)
    for k in out:
        out[k].sort(key=lambda r: (-r["reports"], r["handle"]))
    return out


# ── apply (append-only) ──────────────────────────────────────────────────────


def _append_entries(picks: list[dict], owner_member: bool, today: str) -> None:
    """Append new subteam rows to team_subteams.yaml. Preserves existing text."""
    layer = "MANAGER" if owner_member else "TEAM"
    lines = ["", f"  # ── auto-discovered {today} ({layer} layer) ──"]
    for p in picks:
        lines.append(f"  - id: {p['id']}")
        lines.append(f"    handle: {p['handle']}")
        if owner_member:
            lines.append("    owner_member: true")
        rep = f", reports={p['reports']}" if p.get("reports") else ""
        note = (f"Auto-discovered {today} via usergroups.list. "
                f"{p.get('name') or p['handle']} "
                f"(size={p['size']}, owner_in={p['owner_in']}{rep}).")
        lines.append(f'    notes: "{note}"')
    with SUBTEAMS_YAML.open("a") as f:
        f.write("\n".join(lines) + "\n")


def do_apply(client: SlackClient, ids: list[str], owner_member: bool) -> int:
    """Resolve ids → fresh group metadata, append rows not already present."""
    owner = _load_owner_slack_id()
    team = _load_team_slack_ids()
    existing = _load_existing()
    groups = {g["id"]: g for g in _fetch_groups(client) if g.get("id")}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    reports = {uid for uid in team if uid != owner}

    picks, missing, dup = [], [], []
    for sid in ids:
        if sid in existing:
            dup.append(sid)
            continue
        g = groups.get(sid)
        if not g:
            missing.append(sid)
            continue
        members = set(g.get("users", []) or [])
        picks.append({
            "id": sid,
            "handle": g.get("handle") or g.get("name") or sid,
            "name": g.get("name", ""),
            "size": len(members),
            "owner_in": owner in members,
            "reports": len(reports & members),
        })

    if missing:
        print(f"[warn] not found in usergroups.list (ignored): {', '.join(missing)}")
    if dup:
        print(f"[warn] already in team_subteams.yaml (skipped): {', '.join(dup)}")
    if not picks:
        print("[apply] nothing new to append.")
        return 1

    layer = "manager (owner_member: true)" if owner_member else "team (ingest filter)"
    _append_entries(picks, owner_member, today)
    print(f"[apply] appended {len(picks)} {layer} subteam(s) to {SUBTEAMS_YAML.name}:")
    for p in picks:
        print(f"        + {p['id']}  @{p['handle']}  (size={p['size']}, reports={p['reports']})")
    print("\nVerify: python -c \"from derive.slack_team import load_team_subteam_ids as f; print(len(f()))\"")
    return 0


# ── report ───────────────────────────────────────────────────────────────────


def _print_report(buckets: dict, owner: str, team: dict) -> None:
    def row(x):
        rn = ", ".join(x["report_names"][:4]) + ("…" if len(x["report_names"]) > 4 else "")
        flags = []
        if x["owner_in"]:
            flags.append("owner")
        if x["reports"]:
            flags.append(f"{x['reports']} reports [{rn}]")
        if x.get("broad"):
            flags.append("BROAD/likely-skip")
        return f"  {x['id']:<13} @{x['handle']:<34} size={x['size']:<3}  {'; '.join(flags)}"

    print(f"owner={owner}  team_roster={len(team)} (owner + {len(team) - 1} reports)")
    print("=" * 80)
    print(f"\n### LAYER 1 — MANAGER  (apply: --apply-manager <ids>)   [{len(buckets['manager'])}]")
    for x in buckets["manager"]:
        print(row(x))
    print(f"\n### LAYER 2 — TEAM     (apply: --apply-team <ids>)      [{len(buckets['team'])}]")
    for x in buckets["team"]:
        print(row(x))
    print(f"\n### AMBIGUOUS — owner + >={REPORTS_MIN_FOR_TEAM} reports (you pick the layer)   [{len(buckets['ambiguous'])}]")
    for x in buckets["ambiguous"]:
        print(row(x))
    print(f"\n### ALREADY CONFIGURED (no change)   [{len(buckets['configured'])}]")
    for x in sorted(buckets["configured"], key=lambda r: r["handle"]):
        print(f"  {x['id']:<13} @{x['handle']:<34} [{x['layer']}]")


def _discover_channel() -> str:
    """Relay card channel for discovered user-groups (config-driven; empty if unset)."""
    try:
        from derive.sources_config import usergroup_discover_channel
        return usergroup_discover_channel()
    except Exception:
        return ""


def _write_json(buckets: dict, owner: str, team: dict, path: Path) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "owner": owner,
        "team_roster": len(team),
        "reports_min_for_team": REPORTS_MIN_FOR_TEAM,
        "channel": _discover_channel(),
        "manager": buckets["manager"],
        "team": buckets["team"],
        "ambiguous": buckets["ambiguous"],
        "configured": buckets["configured"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


# ── main ─────────────────────────────────────────────────────────────────────


def _refresh_proposal(client: SlackClient, out_path: Path = None,
                      print_report: bool = False) -> Path:
    """Re-classify against current config + skiplist and rewrite the proposal JSON.
    Single source the dashboards/cron-status read — must be refreshed after any
    apply/skip so a resolved group leaves the pending view immediately."""
    owner = _load_owner_slack_id()
    team = _load_team_slack_ids()
    if not owner:
        print("[fatal] owner Slack id not resolvable (people.yaml / OWNER_EMAIL).")
        return None
    groups = _fetch_groups(client)
    buckets = classify(groups, owner, team, _load_existing(), _load_skiplist())
    if print_report:
        _print_report(buckets, owner, team)
    path = out_path or DEFAULT_JSON_OUT
    if not path.is_absolute():
        path = _REPO_ROOT / path
    _write_json(buckets, owner, team, path)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply-manager", nargs="+", metavar="SID",
                    help="append these subteam ids with owner_member: true")
    ap.add_argument("--apply-team", nargs="+", metavar="SID",
                    help="append these subteam ids without owner_member (ingest filter)")
    ap.add_argument("--skip", nargs="+", metavar="SID",
                    help="suppress these ids from future proposals")
    ap.add_argument("--json-out", metavar="PATH",
                    help="write proposal JSON here (default state/last_slack_discover_usergroups.json)")
    args = ap.parse_args()

    if args.skip:
        _add_skips(args.skip)

    client = SlackClient()

    mutated = bool(args.skip)
    rc = 0
    if args.apply_manager:
        rc |= do_apply(client, args.apply_manager, owner_member=True)
        mutated = True
    if args.apply_team:
        rc |= do_apply(client, args.apply_team, owner_member=False)
        mutated = True

    if mutated:
        # Refresh the proposal JSON so the dashboard + cron-status drop the
        # resolved group right away (not just on the next cron fire). Confirmation
        # goes to stderr so the apply/skip line stays the last stdout line (Relay
        # uses that as the button's thread note).
        p = _refresh_proposal(client)
        if p:
            print(f"[proposal] refreshed → {p}", file=sys.stderr)
        return rc

    # pure propose
    out_path = Path(args.json_out) if args.json_out else None
    p = _refresh_proposal(client, out_path=out_path, print_report=True)
    if p is None:
        return 2
    print(f"\n[proposal] written to {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

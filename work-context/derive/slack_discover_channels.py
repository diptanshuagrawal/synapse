#!/usr/bin/env python3
"""
slack_discover_channels.py — find active team channels not yet in ingest yaml.

Walks each team member's `users.conversations`, finds channels where ≥N team
members are present, scores by team-INVOLVED message count in last 90d (author
∈ team OR body @-mentions a team member OR body pings a team subteam handle
like @team-oncall), prints a proposal table. Optional `--apply`
appends new channels to `config/slack_channels.yaml` (MPIMs need explicit
`--include-mpim`).

Owner-presence bypass: announcement / manager / HR rooms (tech-management,
hr-tech, hr-tech-managers, …) are channels the OWNER sits in but few/no direct
reports do, so they never reach min_team and team-involvement scoring is ~0 —
yet the announcements themselves are the signal. When the owner is a member and
the name reads as an announcement channel (_is_announcement_like), the channel
bypasses the team + activity floors and is auto-added as ingest_mode=full. A
bot-ratio guard keeps a bot firehose the owner merely lurks in from sneaking in.

Usage:
    python -m derive.slack_discover_channels                  # report only
    python -m derive.slack_discover_channels --apply          # report + write yaml
    python -m derive.slack_discover_channels --days 30 --min-team 4
    python -m derive.slack_discover_channels --include-mpim   # MPIM consent
    python -m derive.slack_discover_channels --top 30         # show more candidates

After --apply: run `python ingest/slack_backfill_app.py <new-channel>` for each
added channel to seed its cursor before the next cron fire picks it up.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ingest.slack_api_client import SlackClient  # noqa: E402
from derive.sources_config import org_match_tokens  # noqa: E402

CHANNELS_YAML = _REPO_ROOT / "config" / "slack_channels.yaml"

from derive.slack_team import (  # noqa: E402
    load_team_slack_ids as _load_team_slack_ids,
    load_team_subteam_ids as _load_team_subteam_ids,
    load_owner_slack_id as _load_owner_slack_id,
    is_team_involved as _is_team_involved,
)


def _load_yaml_channel_ids() -> set[str]:
    """Returns set of channel-ids already in slack_channels.yaml."""
    with CHANNELS_YAML.open() as f:
        cfg = yaml.safe_load(f)
    out = set()
    for c in cfg.get("channels", []):
        cid = c.get("id")
        if cid and cid != "TODO":
            out.add(cid)
    return out


def _load_yaml_channels_full() -> list[dict]:
    """Every channel entry in slack_channels.yaml with id/name/class — the
    decision input for the prune pass."""
    with CHANNELS_YAML.open() as f:
        cfg = yaml.safe_load(f)
    out = []
    for c in cfg.get("channels", []):
        cid = c.get("id")
        if cid and cid != "TODO":
            out.append({"id": cid, "name": c.get("name", cid),
                        "class": c.get("class", ""),
                        "ingest_mode": c.get("ingest_mode", "")})
    return out


def _channel_active(conn, cid: str, days: int) -> bool:
    """True if the channel has ≥1 slack message of ANY author (incl. bots) in
    the last `days`. Deliberately NOT team-involvement: alert/bot channels carry
    real value with zero team-authored messages, so 'stale' must mean 'no
    traffic at all' — otherwise the prune would delete live alert feeds. A
    dead, auto-discovered channel is re-added by the additive discovery pass if
    it ever revives, so this prune is self-healing."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = conn.execute(
        "SELECT 1 FROM events WHERE source='slack' AND channel_id=? AND ts >= ? LIMIT 1",
        (cid, since),
    ).fetchone()
    return n is not None


def _slack_last_msg_age_days(client, cid: str) -> float | None:
    """Age (days) of the channel's most recent Slack message, via
    conversations.history. None if the call fails. -1 sentinel if the channel
    has no messages at all (treat as very old)."""
    try:
        r = client._call("conversations.history", {"channel": cid, "limit": 1})
    except Exception:
        return None
    msgs = r.get("messages", [])
    if not msgs:
        return -1.0
    try:
        ts = float(msgs[0]["ts"])
    except (KeyError, ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc).timestamp() - ts) / 86400.0


def _find_prunable(channels: list[dict], client, conn, stale_days: int):
    """Decide what to remove from yaml. Returns (removable, ingest_gaps).

    `events.db` is the right liveness signal because the ingest ALREADY applies
    each channel's mode filter when storing — so 0-rows-in-window means "nothing
    we keep for this channel happened":
      - team_involved channel, 0 rows → no team-involved traffic → dead to us → prune.
      - full channel, 0 rows → either truly dead OR a storage bug. We disambiguate
        against Slack: if Slack shows a recent message the ingest SHOULD have
        stored (full mode) but didn't, that's an INGEST GAP — flag, don't prune.

      - ARCHIVED (any class) → removable.
      - Hand-curated channels are never pruned for staleness; only archived.
    On any ambiguous Slack error the channel is KEPT (conservative)."""
    removable, ingest_gaps = [], []
    for ch in channels:
        cid, name, klass, mode = ch["id"], ch["name"], ch["class"], ch.get("ingest_mode", "")
        archived = None
        try:
            info = client.conversations_info(cid)
            archived = bool(info.get("channel", {}).get("is_archived"))
        except RuntimeError as e:
            # channel_not_found / is_archived-only-readable → treat as gone.
            archived = True if ("not_found" in str(e) or "archived" in str(e)) else None
        except Exception:
            archived = None  # network/other → don't risk a removal

        if archived:
            removable.append({"id": cid, "name": name, "class": klass, "reason": "archived"})
            continue
        if archived is None:
            continue  # couldn't verify — keep

        # Stale check — auto-discovered only.
        if klass != "auto-discovered":
            continue
        if _channel_active(conn, cid, stale_days):
            continue  # we stored traffic in-window → live (mode-appropriately)

        # 0 stored in-window. For `full` channels that should mean truly empty —
        # confirm against Slack to separate dead from a storage bug.
        if mode == "full":
            age = _slack_last_msg_age_days(client, cid)
            if age is None:
                continue  # couldn't verify — keep
            if 0 <= age <= stale_days:
                ingest_gaps.append({"id": cid, "name": name, "class": klass,
                                    "slack_age_days": round(age, 1),
                                    "reason": f"full-mode but live in Slack ({age:.0f}d), "
                                              "absent from DB — storage gap"})
            else:
                removable.append({"id": cid, "name": name, "class": klass,
                                  "reason": ("stale: Slack idle "
                                             + ("(no messages)" if age < 0 else f"{age:.0f}d"))})
        else:
            # team_involved (or other filtered mode): 0 stored = no traffic we
            # keep. Re-added by the additive pass if team involvement returns.
            removable.append({"id": cid, "name": name, "class": klass,
                              "reason": f"stale: no team-involved traffic in {stale_days}d"})
    return removable, ingest_gaps


def _remove_channels_from_yaml(ids: set[str]) -> int:
    """Surgically delete the block for each id from slack_channels.yaml,
    preserving all other formatting + comments. A block is the `- id: <cid>`
    line plus its indented body lines (and any blank/comment lines immediately
    above it that belong to it). Returns the count removed."""
    lines = CHANNELS_YAML.read_text().splitlines(keepends=True)
    keep: list[str] = []
    i = 0
    removed = 0
    while i < len(lines):
        ln = lines[i]
        stripped = ln.strip()
        m = stripped.startswith("- id:")
        cid = stripped.split("- id:", 1)[1].strip() if m else None
        if m and cid in ids:
            # Drop this list item: the `- id:` line + all following lines more
            # indented than the dash (the field body), until the next list item
            # / comment header / dedent.
            dash_indent = len(ln) - len(ln.lstrip())
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if not nxt.strip():           # blank line inside block → drop
                    i += 1
                    continue
                nxt_indent = len(nxt) - len(nxt.lstrip())
                if nxt_indent <= dash_indent:  # next item / comment / dedent
                    break
                i += 1
            removed += 1
            continue
        keep.append(ln)
        i += 1
    if removed:
        CHANNELS_YAML.write_text("".join(keep))
    return removed


EXCLUDE_YAML = _REPO_ROOT / "config" / "discover_exclude.yaml"


def _load_excluded() -> tuple[set[str], set[str]]:
    """Owner's permanent discovery denylist → (excluded_ids, excluded_names).

    Channels here are never proposed/applied even if they score above the
    floor (e.g. an alert-channel name that matches the team-domain rule but
    the owner doesn't want). Matched by id OR lowercase name. Missing file →
    empty sets (no exclusions).
    """
    ids: set[str] = set()
    names: set[str] = set()
    if not EXCLUDE_YAML.exists():
        return ids, names
    with EXCLUDE_YAML.open() as f:
        cfg = yaml.safe_load(f) or {}
    for e in cfg.get("exclude", []) or []:
        if e.get("id"):
            ids.add(e["id"])
        if e.get("name"):
            names.add(str(e["name"]).strip().lower())
    return ids, names


def _slugify(name: str) -> str:
    """Slack channel name → safe yaml slug. Names already lowercase-ish."""
    return name.strip().lower().replace(" ", "-")[:80]


def _channel_kind(meta: dict) -> str:
    if meta.get("is_im"):
        return "DM"
    if meta.get("is_mpim"):
        return "MPIM"
    if meta.get("is_private"):
        return "private"
    return "public"


def _mpim_team_count(name: str, team_slack_handles: set[str]) -> int:
    """Parse mpdm-foo--bar--baz-1 name → count team handles among participants.

    Slack MPIM names follow `mpdm-<handle>--<handle>--<handle>-N` format.
    Handles may be truncated by Slack (e.g. "bobx" for "bob.example") when
    name exceeds 80 chars — use prefix match to recover those cases.
    """
    if not name.startswith("mpdm-"):
        return 0
    body = name[len("mpdm-"):]
    # Strip trailing "-N" iteration suffix
    if "-" in body and body.rsplit("-", 1)[1].isdigit():
        body = body.rsplit("-", 1)[0]
    parts = [p for p in body.split("--") if p]
    count = 0
    for p in parts:
        if p in team_slack_handles:
            count += 1
        elif len(p) >= 3 and any(h.startswith(p) for h in team_slack_handles):
            count += 1  # handle truncated by Slack
    return count


# Default mode decision tree — applied when --auto-mode is set.
MPIM_TEAM_THRESHOLD = 3                 # team handles in MPIM → auto-add as full
TEAM_RATIO_FULL_THRESHOLD = 0.5         # team_msgs/total_msgs ≥ this → full mode
BOT_NAME_PREFIXES = ("opsgenie-", "alert-", "pagerduty-", "datadog-",
                     "github-", "sentry-", "jenkins-")
ANNOUNCE_NAME_PATTERNS = {
    "general", "announcements", "tech", "product_announcements",
    "all-hands", "all-engineering", "company-announcements",
}

# Owner-present announcement / manager / leadership / HR / EM rooms (e.g.
# tech-management, hr-tech, hr-tech-managers, jayanth-ems, eng-leadership).
# The owner sits in these but few/no direct reports do, so they never reach
# min_team and team-involvement scoring is ~0 — yet they ARE owner-context
# signal. Whole-token match (not raw substring) to avoid mis-fires (e.g. "hr"
# inside "chrome", "em" inside "team"). Lurk firehoses (vendor / liabilities /
# merchant) carry none of these tokens, so they stay out — plus the bot-ratio
# guard in _decide_mode drops any that are bot-dominated.
#
# STRONG tokens unambiguously name a PEOPLE room → match on name alone.
STRONG_OWNER_TOKENS = (
    "hr", "leadership", "leader", "leaders",
    "managers", "manager", "mgrs", "mgr",
    "em", "ems", "townhall", "town", "broadcast",
)
STRONG_OWNER_PREFIXES = ("announce",)   # announcement / announcements

# AMBIGUOUS: "management"/"mgmt" mean PEOPLE in "tech-management" but a TOPIC in
# "vulns-mgmt" / "cost-management" / "incident-management". Count as a people
# room ONLY when (a) the channel is not a wg-/tmp- working group, AND (b) the
# token immediately before it is not a domain/topic noun. This keeps
# tech-management / tech-mgmt while dropping topic-management channels.
AMBIGUOUS_MGMT_TOKENS = ("management", "mgmt")
WORKING_GROUP_PREFIXES = ("wg", "tmp")
MGMT_TOPIC_NOUNS = frozenset({
    "vuln", "vulns", "vulnerability", "vulnerabilities", "incident", "incidents",
    "cost", "costs", "risk", "risks", "change", "release", "releases", "config",
    "configuration", "vendor", "vendors", "capacity", "access", "identity",
    "fleet", "asset", "assets", "data", "knowledge", "secret", "secrets",
    "key", "keys", "patch", "patches", "device", "devices", "project",
    "projects", "inventory", "order", "orders", "fraud", "content", "case",
})


def _is_announcement_like(name: str) -> bool:
    """True if a channel name reads as an announcement / manager / leadership /
    HR / EM room.

    Used only for the owner-presence bypass — broader than ANNOUNCE_NAME_PATTERNS
    (which gates org-wide noisy channels toward team_involved). Ambiguous
    management/mgmt tokens are disambiguated by the preceding token so that
    "tech-management" (people) matches but "vulns-mgmt" (topic) does not."""
    n = name.lower()
    if n in ANNOUNCE_NAME_PATTERNS or n.startswith(("announce", "all-hands")):
        return True
    tokens = n.replace("_", "-").split("-")
    is_wg = bool(tokens) and tokens[0] in WORKING_GROUP_PREFIXES
    for i, t in enumerate(tokens):
        if t in STRONG_OWNER_TOKENS or t.startswith(STRONG_OWNER_PREFIXES):
            return True
        if t in AMBIGUOUS_MGMT_TOKENS and not is_wg:
            prev = tokens[i - 1] if i > 0 else ""
            if prev not in MGMT_TOPIC_NOUNS:
                return True
    return False

# ── Alert-channel detection ──────────────────────────────────────────────────
# Alert/monitoring channels are bot-authored firehoses — the team is a member
# and the alerts pertain to team-owned systems, but the team rarely posts or
# @-mentions, so author/mention scoring scores them ~0 and the activity floor
# drops them. For these we want ingest_mode=full (the bot alert IS the signal).
#
# Gate = looks-like-alert-channel AND name carries a team-DOMAIN keyword. The
# domain keyword is what separates the team's own alert streams (accounting /
# ledger-balance / service-c / txn / service-a) from other-pod firehoses that
# the owner merely lurks in (vendor / instant-pay / liabilities / merchant).
# deposits/withholding are NOT this team's domain so deliberately excluded.
ALERT_NAME_TOKENS = ("alert", "tracker", "opsgenie", "sentry", "pagerduty",
                     "notifications", "-logs")
ALERT_BOT_RATIO = 0.8                   # ≥80% bot-authored ⇒ treat as alert stream
# Single-token domain keywords: matched against whole `-`/`_`-split tokens
# (with startswith for plurals/prefixes like txn→txns). NOT raw substring —
# that mis-fired on "gl" inside "breakGLass". "gl" (general ledger) dropped
# entirely; GL channels also carry accounting/recon/ledger-balance.
ALERT_DOMAIN_TOKENS = [
    "accounting", "recon", "transaction", "transactions",
    "txn", "pending_txn", "account-freeze",
] + org_match_tokens()
# Compound keywords: matched as substring on the normalized (`-`→`_`) name.
ALERT_DOMAIN_COMPOUND = ("ledger_balance", "pending_txn")


def _is_alert_channel(name: str, bot_ratio: float) -> bool:
    """Alert/monitoring firehose: name token match OR bot-dominated authorship."""
    n = name.lower()
    if bot_ratio >= ALERT_BOT_RATIO:
        return True
    return any(tok in n for tok in ALERT_NAME_TOKENS)


def _name_has_team_domain(name: str) -> bool:
    """Team-domain match: whole-token (with prefix) OR compound substring.

    Token-aware to avoid substring mis-fires (e.g. "gl" inside "breakglass").
    """
    norm = name.lower().replace("-", "_")
    tokens = norm.split("_")
    for kw in ALERT_DOMAIN_COMPOUND:
        if kw in norm:
            return True
    for kw in ALERT_DOMAIN_TOKENS:
        if any(t == kw or t.startswith(kw) for t in tokens):
            return True
    return False


def _decide_mode(meta: dict, team_set: set[str], team_msgs: int,
                 total_msgs: int, mpim_team_count: int,
                 min_team_msgs: int = 5,
                 min_mpim_msgs: int = 1,
                 bot_ratio: float = 0.0,
                 owner_present: bool = False,
                 below_team_floor: bool = False) -> tuple[str, dict]:
    """Returns (verdict, extras) where verdict ∈ {auto_full, auto_team_involved,
    needs_review, skip}. extras carries mode+allow_mpim+rationale for the row.
    """
    name = meta.get("name", "")
    is_mpim = bool(meta.get("is_mpim"))

    # Hard skip
    if meta.get("is_im") or meta.get("is_archived"):
        return "skip", {"reason": "is_im or archived"}

    # Team-owned alert channel — bypass the activity floor. Bot alerts about
    # team systems (e.g. accounting_alerts, ledger_balance_tracker) carry
    # near-zero team authorship but ARE the signal. Capture in full mode
    # (team_involved would drop every bot-authored alert). Non-MPIM only.
    if not is_mpim and _is_alert_channel(name, bot_ratio) and _name_has_team_domain(name):
        return "auto_full", {
            "mode": "full",
            # Skip per-fire reply-thread reconcile: alert replies are bot
            # acks/status noise, and re-fetching them every fire dominates
            # ingest wall-time. Top-level alerts + edit/delete reconcile stay.
            "no_threads": True,
            "rationale": f"team-domain alert channel ({bot_ratio:.0%} bot · {total_msgs} msgs/90d)",
        }

    # Owner-present announcement / manager / HR channel — bypass BOTH the team
    # floor (admitted in the candidate filter) and the activity floor here. The
    # owner sits in these rooms but few/no direct reports do, so team-involvement
    # scoring is ~0 — yet the announcements themselves are the signal. Capture in
    # full. Bot-ratio guard keeps a bot firehose the owner merely lurks in from
    # masquerading as an announcement channel.
    #   `below_team_floor` gate is load-bearing: WITHOUT it, big org-wide rooms
    #   the owner also sits in (general, tech, data-announcements) would flip
    #   from team_involved → full. Those have ample team presence, so they fall
    #   through to the normal tree below. Only the would-be-dropped rooms bypass.
    if (not is_mpim and owner_present and below_team_floor
            and _is_announcement_like(name) and bot_ratio < ALERT_BOT_RATIO):
        return "auto_full", {
            "mode": "full",
            "owner_bypass": True,
            "rationale": f"owner-present announcement channel "
                         f"({total_msgs} msgs/90d · {bot_ratio:.0%} bot)",
        }

    # Dead channel — zero messages of ANY author in the window means a defunct
    # group DM or channel. Skip (not needs_review) so it stops bloating the
    # review list: 0-msg MPIMs were ~44% of needs_review and pile up as new
    # group DMs / channels get created. Self-healing — if it goes active later,
    # the next run scores total_msgs>0 and it re-surfaces. Placed AFTER the
    # alert + owner-announcement bypasses so those still capture quiet channels
    # on name alone.
    if total_msgs == 0:
        return "skip", {"reason": f"dead {'MPIM' if is_mpim else 'channel'} (0 msgs/90d)"}

    # Team-silent — active channel (total_msgs>0, already past the dead check)
    # but ZERO team involvement in the window. Adding it in team_involved mode
    # would ingest nothing; full would be pure noise — so it is not actionable.
    # Bucketed separately from needs_review (which keeps channels with SOME team
    # signal) so the review list stays about channels worth deciding on. The
    # owner can still hand-promote one to full from the dashboard's team-silent
    # section. Self-healing — team posts → team_msgs>0 → leaves this bucket.
    # Reached only after the alert + owner-announcement bypasses, so team-domain
    # alerts and owner rooms are already captured on name alone.
    if team_msgs == 0:
        return "team_silent", {
            "mode": "team_involved",
            "allow_mpim": is_mpim,
            "rationale": f"active but 0 team msgs/90d ({total_msgs} total)",
        }

    # Universal activity floor — applied BEFORE other checks. MPIMs allowed
    # a lower threshold since they're inherently bounded (≤9 members).
    floor = min_mpim_msgs if is_mpim else min_team_msgs
    if team_msgs < floor:
        return "needs_review", {
            "mode": "full" if is_mpim else "team_involved",
            "allow_mpim": is_mpim,
            "rationale": f"below activity floor ({team_msgs}<{floor} team msgs/90d)",
        }

    # MPIM rule — auto-add as full if ≥3 team handles in mpdm-name
    if is_mpim:
        if mpim_team_count >= MPIM_TEAM_THRESHOLD:
            return "auto_full", {
                "mode": "full", "allow_mpim": True,
                "rationale": f"MPIM with {mpim_team_count} team handles · {team_msgs} msgs/90d",
            }
        return "needs_review", {
            "mode": "full", "allow_mpim": True,
            "rationale": f"MPIM with {mpim_team_count} team handles — below threshold",
        }

    # Bot/alert name patterns → team_involved
    if any(name.startswith(p) for p in BOT_NAME_PREFIXES):
        return "auto_team_involved", {
            "mode": "team_involved",
            "rationale": f"bot-channel name pattern ({name[:20]}…)",
        }

    # Announcement-style names → team_involved
    if name in ANNOUNCE_NAME_PATTERNS or name.startswith("announce") \
            or name.startswith("all-"):
        return "auto_team_involved", {
            "mode": "team_involved",
            "rationale": "announcement-channel name pattern",
        }

    # High team signal/noise ratio → full
    ratio = team_msgs / total_msgs if total_msgs > 0 else 0
    if ratio >= TEAM_RATIO_FULL_THRESHOLD:
        return "auto_full", {
            "mode": "full",
            "rationale": f"high team ratio {ratio:.0%} ({team_msgs}/{total_msgs})",
        }

    # Low ratio + active → team_involved
    if total_msgs >= 20:
        return "auto_team_involved", {
            "mode": "team_involved",
            "rationale": f"low team ratio {ratio:.0%} on active channel ({total_msgs} msgs)",
        }

    # Quiet + low signal — defer
    return "needs_review", {
        "mode": "team_involved",
        "rationale": f"insufficient signal ({total_msgs} msgs, {ratio:.0%} team)",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90,
                    help="activity window for team-msg count (default 90)")
    ap.add_argument("--min-team", type=int, default=3,
                    help="minimum team members in channel to be a candidate (default 3)")
    ap.add_argument("--top", type=int, default=20,
                    help="cap shown candidates (default 20, sorted by team-msg desc)")
    ap.add_argument("--include-mpim", action="store_true",
                    help="legacy: include ALL MPIM channels regardless of team count "
                         "(default applies MPIM_TEAM_THRESHOLD heuristic)")
    ap.add_argument("--auto-mode", action="store_true",
                    help="apply decision-tree: pick ingest_mode + allow_mpim per channel "
                         "(full vs team_involved vs needs_review)")
    ap.add_argument("--min-team-msgs", type=int, default=5,
                    help="activity floor for non-MPIM channels — below this drops to "
                         "needs_review instead of auto-add (default 5)")
    ap.add_argument("--min-mpim-msgs", type=int, default=1,
                    help="activity floor for MPIMs — Slack auto-creates new MPIMs "
                         "often; this filters out dead ones (default 1)")
    ap.add_argument("--apply", action="store_true",
                    help="append rows to config/slack_channels.yaml")
    ap.add_argument("--prune", action="store_true",
                    help="also remove dead channels from yaml: archived (any class) "
                         "+ stale auto-discovered (no team activity in --prune-stale-days)")
    ap.add_argument("--prune-stale-days", type=int, default=45,
                    help="staleness window for pruning auto-discovered channels (default 45)")
    ap.add_argument("--prune-max-frac", type=float, default=0.30,
                    help="safety cap: abort the prune if it would remove more than this "
                         "fraction of channels (guards against a Slack-API blip; default 0.30)")
    ap.add_argument("--json-out",
                    help="write proposals as JSON to this path (for cron-status consumption)")
    args = ap.parse_args()

    team = _load_team_slack_ids()
    team_ids = set(team.keys())
    team_subteam_ids = _load_team_subteam_ids()
    owner_sid = _load_owner_slack_id()
    owner_canonical = team.get(owner_sid) if owner_sid else None
    print(f"[team] {len(team)} members with slack_id · "
          f"{len(team_subteam_ids)} team subteam(s) for mention scoring · "
          f"owner={owner_canonical or 'UNRESOLVED'}", flush=True)

    # Build team slack-username set for MPIM name-parsing.
    # MPIM names use Slack's `user.name` field (e.g. mpdm-foo.bar--baz.qux-1),
    # NOT display_name or canonical. Resolve per-team-member via users.info.
    team_slack_handles: set[str] = set()
    if args.auto_mode:
        client_init = SlackClient()
        for sid in team:
            try:
                info = client_init.users_info(sid)
                uname = info.get("user", {}).get("name", "").lower()
                if uname:
                    team_slack_handles.add(uname)
            except Exception:
                pass
        print(f"[team] {len(team_slack_handles)} slack usernames for MPIM matching",
              flush=True)

    already = _load_yaml_channel_ids()
    print(f"[yaml] {len(already)} channels already ingested", flush=True)

    excl_ids, excl_names = _load_excluded()
    if excl_ids or excl_names:
        print(f"[exclude] {len(excl_ids)} id(s) + {len(excl_names)} name(s) on owner denylist",
              flush=True)

    client = SlackClient()
    # Will hold name + meta per discovered channel
    chan_meta: dict[str, dict] = {}
    # channel_id → set of team-canonical-names present
    chan_team: dict[str, set[str]] = defaultdict(set)

    print(f"\n[discover] walking users.conversations for each team member...", flush=True)
    t0 = time.monotonic()
    for sid, canonical in team.items():
        try:
            count = 0
            for ch in client.iter_users_conversations(user_id=sid):
                cid = ch.get("id")
                if not cid:
                    continue
                chan_team[cid].add(canonical)
                # Store first-seen metadata (channels.list-shape: name, is_im, is_mpim, is_private)
                if cid not in chan_meta:
                    chan_meta[cid] = {
                        "name": ch.get("name") or ch.get("name_normalized") or cid,
                        "is_im": bool(ch.get("is_im")),
                        "is_mpim": bool(ch.get("is_mpim")),
                        "is_private": bool(ch.get("is_private")),
                        "is_archived": bool(ch.get("is_archived")),
                        "created": ch.get("created", 0),
                    }
                count += 1
            print(f"  {canonical:20s} → {count} channels", flush=True)
        except RuntimeError as e:
            print(f"  {canonical:20s} ERR  {e}", file=sys.stderr)
    print(f"[discover] {len(chan_meta)} distinct channels seen "
          f"in {time.monotonic() - t0:.1f}s\n", flush=True)

    # Filter to candidates: not in yaml, not on owner denylist, ≥min_team
    # members, not is_im, not archived.
    # Owner-present announcement/manager/HR rooms bypass the team floor — the
    # owner sits in them but reports don't, yet the announcements are the signal.
    # Final full/skip is bot-ratio-guarded in _decide_mode; the name gate here
    # keeps the bypassed candidate set small (not every channel the owner lurks).
    candidates: list[tuple[str, dict, set[str]]] = []
    n_excluded = 0
    n_owner_bypass = 0
    for cid, team_set in chan_team.items():
        if cid in already:
            continue
        meta = chan_meta[cid]
        if cid in excl_ids or str(meta["name"]).strip().lower() in excl_names:
            n_excluded += 1
            continue
        if meta["is_im"]:
            continue
        if meta["is_archived"]:
            continue
        if len(team_set) < args.min_team:
            owner_here = owner_canonical is not None and owner_canonical in team_set
            if owner_here and not meta["is_mpim"] and _is_announcement_like(meta["name"]):
                n_owner_bypass += 1
            else:
                continue
        candidates.append((cid, meta, team_set))
    print(f"[filter] {len(candidates)} candidates pass min_team={args.min_team} "
          f"+ not-in-yaml + not-excluded + not-archived "
          f"({n_excluded} dropped by denylist, "
          f"{n_owner_bypass} owner-announcement bypass)", flush=True)

    # Score by team-author messages in last `--days`
    oldest_dt = datetime.now(tz=timezone.utc) - timedelta(days=args.days)
    oldest_epoch = f"{oldest_dt.timestamp():.6f}"

    print(f"\n[score] counting team-author msgs in last {args.days}d...", flush=True)
    # scored: (team_msgs, total_msgs, bot_msgs, cid, meta, team_set)
    scored: list[tuple[int, int, int, str, dict, set[str]]] = []
    for i, (cid, meta, team_set) in enumerate(candidates, 1):
        msg_count = 0
        total_count = 0
        bot_count = 0
        try:
            # Single page covers most channels for 90d; tier-3 budget bounded
            page = client.history(cid, oldest=oldest_epoch, limit=200)
            for m in page.get("messages", []):
                total_count += 1
                if m.get("bot_id") or m.get("subtype") == "bot_message":
                    bot_count += 1
                # Team-involved = authored by team OR @-mentions a team member
                # OR pings a team subteam handle (e.g. @team-oncall).
                # Subteam-ping coverage catches oncall/incident channels where
                # the team is paged via user-group handle, not individual @UID.
                if _is_team_involved(m.get("user"), m.get("text"),
                                     team_ids, team_subteam_ids):
                    msg_count += 1
        except RuntimeError as e:
            print(f"  {meta['name']:35s} ERR  {e}", file=sys.stderr)
        scored.append((msg_count, total_count, bot_count, cid, meta, team_set))
        if i % 10 == 0:
            print(f"  scored {i}/{len(candidates)}", flush=True)
    scored.sort(key=lambda x: x[0], reverse=True)

    # ── Classify each candidate (auto-mode applies decision tree) ──
    proposals: list[dict] = []
    for team_msgs, total_msgs, bot_msgs, cid, meta, team_set in scored:
        kind = _channel_kind(meta)
        bot_ratio = bot_msgs / total_msgs if total_msgs > 0 else 0.0
        mpim_tc = _mpim_team_count(meta["name"], team_slack_handles) if args.auto_mode else 0
        if args.auto_mode:
            verdict, extras = _decide_mode(
                meta, team_set, team_msgs, total_msgs, mpim_tc,
                min_team_msgs=args.min_team_msgs,
                min_mpim_msgs=args.min_mpim_msgs,
                bot_ratio=bot_ratio,
                owner_present=(owner_canonical is not None
                               and owner_canonical in team_set),
                below_team_floor=(len(team_set) < args.min_team),
            )
        else:
            # Legacy behaviour: needs_review unless include-mpim
            if kind == "MPIM" and not args.include_mpim:
                verdict, extras = "skip", {"reason": "MPIM without --include-mpim"}
            else:
                verdict, extras = "auto_team_involved", {"mode": "team_involved"}
        proposals.append({
            "channel_id": cid,
            "name": meta["name"],
            "kind": kind,
            "team_members": len(team_set),
            "team_msgs": team_msgs,
            "total_msgs": total_msgs,
            "mpim_team_count": mpim_tc,
            "verdict": verdict,
            "mode": extras.get("mode"),
            "allow_mpim": extras.get("allow_mpim", False),
            "no_threads": extras.get("no_threads", False),
            "owner_bypass": extras.get("owner_bypass", False),
            "rationale": extras.get("rationale", extras.get("reason", "")),
        })

    auto_full = [p for p in proposals if p["verdict"] == "auto_full"]
    auto_ti = [p for p in proposals if p["verdict"] == "auto_team_involved"]
    needs_review = [p for p in proposals if p["verdict"] == "needs_review"]
    team_silent = [p for p in proposals if p["verdict"] == "team_silent"]
    skipped = [p for p in proposals if p["verdict"] == "skip"]

    # ── Report ──
    print(f"\n{'='*98}")
    print(f"  Discovered channels (top {min(args.top, len(proposals))} by team activity)")
    print(f"{'='*98}")
    print(f"  {'channel':<40}  {'team':>4}  {'team/total':>10}  {'kind':<8}  {'verdict':<22}  mode")
    print(f"  {'-'*40}  {'-'*4}  {'-'*10}  {'-'*8}  {'-'*22}  {'-'*15}")
    for p in proposals[:args.top]:
        ratio_str = f"{p['team_msgs']}/{p['total_msgs']}"
        mode_str = p["mode"] or "-"
        if p["allow_mpim"]:
            mode_str += "*"
        print(f"  {p['name'][:40]:<40}  {p['team_members']:>4}  {ratio_str:>10}  "
              f"{p['kind']:<8}  {p['verdict']:<22}  {mode_str}")

    print(f"\n[buckets] auto_full={len(auto_full)}  auto_team_involved={len(auto_ti)}  "
          f"needs_review={len(needs_review)}  team_silent={len(team_silent)}  "
          f"skipped={len(skipped)}")

    # ── Prune scan (archived + stale auto-discovered already in yaml) ──
    removable: list[dict] = []
    ingest_gaps: list[dict] = []
    if args.prune:
        from ingest.common import get_db  # noqa: E402
        yaml_channels = _load_yaml_channels_full()
        conn = get_db()
        removable, ingest_gaps = _find_prunable(yaml_channels, client, conn,
                                                args.prune_stale_days)
        conn.close()
        print(f"\n[prune] {len(removable)} removable "
              f"({sum(r['reason']=='archived' for r in removable)} archived, "
              f"{sum(r['reason']!='archived' for r in removable)} stale) of "
              f"{len(yaml_channels)} channels")
        for r in removable:
            print(f"  - {r['name'][:40]:<40}  {r['reason']}")
        if ingest_gaps:
            print(f"\n[ingest-gap] {len(ingest_gaps)} channel(s) live in Slack but "
                  "absent from our DB — NOT pruned; fix ingestion:")
            for g in ingest_gaps:
                print(f"  ! {g['name'][:40]:<40}  {g['reason']}")

    # ── JSON output (for cron-status consumption) ──
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "days": args.days,
                "auto_full": auto_full,
                "auto_team_involved": auto_ti,
                "needs_review": needs_review,
                "team_silent": team_silent,
                "skipped": skipped,
                "removed": removable,
                "ingest_gaps": ingest_gaps,
            }, f, indent=2)
        print(f"\n[json] wrote proposals to {args.json_out}")

    # ── Apply ──
    if not args.apply:
        print("\n[dry] no yaml writes. Re-run with --apply to add/prune channels.")
        return 0

    # Prune removals first (rewrites yaml), guarded by a blast-radius cap so a
    # Slack-API blip that mis-reports many channels can't wipe the config.
    if args.prune and removable:
        total = len(_load_yaml_channels_full())
        frac = len(removable) / max(1, total)
        if frac > args.prune_max_frac:
            print(f"\n[prune] ABORTED — would remove {len(removable)}/{total} "
                  f"({frac:.0%} > {args.prune_max_frac:.0%} cap). Not pruning; "
                  "investigate (Slack API blip?). Add candidates are unaffected.")
        else:
            n = _remove_channels_from_yaml({r["id"] for r in removable})
            print(f"[prune] removed {n} channel(s) from config/slack_channels.yaml")

    to_add = auto_full + auto_ti
    if not to_add:
        print("\n[apply] nothing in auto_full + auto_team_involved buckets.")
        return 0

    with CHANNELS_YAML.open("a") as f:
        f.write("\n  # ─── auto-discovered " + datetime.now(timezone.utc).date().isoformat()
                + " ───\n")
        for p in to_add:
            slug = _slugify(p["name"])
            f.write(f"\n  - id: {p['channel_id']}\n")
            f.write(f"    name: {slug}\n")
            f.write(f"    class: auto-discovered\n")
            f.write(f"    compaction_policy: standard\n")
            f.write(f"    ingest_mode: {p['mode']}\n")
            if p.get("allow_mpim"):
                f.write(f"    allow_mpim: true\n")
            if p.get("no_threads"):
                f.write(f"    no_threads: true\n")
            f.write(f"    notes: \"auto-discovered {datetime.now(timezone.utc).date().isoformat()}: "
                    f"{p['team_members']}t · {p['team_msgs']}/{p['total_msgs']} msgs/{args.days}d · {p['rationale']}\"\n")

    print(f"\n[apply] appended {len(to_add)} rows to config/slack_channels.yaml")
    print(f"[apply] {len(needs_review)} channels in needs_review bucket — not auto-applied. "
          "Review state cache or use --include-mpim to override.")
    print("\nNext cron fire auto-bootstraps each new channel from 365d.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
retro_census.py — coverage-guaranteed census for retros.

The retro recall problem: synthesising from curated feeds (clusters, MoM
bullets) silently misses anything not in those feeds (e.g. a 100% go-live
buried in a 73-reply "Prod Readiness" thread; on-call incident-alert
threads that got auto-stubbed as recurring noise).

This module fixes recall by CONSTRUCTION:

  1. Denominator = EVERY subject with ≥1 event in the window (census, not
     a sample of feeds).
  2. Exhaustive deterministic partition — each subject lands in exactly one
     (ownership × signal-type) bucket via routing detectors. Detectors only
     ROUTE; they never gate inclusion. A detector miss lands in `discussion`
     or `unclassified`, never silent loss.
  3. Coverage proof — `represented + excluded_noise + unclassified == total`.
     `unclassified` MUST be 0; if not, the subjects are listed so the gap is
     visible, not hidden.
  4. Incident + rollout sub-censuses — full enumeration with evidence URLs,
     so a retro can reconcile "incidents in window" vs "incidents in retro".

The LLM's job shrinks to JUDGEMENT WITHIN buckets (phrasing, impact, merge).
Discovery is this census: deterministic, complete, auditable.

Usage:
    .venv/bin/python derive/retro_census.py \\
        --since 2026-05-01T00:00:00Z --until 2026-05-28T23:59:59Z \\
        > /tmp/retro_census.json
    .venv/bin/python derive/retro_census.py --since ... --until ... --format summary
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
from derive.sources_config import home_team, slack_workspace, github_org, atlassian_host, org_match_tokens  # noqa: E402

HOME_TEAM = home_team()

# ── Signal-type detectors (deterministic; applied to title+body lowercased) ──
# Order = priority. First match wins. Detectors ROUTE, never gate inclusion.

INCIDENT_PATTERNS = [
    "is alerting incident commander",
    "incident commander:",
    "incident :", "incident:",
    " sr down", " sr is down", " sr dropped", " sr dip", "sr down to", "sr is down to",
    "[firing", "firing:", "firing :",
    " 5xx", "5xx error", "5xx errors",
    "outage", "degraded", "downtime", "stopped being picked",
    "p0 incident", "p1 incident", "internal server error",
    "timeouts from", "timeout from", "latency spike",
]
ROLLOUT_PATTERNS = [
    "go-live", "go live", "going live", "went live",
    "rolled out", "roll out", "rollout",
    "live in prod", "in production",
    "prod readiness", "production rollout", "ga release",
    "rolled out to all users", "rollout to all users",
] + [f"migrated to {t}" for t in org_match_tokens()] + [f"live on {t}" for t in org_match_tokens()]
ROLLOUT_PCT_RE = re.compile(r"\b(\d{1,3})\s*%")
# Past-tense / completion markers → confirmed rollout
ROLLOUT_CONFIRMED = ["rolled out", "went live", "live on", "live in prod",
                     "completed", "is live", "now live", "100%", "successfully rolled"]
# Intent markers → announced-but-unconfirmed
ROLLOUT_INTENT = ["will go live", "going live", "targeted", "planned for",
                  "going to", "to be rolled", "scheduled", "today", "tomorrow", "eta"]

DESIGN_PATTERNS = [
    "trd", "tech spec", "technical spec", "design doc", "rfc",
    " approach", "proposal", "pre-read", "sequence diagram", "architecture",
    "[epic", "epic created", "schema design",
]
NOISE_PATTERNS = [
    "has joined the channel", "has left the channel",
    "set the channel topic", "pinned a message",
]
NOISE_RECURRING = [
    "daily oncall report", "oncall report -", "daily stats", "daily report",
    "release branch", "release-branch", "todays common branch", "today's common branch",
    "uat branch", "standup", "please join", "weekly sync call",
]


def _has(text: str, patterns: list[str]) -> str | None:
    for p in patterns:
        if p in text:
            return p
    return None


OOO_HR_PATTERNS = [
    "not feeling well", "taking a day off", "day off", "on leave", "wfh",
    "working from home", "work from home", "flight got delayed", "flight delayed",
    "will be login", "will be on leave", "logout today", "log out today",
    "coming in around", "out of office", " ooo", "ooo on", "half day",
    "sick leave", "wisdom teeth", "surgery", "feeling unwell", "on vacation",
    "personal plans", "planned leave", "planned leaves", "update planned leaves",
    # HR-admin coordination noise
    "msf nomination", "msf nominations", "self reviews by", "self-reviews by",
    "performance review cycle", "nominations by",
    # IT-helpdesk noise
    "unlock the laptop", "laptop is locked", "laptop unlock",
    "not able to login", "not able to log in", "vpn is enabled", "reset password",
    "fresh new seats", "seats for us", "new seats",
    # standup / meeting nudges
    "join standup", "join the standup", "please join standup", "pls join standup",
    "join the weekly sync", "join the daily",
]


def _signal_type(title: str, body: str, source: str, event_types: set[str],
                 channel_id: str = "", incident_channels: set[str] | None = None,
                 alert_channels: set[str] | None = None,
                 issue_type: str | None = None, went_done: bool = False) -> tuple[str, str]:
    """Return (signal_type, matched_evidence).

    STRUCTURAL signals (channel role, source, event_type, jira issue_type,
    status→Done) take priority over phrase-matching — they have complete recall
    where keywords have holes. Detectors only ROUTE; a miss lands in
    `discussion`, never silent loss.
    """
    incident_channels = incident_channels or set()
    alert_channels = alert_channels or set()
    t = f"{title or ''} {body or ''}".lower()

    # 1. Structural noise — channel join/leave/topic. Exception: an
    #    "Incident Commander" topic-set IS an incident marker.
    nm = _has(t, NOISE_PATTERNS)
    if nm:
        if "incident commander" in t:
            return "incident", "ic-topic"
        return "noise", nm

    # 2. JIRA — route by issue_type (structural; complete recall, no keywords).
    #    A "Fix Double Credit" Bug ticket lands in `fix` even with no keyword.
    if source == "jira" and issue_type:
        it = issue_type.lower()
        if it == "epic":
            return "design", "jira-epic"
        if it in ("bug", "incident action item"):
            return ("delivery" if went_done else "fix"), f"jira-{it}{'/done' if went_done else ''}"
        if it == "cmr":
            return ("delivery" if went_done else "cmr_ops"), f"jira-cmr{'/done' if went_done else ''}"
        # Task / Story / other → delivery if shipped this window, else work.
        return ("delivery" if went_done else "work"), f"jira-{it}{'/done' if went_done else ''}"

    # 3. Confluence pages = design artefacts (structural).
    if source == "confluence":
        return "design", "confluence-page"

    # 4. GitHub PRs = delivery work (structural).
    if source == "github" and ({"pr_opened", "pr_merged"} & event_types):
        return "pr_work", "github-pr"

    # 5. Incident-RESPONSE channel — any thread root here is an incident by the
    #    channel's purpose. Structural ⇒ complete recall.
    if channel_id and channel_id in incident_channels:
        return "incident", "oncall-channel"

    # 6. Auto-alert FEED channel (opsgenie/grafana) — machine alerts, recurring.
    if channel_id and channel_id in alert_channels:
        return "alert_auto", "alert-feed-channel"

    # 7. OOO / HR / leave (slack) → noise.
    m = _has(t, OOO_HR_PATTERNS)
    if m:
        return "noise", f"ooo:{m}"

    # 8. Keyword incident — for incident signals OUTSIDE the response channels.
    m = _has(t, INCIDENT_PATTERNS)
    if m:
        return "incident", m

    # 9. Rollout — require a rollout KEYWORD; a bare percentage is not enough.
    m = _has(t, ROLLOUT_PATTERNS)
    if m:
        return "rollout", m

    # 10. Recurring noise (daily reports, release-branch, standup).
    m = _has(t, NOISE_RECURRING)
    if m:
        return "noise", m

    # 11. Design keywords (TRD/approach/proposal).
    m = _has(t, DESIGN_PATTERNS)
    if m:
        return "design", m

    return "discussion", ""


def _ownership_class(owner: str | None, team_ids: dict) -> str:
    if owner == HOME_TEAM:
        return "team"
    if owner in ("external", "unknown", None, ""):
        return "external"
    if owner in team_ids:
        return "sister"
    return "external"


def _rollout_confirmed(title: str, body: str) -> bool | None:
    t = f"{title or ''} {body or ''}".lower()
    if _has(t, ROLLOUT_CONFIRMED):
        return True
    if _has(t, ROLLOUT_INTENT):
        return False
    return None  # rollout mention without clear tense


def _slack_url(subject: str) -> str:
    parts = subject.split(":")
    if len(parts) == 3 and parts[0] == "slack":
        return f"https://{slack_workspace()}.slack.com/archives/{parts[1]}/p{parts[2].replace('.', '')}"
    if subject.startswith("page:"):
        return f"(confluence {subject})"
    if subject.startswith(github_org() + "/") and "#" in subject:
        repo, num = subject.rsplit("#", 1)
        return f"https://github.com/{repo}/pull/{num}"
    if re.match(r"^[A-Z]+-\d+$", subject):
        return f"https://{atlassian_host()}/browse/{subject}"  # jira
    return f"({subject})"


def _load_team_ids() -> set[str]:
    import yaml
    teams = yaml.safe_load((_REPO_ROOT / "config" / "teams.yaml").read_text())
    return {t["id"] for t in teams.get("teams", []) if t.get("id")}


def _terminal_states() -> list[str]:
    """Delivered/shipped status names — single source of truth in
    config/tier_expectations.yaml::status_classes (shipped + ops_closed).
    Shared with person_profile so a new terminal state is one config edit.
    """
    import yaml
    cfg = yaml.safe_load((_REPO_ROOT / "config" / "tier_expectations.yaml").read_text())
    sc = cfg.get("status_classes", {}) or {}
    return list(sc.get("shipped", []) or []) + list(sc.get("ops_closed", []) or [])


def _terminal_sql(col: str = "to_status") -> str:
    """Substring-tolerant SQL predicate for terminal states (handles emoji/
    prefix variants like 'Change Released 🧩' matching 'Released')."""
    names = _terminal_states()
    if not names:
        return "0"
    return " OR ".join(f"{col} LIKE '%' || ? || '%'" for _ in names)


def _load_incident_channels() -> tuple[set[str], set[str]]:
    """Return (incident_response_channels, auto_alert_feed_channels).

    Structural incident detection: any thread in an incident-RESPONSE channel
    is an incident; auto-alert FEED channels get the separate `alert_auto`
    signal. Derived from channel names in slack_channels.yaml.
    """
    import yaml
    response, feed = set(), set()
    cfg = _REPO_ROOT / "config" / "slack_channels.yaml"
    if not cfg.exists():
        # gitignored private config — absent in CI / fresh checkouts; degrade to no
        # structural incident/alert channels rather than crashing.
        return response, feed
    ch = yaml.safe_load(cfg.read_text())["channels"]
    for c in ch:
        n = (c.get("name") or "").lower()
        cid = c.get("id")
        if not cid:
            continue
        if ("oncall" in n or "on-call" in n) and not n.startswith("it-"):
            response.add(cid)          # service-c-oncall channels, on-call
        elif "incident" in n:
            response.add(cid)
        elif any(k in n for k in ("opsgenie", "grafana", "_alerts", "-alerts", "alert_")):
            feed.add(cid)
    return response, feed


def build_census(conn: sqlite3.Connection, since: str, until: str) -> dict:
    team_ids = _load_team_ids()
    incident_channels, alert_channels = _load_incident_channels()

    # 1. Denominator — every subject with ≥1 event in window. went_done =
    #    reached a terminal status (config-driven, status_classes) IN window.
    terminal_names = _terminal_states()
    term_pred = _terminal_sql("to_status")
    rows = conn.execute(
        "SELECT subject, source, "
        "       GROUP_CONCAT(DISTINCT event_type) AS etypes, "
        "       MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
        "       MAX(issue_type) AS issue_type, "
        f"       MAX(CASE WHEN event_type='status_change' AND ({term_pred}) "
        "                THEN 1 ELSE 0 END) AS went_done "
        "FROM events WHERE ts BETWEEN ? AND ? AND subject IS NOT NULL "
        "GROUP BY subject",
        (*terminal_names, since, until),
    ).fetchall()

    # Window-edge / fate: which delivery candidates reached a terminal state
    # BEFORE the window (work delivered earlier, only closed/touched in-window —
    # the example-db leak). Earliest-ever terminal < since ⇒ pre-window delivery.
    pre_window_delivery: set[str] = set()
    if terminal_names:
        for (sub, first_term) in conn.execute(
            f"SELECT subject, MIN(ts) FROM events WHERE event_type='status_change' "
            f"AND ({term_pred}) GROUP BY subject HAVING MIN(ts) < ?",
            (*terminal_names, since),
        ).fetchall():
            pre_window_delivery.add(sub)

    # Preload subject_summary (title/body come from events; owner from summary).
    summ = {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT subject, owned_by_primary, summary FROM subject_summary").fetchall()
    }

    buckets: dict[str, list[str]] = {}
    incidents: list[dict] = []
    rollouts: list[dict] = []
    window_edge: list[dict] = []   # delivery candidates that shipped BEFORE the window
    by_owner = {"team": 0, "sister": 0, "external": 0}
    by_signal = {"incident": 0, "alert_auto": 0, "rollout": 0, "delivery": 0,
                 "fix": 0, "cmr_ops": 0, "work": 0, "design": 0,
                 "pr_work": 0, "discussion": 0, "noise": 0}
    unclassified: list[str] = []

    for subject, source, etypes, first_ts, last_ts, issue_type, went_done in rows:
        etset = set((etypes or "").split(","))
        # title/body — prefer thread_started/issue_created/page root event
        ev = conn.execute(
            "SELECT title, body, actor FROM events WHERE subject=? "
            "ORDER BY CASE event_type "
            "  WHEN 'thread_started' THEN 0 WHEN 'issue_created' THEN 0 "
            "  WHEN 'page_created' THEN 0 WHEN 'pr_opened' THEN 0 ELSE 1 END, ts "
            "LIMIT 1",
            (subject,),
        ).fetchone()
        title, body, actor = (ev or ("", "", ""))
        owner = summ.get(subject, (None, None))[0]
        channel_id = subject.split(":")[1] if subject.startswith("slack:") and subject.count(":") == 2 else ""

        oclass = _ownership_class(owner, team_ids)
        stype, evidence = _signal_type(title, body, source, etset, channel_id,
                                       incident_channels, alert_channels,
                                       issue_type, bool(went_done))
        by_owner[oclass] += 1
        by_signal[stype] += 1

        key = f"{oclass}/{stype}"
        buckets.setdefault(key, []).append(subject)

        # Window-edge: a delivery candidate that reached terminal BEFORE the
        # window was delivered earlier (only closed/touched in-window). Flag so
        # synthesis doesn't claim it as an in-window delivery (example-db leak).
        if stype in ("delivery", "fix", "cmr_ops") and subject in pre_window_delivery:
            window_edge.append({
                "subject": subject, "signal": stype, "owner": owner or "unknown",
                "ownership_class": oclass, "title": (title or "")[:100],
                "note": "terminal status reached before window — delivered earlier; "
                        "not an in-window delivery. Verify before claiming as a High.",
                "url": _slack_url(subject),
            })

        if stype == "incident":
            incidents.append({
                "subject": subject, "ts": first_ts, "owner": owner or "unknown",
                "ownership_class": oclass, "title": (title or "")[:120],
                "evidence": evidence, "url": _slack_url(subject),
            })
        elif stype == "rollout":
            rollouts.append({
                "subject": subject, "ts": first_ts, "owner": owner or "unknown",
                "ownership_class": oclass, "title": (title or "")[:120],
                "pct": (ROLLOUT_PCT_RE.search(f"{title} {body}".lower()).group(1)
                        if ROLLOUT_PCT_RE.search(f"{title} {body}".lower()) else None),
                "confirmed": _rollout_confirmed(title, body),
                "url": _slack_url(subject),
            })

    # ── Ownership audit — surface the residual the pipeline is unsure about ──
    # identity-fallback = subjects whose ownership came from author/channel
    # (thin content), the known blind spot. review_present = review-flagged
    # domains that appeared this window (owner should confirm their team).
    from derive.ownership_resolve import load_map, review_slugs
    omap = load_map()
    review_set = set(review_slugs(omap))
    id_fallback = conn.execute(
        "SELECT COUNT(*) FROM subject_summary WHERE ownership_reasoning LIKE 'identity:%'"
    ).fetchone()[0]
    win_subjects = {r[0] for r in rows}
    review_present: dict[str, int] = {}
    if review_set:
        for r in conn.execute(
            "SELECT subject, domains FROM subject_summary WHERE domains IS NOT NULL"
        ).fetchall():
            if r[0] not in win_subjects:
                continue
            try:
                for d in json.loads(r[1] or "[]"):
                    if d in review_set:
                        review_present[d] = review_present.get(d, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass
    ownership_audit = {
        "identity_fallback_subjects_total": id_fallback,
        "review_domains_in_window": dict(sorted(review_present.items(), key=lambda x: -x[1])),
        "note": "identity_fallback = ownership from author/channel (thin content). "
                "review_domains = ambiguous-primary slugs present this window; confirm team in domain_team_map.yaml.",
    }

    total = len(rows)
    noise_n = by_signal["noise"]
    # external/discussion = surfaced-only-as-context; not "noise" but not a high/low driver.
    excluded_noise = noise_n
    represented = total - excluded_noise - len(unclassified)

    return {
        "window": {"since": since, "until": until},
        "totals": {
            "subjects": total,
            "represented": represented,
            "excluded_noise": excluded_noise,
            "unclassified": len(unclassified),
        },
        "coverage_ok": (represented + excluded_noise + len(unclassified) == total) and len(unclassified) == 0,
        "by_ownership": by_owner,
        "by_signal": by_signal,
        "ownership_audit": ownership_audit,
        "window_edge": window_edge,
        "bucket_counts": {k: len(v) for k, v in sorted(buckets.items())},
        "incidents": sorted(incidents, key=lambda x: x["ts"]),
        "rollouts": sorted(rollouts, key=lambda x: x["ts"]),
        "buckets": buckets,
        "unclassified": unclassified,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--format", choices=["json", "summary"], default="json")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    census = build_census(conn, args.since, args.until)

    if args.format == "summary":
        t = census["totals"]
        print(f"window {args.since[:10]} → {args.until[:10]}")
        print(f"coverage_ok: {census['coverage_ok']}")
        print(f"  subjects={t['subjects']}  represented={t['represented']}  "
              f"noise={t['excluded_noise']}  unclassified={t['unclassified']}")
        print(f"by_ownership: {census['by_ownership']}")
        print(f"by_signal:    {census['by_signal']}")
        oa = census["ownership_audit"]
        print(f"ownership_audit: identity-fallback={oa['identity_fallback_subjects_total']} "
              f"(thin-content residual) · review-domains-in-window={oa['review_domains_in_window']}")
        we = census["window_edge"]
        print(f"window_edge: {len(we)} delivery candidate(s) shipped BEFORE window (verify, not in-window deliveries)")
        for w in we[:10]:
            print(f"  [{w['ownership_class']}/{w['signal']}] {w['subject']}  {w['title']}")
        print(f"\nincidents ({len(census['incidents'])}):")
        for inc in census["incidents"]:
            print(f"  {inc['ts'][:10]} [{inc['ownership_class']:8s}] {inc['title']}")
            print(f"             {inc['url']}")
        print(f"\nrollouts ({len(census['rollouts'])}):")
        for r in census["rollouts"]:
            conf = {True: "CONFIRMED", False: "ANNOUNCED-unconfirmed", None: "?"}[r["confirmed"]]
            pct = f" {r['pct']}%" if r["pct"] else ""
            print(f"  {r['ts'][:10]} [{r['ownership_class']:8s}] {conf}{pct}  {r['title']}")
        return 0

    print(json.dumps(census, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

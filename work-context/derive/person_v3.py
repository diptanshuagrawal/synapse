#!/usr/bin/env python3
"""
person_v3.py — merged per-person narrative bundle (V1 rating + V2 discovery).

Combines the two prior approaches:
  - V2 (person_census)  → DISCOVERY: complete recall + signal taxonomy
    (shipped / fixed / responded / designed / built / ops), own-vs-contributed,
    coverage proof, CMR-as-delivery, window-edge.
  - V1 (person_profile) → RATING: reliability gates, tier verdict, sprint
    cadence, PR cycle-time/fate, behavioral signals.

Plus the fix the comparison exposed: V1 mis-rates platform/ops engineers
(Bob read "below-band, 0 SP shipped" because his work is CMRs + TRDs +
fixes, not SP-pointed feature PRs). V3 classifies a delivery TRACK from the V2
own-work signal mix and, when the person is not feature-track, marks the
feature-track tier verdict NOT-APPLICABLE and routes evaluation to the V2
delivery evidence.

Track classification (from V2 own_by_signal):
  feature  — own pr_work (authored feature PRs) dominates
  platform — own design + cmr_ops + jira deliveries (DB/infra/SP-less) dominate
  ops      — own incident + cmr_ops + ops_duty dominate
  mixed    — no clear majority

Usage:
    .venv/bin/python derive/person_v3.py --name <canonical> \\
        --since 2026-05-01T00:00:00Z --until 2026-05-28T23:59:59Z
    .venv/bin/python derive/person_v3.py --name <canonical> ... --format summary
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
from derive.person_profile import (  # noqa: E402  (V1 rating)
    compute_profile,
    _infer_window_role,
    _ROLE_PRIORITY,
)
from derive.person_census import build_person_census, _resolve_canonical  # noqa: E402  (V2 discovery)
from derive.jira_metrics import get_aliases_for  # noqa: E402


LED_ROLES = ("AUTHOR", "RESOLVER", "DECIDER")
REVIEW_EVENT_TYPES = ("review", "comment", "commit_in_pr")


def _review_concentration(conn, aliases: list[str], since: str, until: str) -> dict | None:
    """Cluster where the person made the most review-shaped events
    (review / comment / commit_in_pr) in window — the 'reviewer-of-record'
    signal. Promotes the old manual SQL probe into a contract field so it
    never has to be hand-run again."""
    if not aliases:
        return None
    aph = ",".join("?" * len(aliases))
    eph = ",".join("?" * len(REVIEW_EVENT_TYPES))
    rows = conn.execute(
        f"""SELECT m.cluster_id, e.event_type, COUNT(*) AS n
              FROM events e JOIN topic_brief_member m ON m.subject = e.subject
             WHERE e.actor IN ({aph}) AND e.event_type IN ({eph})
               AND e.ts >= ? AND e.ts < ?
             GROUP BY m.cluster_id, e.event_type""",
        (*aliases, *REVIEW_EVENT_TYPES, since, until),
    ).fetchall()
    if not rows:
        return None
    by: dict[int, dict] = {}
    for cid, et, n in rows:
        by.setdefault(cid, {"review": 0, "comment": 0, "commit_in_pr": 0})[et] = n
    top_cid = max(by, key=lambda c: sum(by[c].values()))
    tot = sum(by[top_cid].values())
    tb = conn.execute("SELECT label FROM topic_brief WHERE cluster_id=?", (top_cid,)).fetchone()
    return {
        "cluster_id": top_cid,
        "label": tb[0] if tb else None,
        "comments": by[top_cid]["comment"],
        "reviews": by[top_cid]["review"],
        "commits": by[top_cid]["commit_in_pr"],
        "total": tot,
    }


def _workstreams(conn, aliases: list[str], since: str, until: str,
                 person_subjects: set[str]) -> list[dict]:
    """Group the person's subjects into cluster workstreams + their cluster-level
    role. Role is the WINDOW role (derived from the person's in-window event
    types via _infer_window_role) — the SAME method compute_project_footprint
    uses, so workstreams and footprint never disagree. lifetime_role (from
    participants_json) + role_drift are carried alongside so a DECIDER→RESPONDER
    handoff surfaces. Upgrades 'shipped these tickets' to 'led / contributed to
    these workstreams'."""
    if not person_subjects or not aliases:
        return []
    subs = list(person_subjects)
    ph = ",".join("?" * len(subs))
    rows = conn.execute(
        f"SELECT subject, cluster_id FROM topic_brief_member WHERE subject IN ({ph})", subs
    ).fetchall()
    by_cluster: dict[int, list[str]] = {}
    for sub, cid in rows:
        by_cluster.setdefault(cid, []).append(sub)
    if not by_cluster:
        return []

    # Per-cluster WINDOW event types for this person (drives window_role) —
    # mirrors compute_project_footprint so role assignment is consistent.
    cids = list(by_cluster)
    aph = ",".join("?" * len(aliases))
    cph = ",".join("?" * len(cids))
    ev_rows = conn.execute(
        f"""SELECT m.cluster_id, e.event_type,
                   SUM(CASE WHEN e.event_type='status_change'
                             AND e.body LIKE '%→ Done%' THEN 1 ELSE 0 END) AS done_n
              FROM events e JOIN topic_brief_member m ON m.subject = e.subject
             WHERE e.actor IN ({aph}) AND m.cluster_id IN ({cph})
               AND e.ts >= ? AND e.ts < ?
             GROUP BY m.cluster_id, e.event_type""",
        (*aliases, *cids, since, until),
    ).fetchall()
    cw: dict[int, dict] = {}
    for cid, et, done_n in ev_rows:
        d = cw.setdefault(cid, {"ets": set(), "done": False})
        d["ets"].add(et)
        if done_n and done_n > 0:
            d["done"] = True

    out = []
    for cid, csubs in by_cluster.items():
        tb = conn.execute(
            "SELECT label, participants_json FROM topic_brief WHERE cluster_id=?", (cid,)
        ).fetchone()
        if not tb:
            continue
        label, pj_raw = tb
        lifetime_role = None
        if pj_raw:
            try:
                for p in json.loads(pj_raw):
                    if isinstance(p, dict) and p.get("person") in aliases:
                        lifetime_role = p.get("role")
                        break
            except (json.JSONDecodeError, TypeError):
                pass
        cwd = cw.get(cid, {"ets": set(), "done": False})
        window_role = _infer_window_role(cwd["ets"], cwd["done"])
        lt = _ROLE_PRIORITY.get(lifetime_role or "", 0)
        wn = _ROLE_PRIORITY.get(window_role or "", 0)
        role_drift = bool(lifetime_role and window_role and abs(lt - wn) >= 2)
        out.append({
            "cluster_id": cid, "label": label,
            "role": window_role,                 # authoritative = WINDOW role
            "lifetime_role": lifetime_role,
            "role_drift": role_drift,
            "n_person_subjects": len(csubs),
            "led": window_role in LED_ROLES,
        })
    out.sort(key=lambda w: (-(1 if w["led"] else 0), -w["n_person_subjects"]))
    return out


# A message body line confirming the on-call member ACK'd/RESOLVED an incident
# (posted by the oncall bot, mentioning the member). Phrase-anchored so a member
# merely listed in a daily-stats report isn't miscounted as a response.
_ONCALL_CONFIRM_MARKERS = ("acknowledged the issue", "resolved the issue",
                           "marked the issue as resolved")
# Slack user/subteam id shape — used to pick the member's @-mention token out of
# the alias bundle (which also holds github / email / jira ids).
_SLACK_ID_RE = re.compile(r"^[UW][A-Z0-9]{5,}$")


def _oncall_ops_subjects(conn, aliases: list[str], since_b: str, until: str) -> set[str]:
    """Distinct slack threads where the member was INVOLVED in on-call work over
    [since_b, until). Three additive signals, mirroring the shared on-call
    detection (derive/oncall_signals) so this coarse baseline role agrees with the
    detailed incident census instead of lagging it:

      1. the member STARTED a thread in an incident-response channel
         (thread_started-by-actor — the original, narrow signal);
      2. the member replied in a thread whose body pings an @oncall HANDLE token,
         in ANY channel — on-call work reaches plain domain channels via the
         handle ping, not only oncall-named channels;
      3. the oncall bot posted an ACK/RESOLVE confirmation mentioning the member
         in a class:oncall channel — incidents are rooted by the bot/reporter, so
         the responder ACKs/RESOLVES them and never `thread_started` them.

    Returns a SET so the signals union without double-counting. Light SQL,
    actor/channel/subject-indexed; fail-soft on absent config (empty → no ops).
    Channels are matched on the indexed `channel_id` column (length-agnostic —
    the old substr(subject,7,11) silently missed sub-11-char channel ids)."""
    from derive.retro_census import _load_incident_channels
    from derive.oncall_signals import oncall_channel_ids, oncall_handle_tokens

    if not aliases:
        return set()
    ph = ",".join("?" * len(aliases))
    subjects: set[str] = set()

    # 1. member STARTED a thread in an incident-response channel.
    inc_ch, _ = _load_incident_channels()
    if inc_ch:
        cph = ",".join("?" * len(inc_ch))
        rows = conn.execute(
            f"SELECT DISTINCT subject FROM events WHERE source='slack' "
            f"AND event_type='thread_started' AND actor IN ({ph}) "
            f"AND ts BETWEEN ? AND ? AND channel_id IN ({cph})",
            (*aliases, since_b, until, *inc_ch)).fetchall()
        subjects.update(r[0] for r in rows)

    # 2. member replied in a thread that pages an @oncall HANDLE (any channel).
    toks = oncall_handle_tokens()
    if toks:
        like = " OR ".join("x.body LIKE ?" for _ in toks)
        rows = conn.execute(
            f"SELECT DISTINCT e.subject FROM events e WHERE e.source='slack' "
            f"AND e.actor IN ({ph}) AND e.ts BETWEEN ? AND ? "
            f"AND EXISTS (SELECT 1 FROM events x WHERE x.subject=e.subject "
            f"  AND x.source='slack' AND ({like}))",
            (*aliases, since_b, until, *[f"%{t}%" for t in toks])).fetchall()
        subjects.update(r[0] for r in rows)

    # 3. oncall bot ACK/RESOLVE confirmation mentioning the member, in a
    #    class:oncall channel (member ACKs/RESOLVES, never roots the incident).
    oncall_ch = oncall_channel_ids()
    mentions = [f"%<@{a}%" for a in aliases if _SLACK_ID_RE.match(a or "")]
    if oncall_ch and mentions:
        cph = ",".join("?" * len(oncall_ch))
        mclause = " OR ".join("body LIKE ?" for _ in mentions)
        kclause = " OR ".join("body LIKE ?" for _ in _ONCALL_CONFIRM_MARKERS)
        rows = conn.execute(
            f"SELECT DISTINCT subject FROM events WHERE source='slack' "
            f"AND channel_id IN ({cph}) AND ts BETWEEN ? AND ? "
            f"AND ({mclause}) AND ({kclause})",
            (*oncall_ch, since_b, until, *mentions,
             *[f"%{m}%" for m in _ONCALL_CONFIRM_MARKERS])).fetchall()
        subjects.update(r[0] for r in rows)

    return subjects


def _baseline_role(conn, aliases: list[str], until: str, days: int = 120) -> tuple[str, str]:
    """Stable role over a trailing window (default 120d) — author-scoped, light
    SQL (no full census). Distinguishes 'normally feature, this month design-
    heavy' from a genuine platform engineer."""
    from datetime import datetime, timedelta
    u = datetime.fromisoformat(until.replace("Z", "+00:00"))
    since_b = (u - timedelta(days=days)).isoformat()
    ph = ",".join("?" * len(aliases))
    feat = conn.execute(
        f"SELECT COUNT(DISTINCT subject) FROM events WHERE event_type IN ('pr_opened','pr_merged') "
        f"AND actor IN ({ph}) AND ts BETWEEN ? AND ?", (*aliases, since_b, until)).fetchone()[0]
    plat = conn.execute(
        f"SELECT COUNT(DISTINCT subject) FROM events WHERE actor IN ({ph}) AND ts BETWEEN ? AND ? "
        f"AND ((source='confluence' AND event_type IN ('page_created','page_updated')) "
        f"  OR (source='jira' AND issue_type IN ('Epic','CMR') AND event_type='issue_created'))",
        (*aliases, since_b, until)).fetchone()[0]
    # ops = on-call INVOLVEMENT (not just thread-started-by-actor): bot-acked
    # incidents + @oncall-handle replies in domain channels also count, so a
    # heavy-oncall engineer is labeled 'ops', not mislabeled 'mixed'.
    ops = len(_oncall_ops_subjects(conn, aliases, since_b, until))
    scores = {"feature": feat, "platform": plat, "ops": ops}
    top = max(scores, key=scores.get)
    if scores[top] == 0:
        return "mixed", f"{scores}"
    ranked = sorted(scores.values(), reverse=True)
    if len(ranked) >= 2 and ranked[0] and (ranked[0] - ranked[1]) / ranked[0] <= 0.25:
        return "mixed", f"{scores}"
    return top, f"{scores}"


def _classify_track(own: dict, dom_owned: int = 0) -> tuple[str, str]:
    """Return (track, basis) from discriminating own-work signals.

    `delivery` (any jira Task→terminal) is role-NEUTRAL — both feature and
    platform engineers ship it — so it does NOT drive the track. Track keys on
    what actually separates roles:
      feature  — authored feature PRs + PR-owned/drove domains
      platform — TRDs/design + CMR (DB/infra) changes
      ops      — incident response + ops-duty rota
    """
    feature = own.get("pr_work", 0) + dom_owned
    platform = own.get("design", 0) + own.get("cmr_ops", 0)
    # ops-TRACK = actual incident RESPONSE, not oncall-rota membership
    # (ops_duty is rota assignment — excluded from shipped AND from track).
    ops = own.get("incident", 0)
    scores = {"feature": feature, "platform": platform, "ops": ops}
    top = max(scores, key=scores.get)
    if scores[top] == 0:
        return "mixed", "no discriminating own signal (delivery-only)"
    ranked = sorted(scores.values(), reverse=True)
    if len(ranked) >= 2 and ranked[0] - ranked[1] <= 1:
        return "mixed", f"close: {scores}"
    return top, f"{scores}"


def build_v3(conn, name: str, since: str, until: str) -> dict:
    canonical = _resolve_canonical(name)
    census = build_person_census(conn, canonical, since, until)
    profile = compute_profile(name, since, until)

    own = census.get("own_by_signal", {})
    nar0 = profile.get("narrative", {}) or {}
    dom_owned = sum(1 for o in (nar0.get("domain_ownership") or [])
                    if o.get("label") in ("OWNED", "DROVE"))
    # THIS window's work-mix (per-window, not a permanent role label).
    window_mix, window_basis = _classify_track(own, dom_owned)
    # Stable role over trailing 120d (role-stability context).
    aliases = get_aliases_for(canonical) or [canonical]
    baseline_role, baseline_basis = _baseline_role(conn, aliases, until)

    v1_verdict = (profile.get("throughput", {}) or {}).get("verdict", {}) or {}
    feature_yardstick = window_mix in ("feature", "mixed")
    if feature_yardstick:
        note = "This window's work-mix is feature-weighted — feature-track tier verdict applies."
    else:
        normally = (f"normally {baseline_role}" if baseline_role == window_mix
                    else f"normally {baseline_role} over 120d, but THIS window was {window_mix}-weighted")
        note = (
            f"This window's work-mix is {window_mix}-weighted ({normally}). Feature-track tier "
            f"verdict ({v1_verdict.get('tier_deviation')}) is NOT the right yardstick for this "
            "window (work is design/CMR/fix, not SP-pointed feature PRs). Evaluate on the delivery "
            "sections + workstreams below."
        )
    rating = {
        "window_work_mix": window_mix,
        "window_mix_basis": window_basis,
        "baseline_role_120d": baseline_role,
        "baseline_basis": baseline_basis,
        "feature_yardstick_applicable": feature_yardstick,
        "v1_feature_verdict": v1_verdict,
        "note": note,
    }

    # Pull the V1 signals worth keeping in the merged bundle.
    nar = profile.get("narrative", {}) or {}
    fate = profile.get("fate", {}) or {}
    v1_signals = {
        "team_rank": nar.get("team_rank"),
        "team_sp_count": nar.get("team_sp_count"),
        "team_median_sp": nar.get("team_median_sp"),
        "team_top_sp": nar.get("team_top_sp"),
        "sp_attributed": nar.get("sp_attributed"),
        "tickets_attributed": nar.get("tickets_attributed"),
        "by_sprint": nar.get("by_sprint", []),
        "ops_tickets_n": len(nar.get("ops_tickets", []) or []),
        "pr_fate_summary": fate.get("pr_fate_summary", {}),
        "domain_ownership": nar.get("domain_ownership", []),
        "risk_flagged_prs": nar.get("risk_flagged_prs", []),
        "attribution_chain": nar.get("attribution_chain", {}),
        "reliability_gates": v1_verdict.get("reliability_gates", {}),
        "ops_track_deviation": v1_verdict.get("ops_track_deviation"),
        "verdict_suppressed_reason": v1_verdict.get("verdict_suppressed_reason"),
        "behavioral": profile.get("behavioral", {}),
    }

    person_subjects = {s for subs in census.get("buckets", {}).values() for s in subs}
    workstreams = _workstreams(conn, aliases, since, until, person_subjects)

    # ── Complete render-contract blocks (so synthesis never re-derives field
    #    names or hand-runs probes — every narrative section maps to a field). ──
    footprint = profile.get("project_footprint", []) or []
    quality = profile.get("quality", {}) or {}
    behavioral = profile.get("behavioral", {}) or {}
    contribution = profile.get("contribution", {}) or {}
    fate_summary = fate.get("pr_fate_summary", {}) or {}
    tp = profile.get("throughput", {}) or {}
    la_feat = (fate.get("lookahead_throughput", {}) or {}).get("feature_track", {}) or {}
    feat_tp = tp.get("feature_track", {}) or {}

    pace = {
        "pr_cycle_median_days": fate_summary.get("pr_cycle_median_days"),
        "slow_pr_count_over_14d": fate_summary.get("slow_pr_count_over_14d"),
        "same_day_pr_count": fate_summary.get("same_day_pr_count"),
        "shipped": fate_summary.get("shipped"),
        "abandoned": fate_summary.get("abandoned"),
        "in_flight": fate_summary.get("in_flight"),
        "shipped_in_lookahead": fate_summary.get("shipped_in_lookahead"),
    }
    completion = {
        "sp_completion_rate_pct": feat_tp.get("sp_completion_rate_pct"),
        "lookahead_sp_completion_rate_pct": la_feat.get("sp_completion_rate_pct"),
        "story_points_shipped": feat_tp.get("story_points_shipped"),
        "story_points_committed": feat_tp.get("story_points_committed"),
    }
    # Role-drift: every project where the person's window role differs from
    # lifetime role (the DECIDER→RESPONDER handoff signal), flattened from the
    # footprint compute_project_footprint already does.
    role_drift = []
    for proj in footprint:
        for c in proj.get("clusters", []):
            if c.get("role_drift"):
                role_drift.append({
                    "project_slug": proj.get("project_slug"),
                    "cluster_id": c.get("cluster_id"),
                    "label": c.get("label"),
                    "lifetime_role": c.get("lifetime_role"),
                    "window_role": c.get("window_role"),
                })
    review_concentration = _review_concentration(conn, aliases, since, until)

    return {
        "person": canonical,
        "window": {"since": since, "until": until},
        "coverage": census.get("totals"),
        "coverage_ok": census.get("coverage_ok"),
        "rating": rating,
        "workstreams": workstreams,                  # cluster-grained: led / contributed (window role)
        "delivery": census.get("sections", {}),     # V2 shipped/fixed/responded/designed/built/ops
        "own_by_signal": own,
        "window_edge": census.get("window_edge", []),
        # ── render-contract blocks (REQUIRED render targets — see ask.md) ──
        "contribution": contribution,   # engagement shape: commits-in-PR, reviews, confluence, slack
        "behavioral": behavioral,
        "pace": pace,
        "quality": quality,
        "completion": completion,
        "project_footprint": footprint,
        "review_concentration": review_concentration,
        "role_drift": role_drift,
        "ticket_fate": fate.get("ticket_fate", {}),   # tickets that resolved just after window close
        "v1_signals": v1_signals,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--format", choices=["json", "summary"], default="json")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    v3 = build_v3(conn, args.name, args.since, args.until)

    if args.format == "summary":
        r = v3["rating"]
        c = v3["coverage"]
        print(f"person: {v3['person']}  window {args.since[:10]} → {args.until[:10]}")
        print(f"coverage_ok: {v3['coverage_ok']}  subjects={c['subjects']} unclassified={c['unclassified']}")
        print(f"\nWINDOW WORK-MIX: {r['window_work_mix']}  ({r['window_mix_basis']})")
        print(f"BASELINE ROLE (120d): {r['baseline_role_120d']}  ({r['baseline_basis']})")
        print(f"feature_yardstick_applicable: {r['feature_yardstick_applicable']}")
        print(f"  → {r['note']}")
        print(f"  V1 feature verdict (for reference): tier_deviation={r['v1_feature_verdict'].get('tier_deviation')}")
        v = v3["v1_signals"]
        print(f"\nV1 signals: team_rank={v['team_rank']}/{v.get('team_sp_count')} "
              f"sp_attributed={v['sp_attributed']} (median={v.get('team_median_sp')} top={v.get('team_top_sp')}) "
              f"tickets={v['tickets_attributed']} pr_fate={v['pr_fate_summary'].get('shipped',0)}shipped/"
              f"{v['pr_fate_summary'].get('in_flight',0)}in-flight ops_tickets={v['ops_tickets_n']}")
        if v.get("ops_track_deviation") or v.get("verdict_suppressed_reason"):
            print(f"  OPS-BAND verdict: {v.get('ops_track_deviation')}"
                  + (f"  (feature verdict suppressed: {v['verdict_suppressed_reason']})"
                     if v.get('verdict_suppressed_reason') else ""))
        rfp = v.get("risk_flagged_prs", []) or []
        if rfp:
            print(f"  RISK-FLAGGED PRs ({len(rfp)}): {rfp[:5]}")
        ac = v.get("attribution_chain", {}) or {}
        if ac and ac.get("creation_fallback", 0) > ac.get("changelog", 0):
            print(f"  ⚠ attribution: creation_fallback={ac.get('creation_fallback')} > changelog={ac.get('changelog')} "
                  "— SP attribution less certain (flag as caveat)")
        tf = v3.get("ticket_fate", {}) or {}
        ril = tf.get("resolved_in_lookahead", []) or []
        if ril:
            print(f"  TICKET-FATE: {len(ril)} ticket(s) resolved just after window "
                  f"(shipped+{tf.get('shifted_to_shipped',0)} / cancelled+{tf.get('shifted_to_cancelled',0)})")

        # ── render-contract blocks (mirror the JSON; never silently dropped) ──
        ct = v3.get("contribution", {}) or {}
        ce = ct.get("confluence_edits", {}) or {}
        csb = ct.get("cross_surface_breadth", {}) or {}
        print(f"\nCONTRIBUTION: commits_in_pr={ct.get('substantive_pr_commits')} "
              f"pr_reviews={ct.get('pr_reviews_total')}/{ct.get('pr_reviews_distinct_subjects')}subj "
              f"confluence={ce.get('events')}edits/{ce.get('body_bytes')}b/{ct.get('confluence_inline_comments')}inline "
              f"slack={ct.get('substantive_slack_replies')}/{ct.get('slack_replies_total')} "
              f"jira_comments={ct.get('jira_comments_total')} authorship={ct.get('authorship')}")
        if csb:
            print(f"  cross_surface: slack={csb.get('slack')} jira={csb.get('jira')} "
                  f"github={csb.get('github')} confluence={csb.get('confluence')}")
        b = v3.get("behavioral", {}) or {}
        print(f"\nBEHAVIORAL: first_responder={b.get('first_responder_rate_pct')}% "
              f"resolver={b.get('resolver_rate_pct')}% p50={b.get('p50_response_latency_min')}min "
              f"p90={b.get('p90_response_latency_min')}min after_hours={b.get('after_hours_share_pct')}% "
              f"weekend={b.get('weekend_share_pct')}% thread_followup={b.get('thread_followup_rate_pct')}%")
        pc = v3.get("pace", {}) or {}
        print(f"PACE: pr_cycle_median_d={pc.get('pr_cycle_median_days')} "
              f"slow>14d={pc.get('slow_pr_count_over_14d')} same_day={pc.get('same_day_pr_count')} "
              f"pr_fate={pc.get('shipped')}shipped/{pc.get('abandoned')}abandoned/{pc.get('in_flight')}in-flight")
        q = v3.get("quality", {}) or {}
        print(f"QUALITY: matterai_p50={q.get('pr_matterai_quality_p50_pct')}% "
              f"critical_flags={q.get('pr_matterai_critical_flags')} reverts={q.get('pr_revert_count')} "
              f"prs={q.get('pr_count_in_window')}")
        cp = v3.get("completion", {}) or {}
        print(f"COMPLETION: sp_completion={cp.get('sp_completion_rate_pct')}% "
              f"(lookahead {cp.get('lookahead_sp_completion_rate_pct')}%) "
              f"shipped {cp.get('story_points_shipped')}/{cp.get('story_points_committed')} SP")
        rc = v3.get("review_concentration")
        if rc:
            print(f"REVIEW-CONCENTRATION: cluster {rc['cluster_id']} '{(rc.get('label') or '')[:50]}' "
                  f"= {rc['comments']}c + {rc['reviews']}rev + {rc['commits']}commit = {rc['total']} events")
        rd = v3.get("role_drift", []) or []
        if rd:
            print(f"ROLE-DRIFT ({len(rd)}): " + "; ".join(
                f"{r['project_slug']} {r['lifetime_role']}→{r['window_role']}" for r in rd[:6]))
        fp = v3.get("project_footprint", []) or []
        if fp:
            print("PROJECT-FOOTPRINT (top 12 by window activity):")
            for p in sorted(fp, key=lambda x: -(x.get("window_event_count_total") or 0))[:12]:
                print(f"  {(p.get('project_slug') or '')[:26]:26s} role={p.get('top_role_in_project')} "
                      f"ev={p.get('window_event_count_total')} clusters={p.get('cluster_count')} "
                      f"drift={p.get('role_drift_cluster_count')}")
            # ALL AUTHOR-role slugs regardless of rank — so a low-event sole-
            # ownership slug (e.g. counter-charge-engine) isn't truncated away.
            author_slugs = [p.get("project_slug") for p in fp
                            if p.get("top_role_in_project") == "AUTHOR"]
            if author_slugs:
                print(f"  AUTHOR-role slugs (ALL, ownership signal): {', '.join(author_slugs)}")

        ws = v3.get("workstreams", [])
        led = [w for w in ws if w["led"]]
        contrib = [w for w in ws if not w["led"]]
        if led:
            print(f"\n== WORKSTREAMS LED ({len(led)}) ==")
            for w in led[:10]:
                print(f"  [{w['role']}·{w['n_person_subjects']} items] {w['label'][:70]}")
        if contrib:
            print(f"\n== WORKSTREAMS CONTRIBUTED ({len(contrib)}) ==")
            for w in contrib[:10]:
                print(f"  [{w['role'] or 'participant'}·{w['n_person_subjects']} items] {w['label'][:66]}")
        for sec in ("shipped", "fixed", "responded_to", "designed", "built", "ops"):
            block = v3["delivery"].get(sec)
            if not block:
                continue
            prim = block.get("primary", [])
            if not prim:
                continue
            print(f"\n== {sec.upper()} (own={len(prim)}, contributed={len(block.get('contributed', []))}) ==")
            for it in prim[:20]:
                print(f"  [{it['role']}] {it['title']}")
        if v3["window_edge"]:
            print(f"\nwindow_edge (delivered before window, excluded): {len(v3['window_edge'])}")
        return 0

    print(json.dumps(v3, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

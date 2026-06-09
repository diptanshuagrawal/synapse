"""
actor_behavior.py — compute per-actor working-style metrics over the
events + topic_brief tables.

Designed to answer use case #6: "Bob jumps in to resolve", "Frank is
first responder", "X replies late". Metrics derive from the thread-reply
graph filtered to *incident-flavored clusters* (clusters whose root_cause
is non-null per Phase C enrichment) so noise threads (joins, MoMs, sprint
pages) don't drown the signal.

Metrics per actor
-----------------
    threads_touched           — incident threads where they appear at all
    threads_authored          — they posted the thread_started / issue_created
    first_responder_count     — earliest reply (by ts) is theirs
    first_responder_rate      — first_responder_count / threads_touched (excluding ones they authored)
    reply_count               — total thread_reply rows for this actor in incident scope
    resolver_count            — replies containing resolution markers
    resolver_rate             — resolver_count / reply_count
    first_reply_latency_p50_sec, p90_sec   — seconds from thread_started to their FIRST reply
    cluster_spread            — list of {cluster_id, cluster_label, reply_count} (top 5)

Resolution markers
------------------
Substring match (case-insensitive) on reply body for any of:
    "resolved", "fixed", "merged", "deployed", "rolled out", "shipped",
    "live", "completed", "closed", "✅", ":white_check_mark:", ":tada:",
    "/pull/", "/commit/"

CLI
---
    .venv/bin/python derive/actor_behavior.py report
        Computes the report, writes to state/actor_behavior_report.json,
        prints summary to stdout.

    .venv/bin/python derive/actor_behavior.py show --person frank-example
        Renders one actor's profile from the saved report.

    .venv/bin/python derive/actor_behavior.py top
        Show top-N actors by participation.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db, _load_people  # noqa: E402


REPORT_PATH = _PKG_ROOT / "state" / "actor_behavior_report.json"

_RESOLUTION_PATTERNS = re.compile(
    r"(?i)(?:\bresolved\b|\bfixed\b|\bmerged\b|\bdeployed\b|\brolled out\b|"
    r"\bshipped\b|\blive\b|\bcompleted\b|\bclosed\b|"
    r"✅|:white_check_mark:|:tada:|/pull/|/commit/)"
)


def _build_actor_canonical_map() -> dict[str, str]:
    """Build raw-actor-id → canonical-handle map.

    Reads from people.yaml. Recognised key shapes:
      slack_id, github, jira_id, email   — single string each
      git_names                           — list of raw git commit-author
                                            names for unlinked GitHub accounts
                                            (also accepts singular `git_name`
                                            for backward compat)
      github_aliases                      — list of raw git author display
                                            names (commits from unlinked
                                            github accounts)
    """
    m: dict[str, str] = {}
    for p in _load_people():
        canon = p.get("canonical")
        if not canon:
            continue
        for key in ("slack_id", "github", "jira_id", "email"):
            v = p.get(key)
            if v:
                m[v] = canon
        git_names = p.get("git_names") or []
        legacy = p.get("git_name")
        if legacy:
            git_names = list(git_names) + [legacy]
        for gn in git_names:
            if gn:
                m[gn] = canon
        for alias in p.get("github_aliases") or []:
            if alias:
                m[alias] = canon
    return m


def _build_actor_scope_map() -> dict[str, str]:
    """Build raw-actor-id → scope (team | org | external) map.

    Unlike `_build_actor_canonical_map`, this honours every entry in
    people.yaml regardless of whether it has a `canonical` slug. Used by
    {github,jira,confluence}_validate.py to classify actors:

        scope == "team"               → counted in analysis
        scope in {"org","external"}   → silenced (known-ack)
        actor absent from map         → unmapped (WARN/FAIL)
    """
    m: dict[str, str] = {}
    for p in _load_people():
        scope = p.get("scope", "team")  # legacy entries default to team
        for key in ("slack_id", "github", "jira_id", "email"):
            v = p.get(key)
            if v:
                m[v] = scope
        git_names = list(p.get("git_names") or [])
        legacy = p.get("git_name")
        if legacy:
            git_names.append(legacy)
        for gn in git_names:
            if gn:
                m[gn] = scope
        for alias in p.get("github_aliases") or []:
            if alias:
                m[alias] = scope
        # `name` is also a valid actor key — Atlassian's "Automation for Jira"
        # arrives as the raw display name with no email.
        nm = p.get("name")
        if nm and not p.get("email"):
            m[nm] = scope
    return m


def _canon(actor_id: str | None, actor_map: dict[str, str]) -> str:
    if not actor_id:
        return "<unknown>"
    return actor_map.get(actor_id, f"<raw:{actor_id}>")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def compute_report(conn) -> dict:
    """Return per-actor behavioral metrics scoped to incident clusters."""
    actor_map = _build_actor_canonical_map()

    # 1. Incident-flavored cluster ids — those with non-null root_cause.
    incident_cids = [r[0] for r in conn.execute(
        "SELECT cluster_id FROM topic_brief WHERE root_cause IS NOT NULL"
    )]
    if not incident_cids:
        return {"actors": {}, "scope": {"incident_clusters": 0}, "computed_at": _now_iso()}
    cid_label = {
        cid: lbl for cid, lbl in conn.execute(
            f"SELECT cluster_id, label FROM topic_brief WHERE cluster_id IN ({','.join('?'*len(incident_cids))})",
            incident_cids,
        )
    }

    # 2. Subjects within those clusters.
    placeholders = ",".join("?" * len(incident_cids))
    subj_to_cid = {
        s: cid for s, cid in conn.execute(
            f"SELECT subject, cluster_id FROM topic_brief_member WHERE cluster_id IN ({placeholders})",
            incident_cids,
        )
    }
    if not subj_to_cid:
        return {"actors": {}, "scope": {"incident_clusters": len(incident_cids)}, "computed_at": _now_iso()}

    subj_list = list(subj_to_cid.keys())
    sub_ph = ",".join("?" * len(subj_list))

    # 3. Pull thread_started / issue_created / pr_opened / page_created rows for "author + start_ts".
    starter_rows = conn.execute(
        f"""SELECT subject, ts, actor, event_type FROM events
             WHERE subject IN ({sub_ph})
               AND event_type IN ('thread_started','issue_created','pr_opened','page_created')
            """,
        subj_list,
    ).fetchall()
    starter_by_subject: dict[str, tuple[str, str]] = {}  # subject -> (start_ts, author)
    for subj, ts, actor, _et in starter_rows:
        # Earliest "start"-like event wins if multiple shapes exist.
        prev = starter_by_subject.get(subj)
        if prev is None or ts < prev[0]:
            starter_by_subject[subj] = (ts, actor)

    # 4. Pull thread replies for incident subjects.
    reply_rows = conn.execute(
        f"""SELECT subject, ts, actor, COALESCE(body, '') FROM events
             WHERE subject IN ({sub_ph})
               AND event_type = 'thread_reply'
             ORDER BY subject, ts ASC
            """,
        subj_list,
    ).fetchall()
    replies_by_subject: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for subj, ts, actor, body in reply_rows:
        replies_by_subject[subj].append((ts, actor, body))

    # 5. Per-actor accumulators.
    actor_stats: dict[str, dict] = defaultdict(lambda: {
        "threads_touched": set(),
        "threads_authored": set(),
        "first_responder_threads": set(),
        "reply_count": 0,
        "resolver_count": 0,
        "first_reply_latencies": [],
        "cluster_replies": defaultdict(int),  # cid -> reply count
    })

    # Author credit.
    for subj, (start_ts, author) in starter_by_subject.items():
        a = _canon(author, actor_map)
        actor_stats[a]["threads_touched"].add(subj)
        actor_stats[a]["threads_authored"].add(subj)
        actor_stats[a]["cluster_replies"][subj_to_cid.get(subj)] += 0  # ensure key

    # Reply traversal: compute first-responder + per-actor first-reply latency.
    for subj, reps in replies_by_subject.items():
        cid = subj_to_cid.get(subj)
        start_info = starter_by_subject.get(subj)
        start_dt = _parse_iso(start_info[0]) if start_info else None
        # Track which actor posted FIRST in this thread (excluding the author).
        seen_first_reply_per_actor: dict[str, str] = {}
        first_responder_actor: str | None = None
        for ts, actor, _body in reps:
            canon = _canon(actor, actor_map)
            actor_stats[canon]["threads_touched"].add(subj)
            actor_stats[canon]["reply_count"] += 1
            actor_stats[canon]["cluster_replies"][cid] += 1
            if first_responder_actor is None and start_info and actor != start_info[1]:
                first_responder_actor = canon
                actor_stats[canon]["first_responder_threads"].add(subj)
            if canon not in seen_first_reply_per_actor:
                seen_first_reply_per_actor[canon] = ts
        # Latency: each actor's FIRST reply ts minus start_ts.
        if start_dt:
            for canon, first_ts in seen_first_reply_per_actor.items():
                rep_dt = _parse_iso(first_ts)
                if rep_dt is not None and rep_dt >= start_dt:
                    actor_stats[canon]["first_reply_latencies"].append((rep_dt - start_dt).total_seconds())
        # Resolver scan.
        for ts, actor, body in reps:
            canon = _canon(actor, actor_map)
            if _RESOLUTION_PATTERNS.search(body):
                actor_stats[canon]["resolver_count"] += 1

    # 6. Finalise per-actor record.
    def _pctl(values, p):
        if not values:
            return None
        s = sorted(values)
        k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
        return float(s[k])

    out_actors: dict[str, dict] = {}
    for actor, st in actor_stats.items():
        touched = len(st["threads_touched"])
        if touched == 0:
            continue
        authored = len(st["threads_authored"])
        first_responder = len(st["first_responder_threads"])
        # Denominator for first_responder_rate excludes threads they authored
        # (you can't first-respond to your own thread by our definition).
        denom = max(0, touched - authored)
        first_responder_rate = (first_responder / denom) if denom > 0 else None
        reply_count = st["reply_count"]
        resolver_count = st["resolver_count"]
        resolver_rate = (resolver_count / reply_count) if reply_count > 0 else None

        # Cluster spread top-5.
        spread = sorted(st["cluster_replies"].items(), key=lambda kv: -kv[1])
        spread = [
            {"cluster_id": cid, "cluster_label": cid_label.get(cid, ""), "reply_count": rc}
            for cid, rc in spread if cid is not None
        ][:5]

        lat = st["first_reply_latencies"]
        out_actors[actor] = {
            "threads_touched": touched,
            "threads_authored": authored,
            "first_responder_count": first_responder,
            "first_responder_rate": first_responder_rate,
            "reply_count": reply_count,
            "resolver_count": resolver_count,
            "resolver_rate": resolver_rate,
            "first_reply_latency_p50_sec": _pctl(lat, 50),
            "first_reply_latency_p90_sec": _pctl(lat, 90),
            "first_reply_latency_n_samples": len(lat),
            "cluster_spread": spread,
        }

    return {
        "computed_at": _now_iso(),
        "scope": {
            "incident_clusters": len(incident_cids),
            "incident_subjects": len(subj_to_cid),
            "incident_replies": len(reply_rows),
        },
        "actors": dict(sorted(out_actors.items(), key=lambda kv: -kv[1]["threads_touched"])),
    }


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── CLI commands ────────────────────────────────────────────────────────────


def cmd_report(args):
    conn = get_db()
    report = compute_report(conn)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2))
    print(f"✓ actor_behavior report written to {REPORT_PATH}")
    print(f"  scope: {report['scope']}")
    print(f"  actors: {len(report['actors'])}")
    print()
    print("Top 10 by threads_touched:")
    for actor, s in list(report["actors"].items())[:10]:
        fr_rate = s["first_responder_rate"]
        rr_rate = s["resolver_rate"]
        p50 = s["first_reply_latency_p50_sec"]
        p50_s = f"p50={int(p50/60)}m" if p50 is not None else "p50=n/a"
        fr_s = f"{fr_rate*100:5.1f}%" if fr_rate is not None else "  n/a"
        rr_s = f"{rr_rate*100:5.1f}%" if rr_rate is not None else "  n/a"
        print(f"  {actor:40s}  threads={s['threads_touched']:3d}  replies={s['reply_count']:3d}  "
              f"first_responder={fr_s}  resolver={rr_s}  {p50_s}")


def cmd_show(args):
    if not REPORT_PATH.exists():
        print(f"missing {REPORT_PATH} — run `report` first")
        return
    report = json.loads(REPORT_PATH.read_text())
    actors = report["actors"]
    # Match prefix-insensitive.
    target = args.person
    matches = [a for a in actors if target.lower() in a.lower()]
    if not matches:
        print(f"no actor matching {target!r}")
        print("available (showing 20):")
        for a in list(actors)[:20]:
            print(f"  {a}")
        return
    for actor in matches:
        s = actors[actor]
        print(f"\n=== {actor} ===")
        print(f"  threads touched (incident scope): {s['threads_touched']}")
        print(f"  threads authored                : {s['threads_authored']}")
        if s["first_responder_rate"] is not None:
            print(f"  first-responder rate            : {s['first_responder_rate']*100:.1f}%"
                  f"  ({s['first_responder_count']} / {s['threads_touched'] - s['threads_authored']} not-authored threads)")
        else:
            print(f"  first-responder rate            : n/a (no non-authored threads)")
        if s["resolver_rate"] is not None:
            print(f"  resolver rate (over their replies): {s['resolver_rate']*100:.1f}%"
                  f"  ({s['resolver_count']} / {s['reply_count']} replies have resolution markers)")
        else:
            print(f"  resolver rate                   : n/a (no replies)")
        if s["first_reply_latency_p50_sec"] is not None:
            p50 = s["first_reply_latency_p50_sec"]
            p90 = s["first_reply_latency_p90_sec"]
            print(f"  first-reply latency             : p50={_fmt_dur(p50)}  p90={_fmt_dur(p90)}  "
                  f"(n={s['first_reply_latency_n_samples']})")
        else:
            print(f"  first-reply latency             : n/a")
        if s["cluster_spread"]:
            print(f"  domain spread (top clusters):")
            for entry in s["cluster_spread"]:
                print(f"     [{entry['cluster_id']:3d}] replies={entry['reply_count']:3d}  {entry['cluster_label']}")


def _fmt_dur(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds/60)}m"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h"
    return f"{seconds/86400:.1f}d"


def cmd_top(args):
    if not REPORT_PATH.exists():
        print(f"missing {REPORT_PATH} — run `report` first")
        return
    report = json.loads(REPORT_PATH.read_text())
    actors = report["actors"]
    by_metric = args.metric
    ranked = sorted(
        actors.items(),
        key=lambda kv: (kv[1].get(by_metric) or -1),
        reverse=True,
    )
    print(f"\nTop {args.n} by {by_metric}:\n")
    for actor, s in ranked[: args.n]:
        v = s.get(by_metric)
        if isinstance(v, float) and 0 <= v <= 1:
            v = f"{v*100:.1f}%"
        print(f"  {actor:40s}  {by_metric}={v}  threads={s['threads_touched']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report", help="Compute + write actor_behavior_report.json")
    r.set_defaults(fn=cmd_report)

    sh = sub.add_parser("show", help="Print one actor's profile")
    sh.add_argument("--person", required=True)
    sh.set_defaults(fn=cmd_show)

    t = sub.add_parser("top", help="Rank actors by a metric")
    t.add_argument("--metric", default="first_responder_rate")
    t.add_argument("-n", type=int, default=10)
    t.set_defaults(fn=cmd_top)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

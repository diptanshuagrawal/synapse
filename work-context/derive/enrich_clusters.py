"""
enrich_clusters.py — chat-driven Phase C enrichment.

Reads `topic_brief` (Phase B labels) + `topic_brief_member` (cluster
membership), dumps each cluster's member contents along with computed
timestamps and per-actor reply counts, and asks the chat to extract:

  status            ACTIVE | RESOLVED | STALE | RECURRING
  decisions         [{text, evidence_subject}]
  blockers          [{text, evidence_subject}]
  root_cause        str | null
  participant_roles {person_canonical: role}

The script computes deterministically (no chat reasoning):

  first_ts, last_activity_ts, participants_observed (canonical → count)

Apply persists chat output to:

  topic_brief.status, decisions_json, blockers_json, root_cause,
  participants_json, first_ts, last_activity_ts

Aligned with `derive/cluster_label_rules.md` flow — scripts NEVER call
an LLM API.

CLI
---
    .venv/bin/python derive/enrich_clusters.py dump [--cluster-ids 21 20]
    .venv/bin/python derive/enrich_clusters.py apply
    .venv/bin/python derive/enrich_clusters.py status
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from ingest.common import get_db, _load_people  # noqa: E402
from derive.subject_content import get_content  # noqa: E402

ROOT = _PKG_ROOT
PENDING_PATH = ROOT / "state" / "pending_cluster_enrichments.json"
RULES_PATH = ROOT / "state" / "pending_cluster_enrichments.json.rules.md"
VERDICTS_PATH = ROOT / "state" / "verdicts.cluster_enrichments.json"
RULES_SOURCE_PATH = ROOT / "derive" / "cluster_enrich_rules.md"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _build_actor_canonical_map() -> dict[str, str]:
    """Map actor_id (slack U-id, github login, jira account-id-ish, raw
    git author name for unlinked GH accounts, etc.) → canonical person name
    from people.yaml. Falls back to the raw id when no entry matches.

    Recognises `git_names` (list) and legacy `git_name` (single string)
    for unlinked GitHub account commit-author resolution.
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
    return m


def _cluster_members(conn, cluster_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT subject FROM topic_brief_member WHERE cluster_id = ? ORDER BY subject",
        (cluster_id,),
    )]


def _event_summary_for_subject(conn, subject: str) -> dict:
    """Compute timestamps + per-actor counts for a subject, plus pull the
    cleaned content body. Considers all events tied to this subject."""
    rows = conn.execute(
        "SELECT ts, actor, event_type FROM events WHERE subject = ? ORDER BY ts",
        (subject,),
    ).fetchall()
    actor_counts: dict[str, int] = defaultdict(int)
    first_ts = None
    last_ts = None
    for ts, actor, _et in rows:
        if first_ts is None or ts < first_ts:
            first_ts = ts
        if last_ts is None or ts > last_ts:
            last_ts = ts
        if actor:
            actor_counts[actor] += 1
    _, content = get_content(conn, subject)
    return {
        "subject": subject,
        "content": content,
        "first_ts": first_ts,
        "last_activity_ts": last_ts,
        "actor_counts": dict(actor_counts),
    }


def _cluster_payload(conn, cluster_id: int, actor_map: dict[str, str], member_chars: int) -> dict:
    row = conn.execute(
        "SELECT label, summary, source_breakdown_json, member_count, confidence "
        "FROM topic_brief WHERE cluster_id = ?",
        (cluster_id,),
    ).fetchone()
    if not row:
        return {}
    label, summary, src_json, member_count, confidence = row
    members = _cluster_members(conn, cluster_id)
    cluster_first_ts = None
    cluster_last_ts = None
    cluster_actor_counts: dict[str, int] = defaultdict(int)
    member_blocks = []
    for subj in members:
        info = _event_summary_for_subject(conn, subj)
        if info["first_ts"] and (cluster_first_ts is None or info["first_ts"] < cluster_first_ts):
            cluster_first_ts = info["first_ts"]
        if info["last_activity_ts"] and (cluster_last_ts is None or info["last_activity_ts"] > cluster_last_ts):
            cluster_last_ts = info["last_activity_ts"]
        for a, c in info["actor_counts"].items():
            cluster_actor_counts[a] += c
        cont = " ".join((info["content"] or "").split())
        if len(cont) > member_chars:
            cont = cont[:member_chars].rstrip() + "…"
        member_blocks.append({
            "subject": subj,
            "first_ts": info["first_ts"],
            "last_activity_ts": info["last_activity_ts"],
            "content": cont,
        })

    # Resolve actor ids to canonical names where possible. Keep raw id when
    # we can't map (bots, deactivated users, unmapped people).
    participants_observed: dict[str, int] = defaultdict(int)
    for actor_id, count in cluster_actor_counts.items():
        canon = actor_map.get(actor_id) or f"<raw:{actor_id}>"
        participants_observed[canon] += count

    return {
        "cluster_id": cluster_id,
        "label": label,
        "label_summary": summary,
        "source_breakdown": json.loads(src_json) if src_json else {},
        "member_count": member_count,
        "label_confidence": confidence,
        "first_ts": cluster_first_ts,
        "last_activity_ts": cluster_last_ts,
        "participants_observed": dict(sorted(participants_observed.items(), key=lambda kv: -kv[1])),
        "members": member_blocks,
    }


# ── dump ────────────────────────────────────────────────────────────────────


def cmd_dump(args):
    conn = get_db()
    actor_map = _build_actor_canonical_map()
    if args.cluster_ids:
        target_ids = sorted(set(args.cluster_ids))
    else:
        target_ids = [r[0] for r in conn.execute(
            "SELECT cluster_id FROM topic_brief ORDER BY cluster_id"
        )]
    if not target_ids:
        print("no clusters in topic_brief — run Phase B first")
        return
    payloads = []
    for cid in target_ids:
        p = _cluster_payload(conn, cid, actor_map, args.member_chars)
        if p:
            payloads.append(p)
    out = {
        "computed_at": _now_iso(),
        "n_clusters": len(payloads),
        "clusters": payloads,
    }
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(out, indent=2))
    if RULES_SOURCE_PATH.exists():
        RULES_PATH.write_text(RULES_SOURCE_PATH.read_text())

    print("✓ enrich-dump complete")
    print(f"  pending file: {PENDING_PATH}")
    print(f"  rules file:   {RULES_PATH}")
    print(f"  clusters:     {len(payloads)}")
    print(f"  total members: {sum(c['member_count'] for c in payloads)}")
    print()
    print(f"Next: in chat, read the rules FIRST, then the dump, then write verdicts to:")
    print(f"  {VERDICTS_PATH}")
    print(f"Then run: .venv/bin/python derive/enrich_clusters.py apply")


# ── apply ───────────────────────────────────────────────────────────────────


def cmd_apply(args):
    if not VERDICTS_PATH.exists():
        print(f"missing {VERDICTS_PATH} — write verdicts first")
        return
    if not PENDING_PATH.exists():
        print(f"missing {PENDING_PATH} — run dump first")
        return
    verdicts = json.loads(VERDICTS_PATH.read_text())
    if not isinstance(verdicts, list):
        print("verdicts file must be a JSON array")
        return
    pending = json.loads(PENDING_PATH.read_text())
    pending_by_id = {c["cluster_id"]: c for c in pending["clusters"]}

    conn = get_db()
    applied = 0
    skipped = 0
    errors = []
    for v in verdicts:
        cid = v.get("cluster_id")
        if cid is None or cid not in pending_by_id:
            errors.append(f"cluster_id {cid!r}: not in pending dump")
            skipped += 1
            continue
        ctx = pending_by_id[cid]
        status = (v.get("status") or "").strip().upper()
        if status not in {"ACTIVE", "RESOLVED", "STALE", "RECURRING"}:
            errors.append(f"cluster_id {cid}: invalid status {status!r}")
            skipped += 1
            continue
        decisions = v.get("decisions") or []
        blockers = v.get("blockers") or []
        root_cause = v.get("root_cause")
        # participants_json combines chat's role assignments with script's counts.
        roles = v.get("participant_roles") or {}
        participants_payload = []
        for person, count in ctx.get("participants_observed", {}).items():
            entry = {"person": person, "contribution_count": count}
            if person in roles:
                entry["role"] = roles[person]
            participants_payload.append(entry)
        # Persist.
        try:
            conn.execute(
                "UPDATE topic_brief "
                "   SET status = ?, decisions_json = ?, blockers_json = ?, "
                "       root_cause = ?, participants_json = ?, "
                "       first_ts = ?, last_activity_ts = ? "
                " WHERE cluster_id = ?",
                (
                    status,
                    json.dumps(decisions, sort_keys=True),
                    json.dumps(blockers, sort_keys=True),
                    root_cause,
                    json.dumps(participants_payload, sort_keys=True),
                    ctx.get("first_ts"),
                    ctx.get("last_activity_ts"),
                    cid,
                ),
            )
            applied += 1
        except Exception as e:
            errors.append(f"cluster_id {cid}: {type(e).__name__}: {e}")
            skipped += 1
    conn.commit()
    print(json.dumps({
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
    }, indent=2))


# ── status ──────────────────────────────────────────────────────────────────


def cmd_status(args):
    conn = get_db()
    info = {
        "pending_dump": PENDING_PATH.exists(),
        "rules_md":     RULES_PATH.exists(),
        "verdicts":     VERDICTS_PATH.exists(),
    }
    if PENDING_PATH.exists():
        info["pending_clusters"] = len(json.loads(PENDING_PATH.read_text())["clusters"])
    if VERDICTS_PATH.exists():
        info["verdicts_count"] = len(json.loads(VERDICTS_PATH.read_text()))
    try:
        info["topic_brief_total"] = conn.execute("SELECT COUNT(*) FROM topic_brief").fetchone()[0]
        info["topic_brief_with_status"] = conn.execute(
            "SELECT COUNT(*) FROM topic_brief WHERE status IS NOT NULL"
        ).fetchone()[0]
    except Exception:
        pass
    print(json.dumps(info, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    d.add_argument("--cluster-ids", type=int, nargs="*", default=None,
                   help="Restrict to specific cluster ids; default = all")
    d.add_argument("--member-chars", type=int, default=600,
                   help="Cap chars per member content snippet; default 600")
    d.set_defaults(fn=cmd_dump)
    a = sub.add_parser("apply")
    a.set_defaults(fn=cmd_apply)
    s = sub.add_parser("status")
    s.set_defaults(fn=cmd_status)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
